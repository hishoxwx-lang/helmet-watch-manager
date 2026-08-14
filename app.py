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
    return render_template(
        "settings.html",
        webhook=webhook, webhook_masked=webhook_masked,
        poizon_url=poizon_url, poizon_token=poizon_token,
        poizon_token_masked=poizon_token_masked,
        glm_key=glm_key, glm_key_masked=glm_key_masked,
        poizon_api_id=poizon_api_id, poizon_api_key=poizon_api_key,
        poizon_api_id_masked=poizon_api_id_masked,
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
    """GitHub から最新版をDLして自動再起動（ユーザーデータは保持）"""
    import urllib.request as _urlreq
    import threading as _th
    import os as _os
    base = "https://raw.githubusercontent.com/hishoxwx-lang/helmet-watch-manager/main/"
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
            _urlreq.urlretrieve(base + rel, tmp)
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
            "linked": bool(link_info.get("url")),
            "monitoring": link_info.get("enabled", False),
            # 在庫状態（products.jsonのpoizon_sku_id経由でstate.jsonから取得）
            "stock_state": "",
            "stock_detail": "",
            "stock_updated": "",
        })

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

    links[sku_id] = {
        "url": url,
        "name": name,
        "enabled": enabled,
    }
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


# ---------- main ----------
def main():
    config = load_config()
    port = int(config.get("port", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
