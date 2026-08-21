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
import re
import sys
import subprocess
import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, jsonify,
)

from werkzeug.security import generate_password_hash, check_password_hash

from checker import fetch_yahoo_variants, fetch_rakuten_variants, fetch_og_image

BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
CHECKER_SCRIPT = BASE_DIR / "checker.py"
STATE_HISTORY_FILE = BASE_DIR / "state_history.json"

# チェック実行状態管理（非同期化用）
_check_status = {"status": "idle", "started_at": "", "finished_at": "", "output": ""}

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


def site_name_of(url):
    u = (url or "").lower()
    if "yahoo.co.jp" in u:
        return "Yahoo!ショッピング"
    if "rakuten.co.jp" in u:
        return "楽天市場"
    if "webike.net" in u:
        return "Webike"
    if "yodobashi.com" in u:
        return "ヨドバシ"
    if "amazon.co.jp" in u:
        return "Amazon"
    return "その他"


def fetch_variants(url):
    """URL からサイト判定し、サイズ/カラー/商品名を取得。
    戻り値: {"name","sizes","colors"} または {"error":"..."}
    """
    u = (url or "").lower()
    if "rakuten.co.jp" in u:
        return fetch_rakuten_variants(url)
    if "yahoo.co.jp" in u:
        return fetch_yahoo_variants(url)
    # その他のサイト: 高速サイズ/カラー抽出（正規表現優先・GLMフォールバック）
    config = load_json(CONFIG_FILE, {})
    glm_key = (config.get("glm_api_key") or "").strip()
    from checker import fetch_variants_fast
    return fetch_variants_fast(url, glm_key)


# ---------- routes ----------
@app.route("/")
@login_required
def index():
    # POIZON出品管理画面へリダイレクト
    return redirect(url_for("poizon"))


@app.route("/add", methods=["POST"])
@login_required
def add():
    name = (request.form.get("name") or "").strip()
    url = (request.form.get("url") or "").strip()
    size_pattern = (request.form.get("size_pattern") or "").strip()
    # ヨドバシ・Amazon等はサイズ不要だが、Webike等のoption方式サイトでは必須。
    # 空欄の場合はチェック時にUNKNOWNになるが、エラーにはしない。
    stock_keyword = (request.form.get("stock_keyword") or "").strip()
    poizon_sku_id = (request.form.get("poizon_sku_id") or "").strip()
    enabled = request.form.get("enabled") == "on"

    if not name or not url:
        flash("名前とURLは必須です。", "danger")
        return redirect(url_for("index"))

    products = load_products()
    image_url = ""
    try:
        image_url = fetch_og_image(url)
    except Exception:
        image_url = ""
    products.append({
        "id": next_product_id(products),
        "name": name,
        "url": url,
        "size_pattern": size_pattern,
        "stock_keyword": stock_keyword,
        "enabled": enabled,
        "image_url": image_url,
        "poizon_sku_id": poizon_sku_id,
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


@app.route("/edit/<int:pid>", methods=["GET", "POST"])
@login_required
def edit(pid):
    """登録済み商品の編集（skuId含む全項目）。"""
    products = load_products()
    product = None
    for p in products:
        if p.get("id") == pid:
            product = p
            break
    if not product:
        flash("商品が見つかりません。", "danger")
        return redirect(url_for("index"))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip()
        if not name or not url:
            flash("名前とURLは必須です。", "danger")
            return render_template("edit.html", product=product)
        product["name"] = name
        product["url"] = url
        product["size_pattern"] = (request.form.get("size_pattern") or "").strip()
        product["stock_keyword"] = (request.form.get("stock_keyword") or "").strip()
        product["poizon_sku_id"] = (request.form.get("poizon_sku_id") or "").strip()
        save_json(PRODUCTS_FILE, products)
        flash("商品を更新しました: {}".format(name), "success")
        return redirect(url_for("index"))
    return render_template("edit.html", product=product)


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
        elif action == "poizon":
            config["poizon_delist_url"] = (request.form.get("poizon_delist_url") or "").strip()
            config["poizon_delist_token"] = (request.form.get("poizon_delist_token") or "").strip()
            save_json(CONFIG_FILE, config)
            flash("POIZON連動設定を保存しました。", "success")
        elif action == "auto_adjust":
            try:
                config["auto_adjust_min_profit"] = int(request.form.get("min_profit") or 0)
            except ValueError:
                config["auto_adjust_min_profit"] = 0
            save_json(CONFIG_FILE, config)
            flash("自動調整設定を保存しました。", "success")
        elif action == "external_token":
            config["external_api_token"] = (request.form.get("external_api_token") or "").strip()
            save_json(CONFIG_FILE, config)
            flash("外部連携トークンを保存しました。", "success")
        elif action == "glm":
            config["glm_api_key"] = (request.form.get("glm_api_key") or "").strip()
            save_json(CONFIG_FILE, config)
            flash("GLM API Key を保存しました。AI判定が有効になります。", "success")
        elif action == "poizon_api":
            config["poizon_api_id"] = (request.form.get("poizon_api_id") or "").strip()
            config["poizon_api_key"] = (request.form.get("poizon_api_key") or "").strip()
            save_json(CONFIG_FILE, config)
            flash("POIZON API設定を保存しました。", "success")
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
    poizon_url = config.get("poizon_delist_url", "")
    poizon_token = config.get("poizon_delist_token", "")
    poizon_token_masked = (poizon_token[:6] + "...") if len(poizon_token) > 6 else poizon_token
    glm_key = config.get("glm_api_key", "")
    glm_key_masked = (glm_key[:8] + "...") if len(glm_key) > 8 else glm_key
    poizon_api_id = config.get("poizon_api_id", "")
    poizon_api_key = config.get("poizon_api_key", "")
    poizon_api_id_masked = (poizon_api_id[:6] + "...") if len(poizon_api_id) > 6 else poizon_api_id
    ext_token = config.get("external_api_token", "")
    ext_token_masked = (ext_token[:8] + "...") if len(ext_token) > 8 else ext_token
    auto_min_profit = int(config.get("auto_adjust_min_profit") or 0)
    return render_template(
        "settings.html",
        webhook=webhook, webhook_masked=webhook_masked,
        poizon_url=poizon_url, poizon_token=poizon_token,
        poizon_token_masked=poizon_token_masked,
        glm_key=glm_key, glm_key_masked=glm_key_masked,
        poizon_api_id=poizon_api_id, poizon_api_key=poizon_api_key,
        poizon_api_id_masked=poizon_api_id_masked,
        ext_token=ext_token,
        ext_token_masked=ext_token_masked,
        auto_min_profit=auto_min_profit,
    )


@app.route("/api/yahoo_variants", methods=["POST"])
@app.route("/api/variants", methods=["POST"])
@login_required
def variants_api():
    """商品URLからサイズ/カラーの選択肢を取得（Yahoo!/楽天 をURLで自動判定）"""
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URLを入力してください"}), 400
    result = fetch_variants(url)
    return jsonify(result)


@app.route("/update", methods=["POST"])
@login_required
def update():
    """GitHub から最新版をDLして自動再起動（ユーザーデータは保持）

    raw.githubusercontent.com がレート制限(429)に掛かる場合があるため、
    GitHub API (contents/base64) を第一経路、raw をフォールバックとする。
    """
    import urllib.request as _urlreq
    import threading as _th
    import os as _os
    import base64 as _b64
    repo = "hishoxwx-lang/helmet-watch-manager"
    branch = "main"
    api_base = "https://api.github.com/repos/{}/contents/{}?ref={}".format(repo, "{}", branch)
    raw_base = "https://raw.githubusercontent.com/{}/main/".format(repo)

    jsd_base = "https://cdn.jsdelivr.net/gh/{}/@main/".format(repo)
    # 最新タグ（jsDelivrの@mainキャッシュは最大12h遅延するがタグは即時）
    jsd_tag = None
    try:
        req_t = _urlreq.Request("https://api.github.com/repos/{}/tags?per_page=1".format(repo),
                                headers={"User-Agent": "helmet-watch-manager-updater"})
        import json as _json_t
        with _urlreq.urlopen(req_t, timeout=15) as resp:
            _tags = _json_t.loads(resp.read().decode("utf-8"))
        if _tags:
            jsd_tag = "https://cdn.jsdelivr.net/gh/{}/@{}/".format(repo, _tags[0]["name"])
    except Exception:
        jsd_tag = None

    def _fetch_file(rel):
        """GitHub API → jsDelivr(タグ) → jsDelivr(main) → raw の順で取得。"""
        # 1) GitHub API
        req = _urlreq.Request(api_base.format(rel), headers={
            "Accept": "application/vnd.github.raw+json",
            "User-Agent": "helmet-watch-manager-updater",
        })
        try:
            with _urlreq.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception:
            pass
        # 2) jsDelivr CDN（最新タグ・即時配信）
        if jsd_tag:
            try:
                with _urlreq.urlopen(jsd_tag + rel, timeout=30) as resp:
                    return resp.read()
            except Exception:
                pass
        # 3) jsDelivr CDN（main・最大12hキャッシュ遅延あり）
        try:
            with _urlreq.urlopen(jsd_base + rel, timeout=30) as resp:
                return resp.read()
        except Exception:
            pass
        # 4) raw（最終フォールバック）
        with _urlreq.urlopen(raw_base + rel, timeout=30) as resp:
            return resp.read()

    targets = [
        "app.py", "checker.py", "poizon_api.py", "requirements.txt",
        "templates/index.html", "templates/login.html",
        "templates/edit.html", "templates/poizon.html",
        "templates/settings.html", "templates/setup.html",
    ]
    errors = []
    for rel in targets:
        try:
            dest = BASE_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(dest) + ".tmp"
            data = _fetch_file(rel)
            with open(tmp, "wb") as f:
                f.write(data)
            _os.replace(tmp, dest)
        except Exception as e:
            errors.append("{}: {}".format(rel, e))
    if errors:
        flash("更新エラー: " + "; ".join(errors), "danger")
        return redirect(url_for("index"))
    flash("最新版に更新しました。再起動します（数秒お待ちください）...", "success")

    def _respawn():
        import time
        time.sleep(1.5)
        subprocess.Popen([sys.executable, str(BASE_DIR / "app.py")], cwd=str(BASE_DIR))
        _os._exit(0)

    _th.Thread(target=_respawn, daemon=True).start()
    return redirect(url_for("index"))


def _run_checker_async():
    """バックグラウンドで checker.py を実行。"""
    global _check_status
    try:
        result = subprocess.run(
            [sys.executable, str(CHECKER_SCRIPT)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        _check_status["status"] = "done"
        _check_status["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _check_status["output"] = out if result.returncode == 0 else "STDOUT:\n" + out + "\nSTDERR:\n" + err
    except subprocess.TimeoutExpired:
        _check_status["status"] = "done"
        _check_status["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _check_status["output"] = "タイムアウト（300秒）"
    except Exception as e:
        _check_status["status"] = "done"
        _check_status["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _check_status["output"] = "エラー: {}".format(e)


@app.route("/run")
@login_required
def run():
    """監視を手動1回実行（非同期・バックグラウンド）。"""
    global _check_status
    if _check_status["status"] == "running":
        return jsonify({"status": "running", "message": "既にチェック実行中です"}), 200
    _check_status = {
        "status": "running",
        "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": "",
        "output": "",
    }
    import threading
    t = threading.Thread(target=_run_checker_async, daemon=True)
    t.start()
    return jsonify({"status": "running", "message": "チェックを開始しました"}), 200


@app.route("/api/check_status")
@login_required
def check_status_api():
    """チェック実行状態を返す。"""
    if not is_logged_in():
        return jsonify({"error": "ログインが必要です"}), 401
    return jsonify(_check_status)


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


# ==================== POIZON 出品管理 ====================

POIZON_LINKS_FILE = BASE_DIR / "poizon_links.json"

# 有名ブランドリスト（spuTitle先頭一致で判定）
KNOWN_BRANDS = [
    "SHOEI", "ARAI", "Arai", "On", "Louis Vuitton", "LOUIS",
    "Dior", "DIOR", "SALOMON", "Salomon", "COACH", "Coach",
    "Michael Kors", "MICHAEL", "LEGO", "CASIO", "PUMA", "adidas",
    "Nike", "NIKE", "SEIKO", "Seiko", "G-SHOCK", "Apple",
]

def _extract_brand(title):
    """spuTitleの先頭からブランド名を判定。"""
    if not title:
        return "その他"
    for b in KNOWN_BRANDS:
        if title.lower().startswith(b.lower()):
            bl = b.lower()
            if bl in ("arai",): return "ARAI"
            if bl in ("louis", "louis vuitton"): return "LOUIS VUITTON"
            if bl in ("dior",): return "DIOR"
            if bl in ("salomon",): return "SALOMON"
            if bl in ("coach",): return "COACH"
            if bl in ("michael", "michael kors"): return "MICHAEL KORS"
            if bl in ("on",): return "On"
            if bl in ("seiko",): return "SEIKO"
            return b.upper()
    return "その他"


def load_poizon_links():
    """POIZON出品と仕入元URLの紐付けを読み込む。
    形式: {"skuId": {"url": "...", "name": "...", "enabled": true}}
    """
    return load_json(POIZON_LINKS_FILE, {})


def save_poizon_links(links):
    with open(POIZON_LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=2)


@app.route("/poizon")
@login_required
def poizon():
    """POIZON出品一覧ページ"""
    config = load_config()
    links = load_poizon_links()

    # POIZON API ID/KEY の確認
    has_api = bool((config.get("poizon_api_id") or "").strip() and
                   (config.get("poizon_api_key") or "").strip())

    # API一覧は JavaScript（fetch API）で非同期取得する（ページ表示をブロックしない）
    return render_template(
        "poizon.html",
        has_api=has_api,
        links=links,
    )


@app.route("/api/poizon/listings")
@login_required
def poizon_listings_api():
    """POIZON出品一覧をJSONで返す（非同期API）"""
    if not is_logged_in():
        return jsonify({"error": "ログインが必要です"}), 401
    from poizon_api import get_active_listings
    config = load_config()
    result = get_active_listings(config)

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 200  # エラーも200で返す（JS側で表示）

    # 紐付け情報 + 商品情報 + 在庫状態を付与
    from poizon_api import fetch_poizon_images_batch
    links = load_poizon_links()
    state = load_state()
    products = load_products()
    # skuId → product のマップ
    sku_to_product = {}
    for p in products:
        sid = str(p.get("poizon_sku_id", ""))
        if sid:
            sku_to_product[sid] = p

    # 全SKU商品情報をバッチ取得（画像・品番・ブランド名）
    all_sku_ids = [item.get("skuId", 0) for item in result if item.get("skuId")]
    app_key = config.get("poizon_api_id", "")
    app_secret = config.get("poizon_api_key", "")
    from poizon_api import fetch_poizon_sku_info_batch
    sku_info_map = fetch_poizon_sku_info_batch(all_sku_ids, app_key, app_secret) if all_sku_ids else {}
    image_map = {sid: d.get("image", "") for sid, d in sku_info_map.items()}

    # 市場価格をバッチ取得
    from poizon_api import fetch_market_prices_batch
    price_map = fetch_market_prices_batch(all_sku_ids, app_key, app_secret) if all_sku_ids else {}

    enriched = []
    for item in result:
        sku_id = str(item.get("skuId", ""))
        spu_id = item.get("spuId", "")
        link_info = links.get(sku_id, {})

        # 商品名（spuTitle）
        spu_title = item.get("spuTitle", "")

        # カラー・サイズ（skuSaleProp または regionSalePvInfoList）
        color = ""
        size = ""
        sku_prop_str = item.get("skuSaleProp", "[]")
        try:
            sku_props = json.loads(sku_prop_str) if isinstance(sku_prop_str, str) else sku_prop_str
            for p in sku_props:
                if p.get("name", "").lower() in ("color", "カラー", "色"):
                    color = p.get("value", "")
                elif p.get("name", "").lower() in ("size", "サイズ"):
                    size = p.get("value", "")
        except Exception:
            pass
        # フォールバック: regionSalePvInfoList
        if not color or not size:
            for info in item.get("regionSalePvInfoList", []):
                if info.get("name") in ("カラー", "Color", "色") and not color:
                    color = info.get("localValue", "")
                elif info.get("name") in ("サイズ", "Size") and not size:
                    size = info.get("localValue", "")

        enriched.append({
            "sellerBiddingNo": item.get("sellerBiddingNo", ""),
            "skuId": sku_id,
            "spuId": spu_id,
            "title": spu_title,
            "price": item.get("price", 0),
            "currency": item.get("currency", ""),
            "color": color,
            "size": size,
            "tradeStatus": item.get("tradeStatus", 0),
            "tradeSubStatus": item.get("tradeSubStatus", 0),
            "quantity": item.get("quantity", 0),
            "image_url": image_map.get(sku_id, ""),
            # 品番・ブランド名（by-sku APIから取得）
            "article_number": sku_info_map.get(sku_id, {}).get("article_number", ""),
            "brand": sku_info_map.get(sku_id, {}).get("brand", "") or _extract_brand(spu_title),
            "market_min": price_map.get(sku_id, {}).get("min_price", 0),
            "cart_price": price_map.get(sku_id, {}).get("cart_price", 0),
            "market_global_min": price_map.get(sku_id, {}).get("global_min", 0),
            "market_jp": price_map.get(sku_id, {}).get("jp_price", 0),
            "global_sku_id": item.get("globalSkuId", 0),
            "source_url": link_info.get("url", ""),
            "source_name": link_info.get("name", ""),
            "cost_price": link_info.get("cost_price", 0) or 0,
            "profit": 0,
            "linked": bool(link_info.get("url")),
            "monitoring": link_info.get("enabled", False),
            # 在庫状態（products.jsonのpoizon_sku_id経由でstate.jsonから取得）
            "stock_state": "",
            "stock_detail": "",
            "stock_updated": "",
        })

        # 利益計算（自価格 - 仕入値）
        cp = enriched[-1].get("cost_price", 0)
        my_price = enriched[-1].get("price", 0) or 0
        if cp and my_price:
            enriched[-1]["profit"] = my_price - cp

        # 在庫状態を付与
        product = sku_to_product.get(sku_id)
        if product:
            s = state.get(str(product.get("id", "")), {})
            enriched[-1]["stock_state"] = s.get("state", "")
            enriched[-1]["stock_detail"] = s.get("detail", "")
            enriched[-1]["stock_updated"] = s.get("updated_at", "")

    return jsonify({"listings": enriched})


@app.route("/api/poizon/update_price", methods=["POST"])
@login_required
def poizon_update_price_api():
    """POIZON出品の価格を変更（apiId=44）"""
    if not is_logged_in():
        return jsonify({"error": "ログインが必要です"}), 401

    seller_bidding_no = (request.form.get("seller_bidding_no") or "").strip()
    global_sku_id = (request.form.get("global_sku_id") or "").strip()
    new_price = (request.form.get("price") or "").strip()

    if not seller_bidding_no or not global_sku_id or not new_price:
        return jsonify({"error": "パラメータ不足"}), 400

    try:
        new_price = int(new_price)
    except ValueError:
        return jsonify({"error": "価格は数値で入力"}), 400

    if new_price < 1:
        return jsonify({"error": "価格は1円以上"}), 400

    config = load_config()
    app_key = (config.get("poizon_api_id") or "").strip()
    app_secret = (config.get("poizon_api_key") or "").strip()

    if not app_key or not app_secret:
        return jsonify({"error": "POIZON API設定が未設定"}), 400

    from poizon_api import update_listing_price
    result = update_listing_price(app_key, app_secret, seller_bidding_no, int(global_sku_id), new_price)
    return jsonify(result)


@app.route("/api/poizon/link", methods=["POST"])
@login_required
def poizon_link_api():
    sku_id = (request.form.get("sku_id") or "").strip()
    url = (request.form.get("url") or "").strip()
    name = (request.form.get("name") or "").strip()
    enabled = request.form.get("enabled") != "off"

    if not sku_id:
        return jsonify({"error": "skuIdが必要"}), 400

    links = load_poizon_links()

    if request.form.get("action") == "unlink":
        links.pop(sku_id, None)
        save_poizon_links(links)
        return jsonify({"ok": True})

    if not url:
        return jsonify({"error": "URLが必要"}), 400

    cost_price = (str(req_or_form_cost(request) or "0")).strip()
    try:
        cost_price = int(float(cost_price))
    except ValueError:
        cost_price = 0

    links[sku_id] = {
        "url": url,
        "name": name,
        "enabled": enabled,
    }
    if cost_price > 0:
        links[sku_id]["cost_price"] = cost_price
    save_poizon_links(links)

    # products.json にも追加（監視対象にする）
    products = load_products()
    # 既存確認（skuId一致）
    existing = None
    for p in products:
        if str(p.get("poizon_sku_id", "")) == sku_id:
            existing = p
            break

    if existing:
        existing["url"] = url
        existing["name"] = name or existing.get("name", "")
        existing["enabled"] = enabled
    else:
        image_url = ""
        try:
            image_url = fetch_og_image(url)
        except Exception:
            pass
        products.append({
            "id": next_product_id(products),
            "name": name or "POIZON:{}".format(sku_id),
            "url": url,
            "size_pattern": "",
            "stock_keyword": "",
            "enabled": enabled,
            "image_url": image_url,
            "poizon_sku_id": sku_id,
        })

    save_json(PRODUCTS_FILE, products)
    return jsonify({"ok": True})


# ---------- 履歴API ----------
@app.route("/api/history")
@app.route("/api/history/<int:product_id>")
@login_required
def history_api(product_id=None):
    """在庫変化履歴をJSONで返す。product_id指定時はその商品のみ。"""
    if not is_logged_in():
        return jsonify({"error": "ログインが必要です"}), 401
    history = load_json(STATE_HISTORY_FILE, [])
    if product_id is not None:
        history = [h for h in history if h.get("product_id") == product_id]
    # 最新50件（全商品一覧用）または該当全件（個別商品用）
    if product_id is None:
        history = history[-50:]
    history.reverse()  # 新しい順
    return jsonify({"history": history})


# ---------- Discord テスト通知API ----------
@app.route("/api/test_discord", methods=["POST"])
@login_required
def test_discord_api():
    """Discord Webhookにテスト通知を送信。"""
    if not is_logged_in():
        return jsonify({"error": "ログインが必要です"}), 401
    config = load_config()
    webhook = (config.get("discord_webhook_url") or "").strip()
    if not webhook:
        return jsonify({"error": "Discord Webhook URLが未設定です"}), 400
    try:
        import requests as _req
        payload = {"content": "🔔 テスト通知: 在庫通知くんのDiscord通知設定は正常に動作しています。"}
        r = _req.post(webhook, json=payload, headers={"Content-Type": "application/json; charset=utf-8"}, timeout=10)
        if 200 <= r.status_code < 300:
            return jsonify({"ok": True, "status_code": r.status_code})
        return jsonify({"error": "HTTP {}: {}".format(r.status_code, r.text[:200])}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 200


def req_or_form_cost(req):
    return req.form.get("cost_price") or (req.get_json(silent=True) or {}).get("cost_price", "")


# ---------- 外部連携API（Chrome拡張から・トークン認証） ----------
@app.route("/api/external/link", methods=["POST"])
def external_link_api():
    """Chrome拡張（バックオフィスURL登録拡張）からの仕入先URL登録。

    ログイン不要の代わりに X-Api-Token ヘッダーで認証する。
    config.json の external_api_token が未設定の場合は 403 を返す。

    パラメータ（form or JSON）:
        sku_id: POIZONのSKU ID（必須）
        url: 仕入先URL（必須）
        name: 商品名（任意）
        enabled: 監視有効（デフォルト on）
        token: 認証トークン（ヘッダー X-Api-Token でも可）
    """
    # JSON でも form でも受け取る
    if request.is_json:
        req = request.get_json(silent=True) or {}
    else:
        req = request.form

    config = load_config()
    expected = (config.get("external_api_token") or "").strip()

    # 認証（ヘッダー優先、なければパラメータ）
    token = request.headers.get("X-Api-Token", "") or (req.get("token") or "").strip()
    if not expected:
        return jsonify({"error": "外部連携トークンが未設定です。設定画面で設定してください。"}), 403
    if token != expected:
        return jsonify({"error": "トークンが不正です"}), 403

    sku_id = (str(req.get("sku_id") or "")).strip()
    url = (str(req.get("url") or "")).strip()
    name = (str(req.get("name") or "")).strip()
    enabled = str(req.get("enabled") or "on") != "off"

    if not sku_id:
        return jsonify({"error": "sku_idが必要です"}), 400
    if not url:
        return jsonify({"error": "urlが必要です"}), 400

    # poizon_links.json に保存
    links = load_poizon_links()

    if req.get("action") == "unlink":
        links.pop(sku_id, None)
        save_poizon_links(links)
        return jsonify({"ok": True})

    links[sku_id] = {
        "url": url,
        "name": name,
        "enabled": enabled,
    }
    save_poizon_links(links)

    # products.json にも追加（監視対象化）
    products = load_products()
    existing = None
    for p in products:
        if str(p.get("poizon_sku_id", "")) == sku_id:
            existing = p
            break

    if existing:
        existing["url"] = url
        if name:
            existing["name"] = name
        existing["enabled"] = enabled
    else:
        products.append({
            "id": next_product_id(products),
            "name": name or "POIZON:{}".format(sku_id),
            "url": url,
            "size_pattern": "",
            "stock_keyword": "",
            "enabled": enabled,
            "image_url": "",
            "poizon_sku_id": sku_id,
        })

    save_json(PRODUCTS_FILE, products)
    return jsonify({"ok": True, "sku_id": sku_id, "monitoring": enabled})


@app.route("/api/external/state", methods=["GET"])
def external_state_api():
    """Chrome拡張から在庫状態を照会（トークン認証）。

    ?sku_ids=111,222,333 の形式で複数指定可能。
    戻り値: {"states": {"111": {"state": "IN_STOCK", "updated_at": "..."}, ...}}
    """
    config = load_config()
    expected = (config.get("external_api_token") or "").strip()
    token = request.headers.get("X-Api-Token", "") or (request.args.get("token") or "").strip()
    if not expected:
        return jsonify({"error": "外部連携トークンが未設定です"}), 403
    if token != expected:
        return jsonify({"error": "トークンが不正です"}), 403

    sku_ids = [s.strip() for s in (request.args.get("sku_ids") or "").split(",") if s.strip()]
    if not sku_ids:
        return jsonify({"error": "sku_idsが必要です"}), 400

    products = load_products()
    state = load_state()
    sku_to_pid = {}
    for p in products:
        sid = str(p.get("poizon_sku_id", ""))
        if sid:
            sku_to_pid[sid] = str(p.get("id", ""))

    result = {}
    links = load_poizon_links()
    for sid in sku_ids:
        info = {"state": "", "detail": "", "updated_at": "", "linked": sid in links}
        pid = sku_to_pid.get(sid)
        if pid:
            s = state.get(pid, {})
            info["state"] = s.get("state", "")
            info["detail"] = s.get("detail", "")
            info["updated_at"] = s.get("updated_at", "")
        result[sid] = info

    return jsonify({"states": result})


# ---------- URL一括紐付けAPI（SKU自動照合） ----------
@app.route("/api/external/auto_link", methods=["POST"])
def external_auto_link_api():
    """仕入先URLを1つ受け取り、サイズ展開を抽出してPOIZON出品SKUに自動紐付けする。

    フロー:
      1. 仕入先URLを取得（Yahoo!ショッピングは__NEXT_DATA__からサイズ抽出）
      2. POIZON出品一覧（apiId=51）を取得
      3. サイズ（or URLに含まれる品番）で照合
      4.一致した全SKUに poizon_links.json + products.json 登録（監視開始）

    パラメータ: url（必須）, token / X-Api-Token（必須）
    戻り値: {"ok": true, "linked": [{sku_id, size, name}...], "skipped": [...]}
    """
    if request.is_json:
        req = request.get_json(silent=True) or {}
    else:
        req = request.form

    config = load_config()
    expected = (config.get("external_api_token") or "").strip()
    token = request.headers.get("X-Api-Token", "") or (req.get("token") or "").strip()
    if not expected:
        return jsonify({"error": "外部連携トークンが未設定です。設定画面で設定してください。"}), 403
    if token != expected:
        return jsonify({"error": "トークンが不正です"}), 403

    url = (str(req.get("url") or "")).strip()
    if not url or not url.startswith("http"):
        return jsonify({"error": "urlが必要です"}), 400

    # --- 1. 仕入先ページからサイズ展開を抽出 ---
    variants = []  # [{"size": "M", "color": "", "label": "...", "cost": 15999}]
    page_title = ""
    product_code = ""
    cost_price = 0  # 仕入値（全SKU共通フォールバック）
    try:
        from checker import fetch_html_auto
        html = fetch_html_auto(url)
    except Exception as e:
        return jsonify({"error": "仕入先ページの取得に失敗: {}".format(e)}), 200

    try:
        import re as _re
        import json as _json
        m = _re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, _re.S)
        if m and "yahoo.co.jp" in url.lower():
            data = _json.loads(m.group(1))
            item = data.get("props", {}).get("pageProps", {}).get("item", {})
            page_title = item.get("name", "")
            # 仕入値（販売価格）抽出: applicablePrice（全SKU共通）or priceTable
            cost_price = 0
            try:
                cost_price = int(item.get("applicablePrice") or 0)
                if not cost_price:
                    pt = item.get("priceTable") or {}
                    p1 = pt.get("price1") or {}
                    cost_price = int(p1.get("price") or 0)
            except Exception:
                cost_price = 0
            # individualItemList: skuId + optionList(サイズ/カラー) + stock
            for it in item.get("individualItemList", []):
                size = ""
                color = ""
                sku_price = int(it.get("price") or 0)
                for opt in it.get("optionList", []):
                    nm = (opt.get("name") or "").lower()
                    if nm in ("サイズ", "size"):
                        size = opt.get("choiceName", "")
                    elif nm in ("カラー", "色", "color"):
                        color = opt.get("choiceName", "")
                if size or color:
                    variants.append({"size": size, "color": color, "label": (color + " " + size).strip(),
                                     "cost": sku_price or cost_price})
            # 品番抽出（優先順位）:
            # 1. sellerManagedItemId / srid（店舗管理ID = ほぼ品番そのもの・最も確実）
            #    例: 3MG10051043 / wf945-jz8731
            # 2. skuIdListのキー（例: WF945-JZ8731-M / 3MG10051043BLKBLK250）
            for _id_field in ("sellerManagedItemId", "srid"):
                _idv = str(item.get(_id_field) or "").strip().upper()
                if _idv and _re.search(r"[A-Z]", _idv) and _re.search(r"\d", _idv) and len(_idv) >= 6:
                    product_code = _idv
                    break
            if not product_code:
                # skuIdList: ハイフン区切り品番（末尾サイズ除去）or 連結形式（英数字境界でカラーコード分離）
                for ent in item.get("skuIdList", []):
                    if isinstance(ent, dict):
                        for k in ent.keys():
                            ku = k.upper()
                            parts = ku.split("-")
                            # 末尾セグメントがサイズ表記（S/M/L/XXL/数字のみ）なら品番から除去
                            while len(parts) > 1 and _re.fullmatch(r"(X{0,2}[SML]|\d{1,3}(?:\.\d)?CM?|US\d+)", parts[-1]):
                                parts.pop()
                            cand = "-".join(parts)
                            if len(parts) == 1:
                                # ハイフンなし連結形式（例: 3MG10051043BLKBLK250）
                                # 品番本体の末尾は数字（3MG10051043）で、その後に英字カラー+サイズが連結される。
                                mm2 = _re.match(r"^(\d{0,3}[A-Z]{1,4}\d{4,10})", ku)
                                if mm2 and len(mm2.group(1)) >= 8:
                                    cand = mm2.group(1)
                            # 品番として妥当: 6文字以上で英字と数字を両方含む
                            if len(cand) >= 6 and _re.search(r"[A-Z]", cand) and _re.search(r"\d", cand):
                                product_code = cand
                                break
                    if product_code:
                        break
            if not product_code:
                # ページタイトルやheadlineから品番抽出（例: JZ8731 / 3MG10064852）
                # パターン: 英字始まり品番（JZ8731等）or 数字始まり品番（3MG10064852等）
                mm = _re.search(r"\b([A-Z]{1,3}\d{4,6}(?:-[A-Z0-9]+)?)\b", page_title)
                if not mm:
                    mm = _re.search(r"\b(\d{1,3}[A-Z]{1,4}\d{3,8}(?:-[A-Z0-9]+)?)\b", page_title, _re.I)
                    if mm:
                        product_code = mm.group(1).upper()
                if mm:
                    product_code = mm.group(1)
        if not product_code:
            # URL末尾から品番抽出フォールバック（例: /wf945-jz8731.html）
            mm = _re.search(r"/([A-Za-z0-9]+-[A-Za-z0-9]+)(?:-[A-Za-z0-9]+)?\.html", url)
            if mm:
                product_code = mm.group(1).upper()
        if not variants and m and "yahoo.co.jp" in url.lower():
            # Yahooだがバリアント抽出できず: 価格だけでも保存
            try:
                cost_price = int(item.get("applicablePrice") or 0)
            except Exception:
                cost_price = 0
        if not variants:
            # Yahoo以外のサイト: 汎用バリアント抽出（正規表現）
            from checker import fetch_variants_fast
            try:
                v = fetch_variants_fast(url, (config.get("glm_api_key") or "").strip())
                for s in (v.get("sizes") or [])[:20]:
                    variants.append({"size": str(s), "color": "", "label": str(s)})
            except Exception:
                pass
    except Exception as e:
        # 抽出失敗でも続行（出品一覧側の品番照合で試みる）
        pass

    # --- 2. POIZON出品一覧取得 ---
    from poizon_api import get_active_listings
    result = get_active_listings(config)
    if isinstance(result, dict) and "error" in result:
        return jsonify({"error": "POIZON出品一覧の取得に失敗: {}".format(result.get("error", ""))}), 200

    # --- 3. 品番・サイズで照合 ---
    # by-sku APIで品番・ブランド取得（バッチ）
    all_sku_ids = [item.get("skuId") for item in result if item.get("skuId")]
    app_key = config.get("poizon_api_id", "")
    app_secret = config.get("poizon_api_key", "")
    from poizon_api import fetch_poizon_sku_info_batch
    sku_info_map = fetch_poizon_sku_info_batch(all_sku_ids, app_key, app_secret) if all_sku_ids else {}

    def _size_of(item):
        sku_prop_str = item.get("skuSaleProp", "[]")
        size = ""
        color = ""
        try:
            props = json.loads(sku_prop_str) if isinstance(sku_prop_str, str) else sku_prop_str
            for p in props:
                nm = (p.get("name") or "").lower()
                if nm in ("size", "サイズ"):
                    size = p.get("value", "")
                elif nm in ("color", "カラー", "色"):
                    color = p.get("value", "")
        except Exception:
            pass
        if not size:
            for info in item.get("regionSalePvInfoList", []):
                if info.get("name") in ("サイズ", "Size") and not size:
                    size = info.get("localValue", "")
                elif info.get("name") in ("カラー", "Color", "色") and not color:
                    color = info.get("localValue", "")
        return size, color

    def _norm_size(s):
        return str(s or "").strip().upper().replace("サイズ", "").replace("SIZE", "").replace("CM", "").replace("ｃｍ", "").replace(".", "").replace(" ", "")

    # US/EU表記対応: 仕入先ページはUS表記（US7(25cm)等）、POIZONはEU表記（40等）が多い。
    # 生のサイズ文字列から US/CM/EU トークンを抽出し、EU換算の集合で照合する。
    _US_TO_EU = {
        "5": "37.5", "5.5": "38", "6": "38.5", "6.5": "39", "7": "40", "7.5": "40.5",
        "8": "41", "8.5": "42", "9": "42.5", "9.5": "43", "10": "44", "10.5": "44.5",
        "11": "45", "11.5": "45.5", "12": "46", "12.5": "47", "13": "48",
    }
    _CM_TO_EU = {
        "22.5": "37.5", "23": "38", "23.5": "38.5", "24": "39", "24.5": "40",
        "25": "40.5", "25.5": "41", "26": "42", "26.5": "42.5", "27": "43",
        "27.5": "44", "28": "44.5", "28.5": "45", "29": "45.5", "29.5": "46", "30": "47",
    }

    def _parse_size_tokens(raw):
        """生のサイズ文字列から (us, cm, eu) を抽出。"""
        s = str(raw or "").strip().upper().replace("サイズ", "").replace("SIZE", "").replace(" ", "")
        us, cm, eu = "", "", ""
        mm = _re2.search(r"US\.?(\d{1,2}(?:\.\d)?)", s)
        if mm:
            us = mm.group(1)
        mm = _re2.search(r"(\d{2}(?:\.\d)?)\s*(?:CM|ｃｍ)", s)
        if mm:
            cm = mm.group(1)
            if cm.endswith(".0"):
                cm = cm[:-2]  # 25.0 → 25（表キー正規化）
        mm = _re2.match(r"^(\d{1,2}(?:\.\d)?)(?:CM)?$", s) or _re2.match(r"^EU(\d{1,2}(?:\.\d)?)", s)
        if mm:
            eu = mm.group(1)
        return us, cm, eu

    def _size_equivalent(a, b):
        """サイズ照合（US/CM/EU/SML表記の違いを吸収）。生のサイズ文字列を受け取る。"""
        sa = str(a or "").strip().upper()
        sb = str(b or "").strip().upper()
        if not sa or not sb:
            return False
        na = sa.replace("サイズ", "").replace("SIZE", "").replace(" ", "").replace(".", "")
        nb = sb.replace("サイズ", "").replace("SIZE", "").replace(" ", "").replace(".", "")
        # 文字列表記（S/M/L/XL等）は直接比較
        if not any(ch.isdigit() for ch in na) and not any(ch.isdigit() for ch in nb):
            return na == nb
        if not any(ch.isdigit() for ch in na) or not any(ch.isdigit() for ch in nb):
            return False
        a_us, a_cm, a_eu = _parse_size_tokens(a)
        b_us, b_cm, b_eu = _parse_size_tokens(b)

        def _to_eu_set(us, cm, eu, raw):
            s = set()
            if eu:
                s.add(eu)
            if us and us in _US_TO_EU:
                s.add(_US_TO_EU[us])
            if cm and cm in _CM_TO_EU:
                s.add(_CM_TO_EU[cm])
            mm = _re2.match(r"^(\d{2}(?:\.\d)?)$", str(raw or "").strip().upper().replace("サイズ", "").replace("SIZE", "").replace(" ", ""))
            if mm and not us:
                s.add(mm.group(1))
            return s

        ea = _to_eu_set(a_us, a_cm, a_eu, a)
        eb = _to_eu_set(b_us, b_cm, b_eu, b)
        return bool(ea & eb)

    matched = []
    for item in result:
        sku_id = str(item.get("skuId", ""))
        if not sku_id:
            continue
        info = sku_info_map.get(sku_id, {})
        item_article = (info.get("article_number") or "").strip().upper()
        # POIZON品番はCJKサフィックス付きのことがある（例: JZ8731包）
        import re as _re2
        item_article = _re2.sub(r"[^A-Z0-9\-]", "", item_article)
        size, color = _size_of(item)
        spu_title = item.get("spuTitle", "")

        # 照合条件: 品番一致が前提。その上でサイズ展開と照合。
        # （品番なし照合は誤紐付けを起こすため禁止）
        hit = False
        # 品番照合: 完全一致 or ハイフン区切りのトークン単位一致のみ許容。
        # 単純な部分一致（in）は「Z8」が「WF945-JZ8731」にヒットする誤照合を起こすため禁止。
        #   Yahoo: WF945-JZ8731 / POIZON: JZ8731 → トークン {WF945,JZ8731} ∩ {JZ8731} ≠ ∅ でOK
        #   Yahoo: WF945-JZ8731 / POIZON: Z8 → トークン不一致 → 除外
        pc_u = (product_code or "").upper()
        pc_tokens = set(t for t in pc_u.replace("_", "-").split("-") if len(t) >= 4)
        ia_tokens = set(t for t in item_article.replace("_", "-").split("-") if len(t) >= 4)
        article_match = bool(pc_u and item_article and (
            pc_u == item_article or (pc_tokens & ia_tokens)
        ))
        if article_match:
            if variants:
                for v in variants:
                    vs = str(v.get("size") or "").strip()
                    ps = str(size or "").strip()
                    # サイズ一致（US/CM/EU表記の違いは _size_equivalent で吸収）
                    if vs and ps and _size_equivalent(vs, ps):
                        hit = True
                        break
                    # バリアントにサイズ情報なし or POIZON側にサイズなし → 品番一致で全SKU対象
                    if not vs or not ps:
                        hit = True
                        break
            else:
                # バリアント抽出できなかった: 品番一致のみで全SKU
                hit = True

        if hit:
            matched.append({"sku_id": sku_id, "size": size, "color": color,
                            "name": spu_title, "article": item_article})

    if not matched:
        hint = ""
        if not product_code:
            hint = " ※仕入先ページから品番を検出できませんでした。Yahoo!/楽天の商品ページURLかご確認ください"
        elif variants:
            hint = " ※品番{}は検出しましたが、サイズ展開がPOIZON出品と一致しません（US/EU表記差異の可能性）".format(product_code)
        return jsonify({"ok": False,
                        "error": "一致するPOIZON出品が見つかりませんでした{}".format(hint),
                        "page_title": page_title, "product_code": product_code,
                        "variants": [v.get("label") for v in variants],
                        "poizon_count": len(result)}), 200

    # --- 4. 全matched SKUに登録 ---
    links = load_poizon_links()
    products = load_products()
    sku_to_product = {}
    for p in products:
        sid = str(p.get("poizon_sku_id", ""))
        if sid:
            sku_to_product[sid] = p

    linked = []
    for m_ in matched:
        sid = m_["sku_id"]
        label = (m_["name"] or "")[:60] + (" [" + (m_["color"] + " " + m_["size"]).strip() + "]" if (m_["size"] or m_["color"]) else "")
        # 仕入値: バリエーション毎の価格 > ページ共通価格
        v_cost = 0
        for v in variants:
            if v.get("size") == m_["size"] and v.get("cost"):
                v_cost = int(v.get("cost"))
                break
        link_cost = v_cost or cost_price
        links[sid] = {"url": url, "name": label, "enabled": True}
        if link_cost:
            links[sid]["cost_price"] = link_cost
        if sid in sku_to_product:
            sku_to_product[sid]["url"] = url
            sku_to_product[sid]["enabled"] = True
        else:
            products.append({
                "id": next_product_id(products),
                "name": label or "POIZON:{}".format(sid),
                "url": url,
                "size_pattern": m_["size"],
                "stock_keyword": "",
                "enabled": True,
                "image_url": "",
                "poizon_sku_id": sid,
            })
        linked.append({"sku_id": sid, "size": m_["size"], "color": m_["color"], "name": label,
                       "cost_price": links[sid].get("cost_price", 0)})

    save_poizon_links(links)
    save_json(PRODUCTS_FILE, products)

    return jsonify({"ok": True, "linked": linked, "count": len(linked),
                    "page_title": page_title, "product_code": product_code})


# ---------- 仕入値更新API ----------
@app.route("/api/poizon/update_cost", methods=["POST"])
@login_required
def poizon_update_cost_api():
    """仕入値を更新（UIインライン編集用）。"""
    if not is_logged_in():
        return jsonify({"error": "ログインが必要です"}), 401
    sku_id = (request.form.get("sku_id") or "").strip()
    cost_raw = (request.form.get("cost_price") or "").strip()
    if not sku_id:
        return jsonify({"error": "sku_idが必要"}), 400
    try:
        cost = int(float(cost_raw))
    except ValueError:
        return jsonify({"error": "仕入値は数値で入力"}), 400
    if cost < 0:
        return jsonify({"error": "仕入値は0以上"}), 400
    links = load_poizon_links()
    if sku_id not in links:
        return jsonify({"error": "未紐付けのSKUです"}), 400
    if cost > 0:
        links[sku_id]["cost_price"] = cost
    else:
        links[sku_id].pop("cost_price", None)
    save_poizon_links(links)
    return jsonify({"ok": True, "cost_price": cost})


# ---------- 自動価格調整API ----------
@app.route("/api/poizon/auto_adjust", methods=["POST"])
@login_required
def poizon_auto_adjust_api():
    """最小利益額を守りながら市場最低値へ自動調整。

    パラメータ:
        sku_ids: カンマ区切り（空なら仕入値・市場最低値のある全SKU）
        dry_run: "1" ならシミュレーションのみ（デフォルト1）
    ロジック:
        - 対象: 自価格 > 市場最低値（競争力を失っている）
        - 新価格 = 市場最低値
        - ガード: 新価格 - 仕入値 < min_profit ならスキップ
          （POIZON手数料5%+決済1%+作業1500円も考慮した実質利益で判定可: use_net=1）
    """
    if not is_logged_in():
        return jsonify({"error": "ログインが必要です"}), 401

    sku_ids_raw = (request.form.get("sku_ids") or "").strip()
    dry_run = (request.form.get("dry_run") or "1") == "1"
    use_net = (request.form.get("use_net") or "0") == "1"

    config = load_config()
    min_profit = int(config.get("auto_adjust_min_profit") or 0)
    app_key = (config.get("poizon_api_id") or "").strip()
    app_secret = (config.get("poizon_api_key") or "").strip()
    if not app_key or not app_secret:
        return jsonify({"error": "POIZON API設定が未設定"}), 400

    # 出品一覧＋市場価格取得
    from poizon_api import get_active_listings, fetch_market_prices_batch, update_listing_price
    result = get_active_listings(config)
    if isinstance(result, dict) and "error" in result:
        return jsonify({"error": result.get("error", "")}), 200
    all_sku_ids = [i.get("skuId") for i in result if i.get("skuId")]
    price_map = fetch_market_prices_batch(all_sku_ids, app_key, app_secret) if all_sku_ids else {}
    links = load_poizon_links()

    targets_specified = [s.strip() for s in sku_ids_raw.split(",") if s.strip()] if sku_ids_raw else None
    plan = []
    for item in result:
        sku_id = str(item.get("skuId", ""))
        if targets_specified and sku_id not in targets_specified:
            continue
        link = links.get(sku_id, {})
        cost = int(link.get("cost_price") or 0)
        market_min = int((price_map.get(sku_id, {}) or {}).get("min_price") or 0)
        my_price = int(item.get("price") or 0)
        if not (cost and market_min and my_price and my_price > market_min):
            continue
        new_price = market_min
        # 実質利益（POIZON手数料5%+決済1%+作業1,500円相当を差し引く）
        if use_net:
            net_profit = int(new_price * 0.94 - 1500 - cost)
        else:
            net_profit = new_price - cost
        entry = {"sku_id": sku_id, "title": (item.get("spuTitle") or "")[:40],
                 "current": my_price, "new": new_price, "cost": cost,
                 "profit": net_profit, "action": "adjust"}
        if net_profit < min_profit:
            entry["action"] = "skip_min_profit"
        plan.append(entry)

    if dry_run:
        return jsonify({"ok": True, "dry_run": True, "plan": plan,
                        "adjustable": sum(1 for p in plan if p["action"] == "adjust")})

    # 実行（1秒間隔）
    results = []
    for entry in plan:
        if entry["action"] != "adjust":
            results.append({**entry, "result": "skipped"})
            continue
        item = next((i for i in result if str(i.get("skuId", "")) == entry["sku_id"]), None)
        if not item:
            continue
        import time as _time
        upd = update_listing_price(app_key, app_secret,
                                   item.get("sellerBiddingNo", ""),
                                   int(item.get("globalSkuId", 0)), entry["new"])
        ok = isinstance(upd, dict) and upd.get("ok")
        results.append({**entry, "result": "ok" if ok else "fail",
                        "detail": (upd or {}).get("error", "") if not ok else ""})
        _time.sleep(1.0)

    return jsonify({"ok": True, "dry_run": False, "results": results,
                    "adjusted": sum(1 for r in results if r["result"] == "ok"),
                    "failed": sum(1 for r in results if r["result"] == "fail")})


# ---------- main ----------
def main():
    config = load_config()
    port = int(config.get("port", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
