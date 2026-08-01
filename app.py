# -*- coding: utf-8 -*-
"""
app.py - Helmet Watch Manager (Flask Web UI)

Webike 等の商品在庫をブラウザで管理・監視する Web アプリ。
- セッション認証（パスワード1つ、config.json にハッシュ保存）
- 商品 CRUD、有効/無効切替
- Discord Webhook URL 設定、パスワード変更
- 監視の 手動実行 (checker.py を subprocess 呼び出し)
- ポート 8080 / host 0.0.0.0
"""
import json
import os
import sys
import subprocess
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, jsonify,
)

from werkzeug.security import generate_password_hash, check_password_hash

from checker import fetch_yahoo_variants

BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
CHECKER_SCRIPT = BASE_DIR / "checker.py"

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.environ.get(
    "HELMET_MANAGER_SECRET",
    "helmet-watch-manager-default-secret-change-me",
)


# ---------- JSON helpers ----------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    return load_json(CONFIG_FILE, {})


def load_products():
    return load_json(PRODUCTS_FILE, [])


def load_state():
    return load_json(STATE_FILE, {})


def next_product_id(products):
    ids = [p.get("id", 0) for p in products if isinstance(p.get("id"), int)]
    return (max(ids) + 1) if ids else 1


# ---------- auth ----------
def is_password_set():
    return bool(load_config().get("password_hash"))


def is_logged_in():
    return session.get("logged_in") is True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_password_set():
            flash("初回セットアップ: パスワードを設定してください。", "info")
            return redirect(url_for("setup"))
        if not is_logged_in():
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def redirect_to_setup_if_needed():
    """パスワード未設定時は /setup, /login(静的) 以外を /setup へ。"""
    if not is_password_set():
        endpoint = request.endpoint or ""
        if endpoint not in ("setup", "static"):
            return redirect(url_for("setup"))


# ---------- state label helper ----------
STATE_LABEL = {
    "IN_STOCK": "在庫あり",
    "SOLD_OUT": "売切れ",
    "UNKNOWN": "未確認",
}


# ---------- routes ----------
@app.route("/")
@login_required
def index():
    products = load_products()
    state = load_state()
    config = load_config()
    rows = []
    for p in products:
        s = state.get(str(p.get("id")), {})
        rows.append({
            "id": p.get("id"),
            "name": p.get("name", ""),
            "url": p.get("url", ""),
            "size_pattern": p.get("size_pattern", ""),
            "stock_keyword": p.get("stock_keyword", "在庫"),
            "enabled": p.get("enabled", True),
            "state": s.get("state"),
            "state_label": STATE_LABEL.get(s.get("state"), "未確認"),
            "detail": s.get("detail", ""),
            "updated_at": s.get("updated_at", ""),
        })
    webhook = config.get("discord_webhook_url", "")
    webhook_masked = ""
    if webhook:
        # マスク表示 (先頭12文字 + ... )
        webhook_masked = webhook[:18] + "..." if len(webhook) > 18 else webhook
    return render_template(
        "index.html",
        rows=rows,
        webhook=webhook,
        webhook_masked=webhook_masked,
    )


@app.route("/add", methods=["POST"])
@login_required
def add():
    name = (request.form.get("name") or "").strip()
    url = (request.form.get("url") or "").strip()
    size_pattern = (request.form.get("size_pattern") or "").strip() or "サイズ：L"
    stock_keyword = (request.form.get("stock_keyword") or "").strip() or "在庫"
    enabled = request.form.get("enabled") == "on"

    if not name or not url:
        flash("名前とURLは必須です。", "danger")
        return redirect(url_for("index"))

    products = load_products()
    products.append({
        "id": next_product_id(products),
        "name": name,
        "url": url,
        "size_pattern": size_pattern,
        "stock_keyword": stock_keyword,
        "enabled": enabled,
    })
    save_json(PRODUCTS_FILE, products)
    flash("商品を追加しました: {}".format(name), "success")
    return redirect(url_for("index"))


@app.route("/toggle/<int:pid>", methods=["POST"])
@login_required
def toggle(pid):
    products = load_products()
    for p in products:
        if p.get("id") == pid:
            p["enabled"] = not p.get("enabled", True)
            save_json(PRODUCTS_FILE, products)
            flash("「{}」を {} にしました。".format(
                p.get("name"), "有効" if p["enabled"] else "無効"), "info")
            break
    return redirect(url_for("index"))


@app.route("/delete/<int:pid>", methods=["POST"])
@login_required
def delete(pid):
    products = load_products()
    new_products = []
    removed_name = None
    for p in products:
        if p.get("id") == pid:
            removed_name = p.get("name")
        else:
            new_products.append(p)
    save_json(PRODUCTS_FILE, new_products)
    # state.json 側も掃除
    state = load_state()
    state.pop(str(pid), None)
    save_json(STATE_FILE, state)
    flash("削除しました: {}".format(removed_name or pid), "info")
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    config = load_config()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "webhook":
            webhook = (request.form.get("discord_webhook_url") or "").strip()
            config["discord_webhook_url"] = webhook
            save_json(CONFIG_FILE, config)
            flash("Discord Webhook URL を保存しました。", "success")
        elif action == "password":
            new_pw = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if not new_pw:
                flash("新しいパスワードを入力してください。", "danger")
            elif new_pw != confirm:
                flash("パスワードが一致しません。", "danger")
            elif len(new_pw) < 4:
                flash("パスワードは4文字以上にしてください。", "danger")
            else:
                config["password_hash"] = generate_password_hash(new_pw)
                save_json(CONFIG_FILE, config)
                flash("パスワードを変更しました。再ログインしてください。", "success")
                session.clear()
                return redirect(url_for("login"))
        return redirect(url_for("settings"))

    webhook = config.get("discord_webhook_url", "")
    webhook_masked = (webhook[:18] + "...") if len(webhook) > 18 else webhook
    return render_template("settings.html", webhook=webhook, webhook_masked=webhook_masked)


@app.route("/api/yahoo_variants", methods=["POST"])
@login_required
def yahoo_variants_api():
    """Yahoo!商品URLからサイズ/カラーの選択肢を取得"""
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URLを入力してください"}), 400
    if "yahoo.co.jp" not in url:
        return jsonify({"error": "Yahoo!ショッピングのURLを入力してください"}), 400
    result = fetch_yahoo_variants(url)
    return jsonify(result)


@app.route("/run")
@login_required
def run():
    """監視を手動1回実行。checker.py を subprocess で呼ぶ。"""
    try:
        result = subprocess.run(
            [sys.executable, str(CHECKER_SCRIPT)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode == 0:
            flash("チェック完了しました。\n" + out, "success")
        else:
            flash("チェック実行でエラーが発生しました。\nSTDOUT:\n" + out + "\nSTDERR:\n" + err, "danger")
    except subprocess.TimeoutExpired:
        flash("チェックがタイムアウトしました（120秒）。", "danger")
    except Exception as e:
        flash("チェック実行に失敗しました: {}".format(e), "danger")
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    # パスワード未設定なら setup へ（before_request でも処理）
    if not is_password_set():
        return redirect(url_for("setup"))
    if request.method == "POST":
        pw = request.form.get("password") or ""
        config = load_config()
        if check_password_hash(config.get("password_hash", ""), pw):
            session["logged_in"] = True
            flash("ログインしました。", "success")
            return redirect(url_for("index"))
        flash("パスワードが違います。", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("ログアウトしました。", "info")
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """初回パスワード設定。既に設定済みなら login へ。"""
    if is_password_set():
        flash("パスワードは既に設定済みです。設定変更は /settings から。", "info")
        return redirect(url_for("login"))
    if request.method == "POST":
        pw = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not pw:
            flash("パスワードを入力してください。", "danger")
        elif pw != confirm:
            flash("パスワードが一致しません。", "danger")
        elif len(pw) < 4:
            flash("パスワードは4文字以上にしてください。", "danger")
        else:
            config = load_config()
            config["password_hash"] = generate_password_hash(pw)
            save_json(CONFIG_FILE, config)
            flash("パスワードを設定しました。ログインしてください。", "success")
            return redirect(url_for("login"))
    return render_template("setup.html")


# ---------- main ----------
def main():
    config = load_config()
    port = int(config.get("port", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
