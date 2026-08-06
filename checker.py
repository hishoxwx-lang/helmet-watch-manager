# -*- coding: utf-8 -*-
"""
checker.py - Webike / Yahoo! / 楽天市場 の商品在庫監視エンジン

products.json を読み、有効な全商品をチェック。
各商品:
  - サイト判定で監視方法を切替:
    * Webike 等 : URL を取得(charset 自動判定)し HTML の <option> タグを正規表現で検索
    * Yahoo!    : __NEXT_DATA__ JSON の choiceName + stockText で判定
    * 楽天市場   : Playwright(ヘッドレスChromium)でレンダリング後の埋め込みJSONを読み
                  variantId ごとの在庫(stockCondition/quantity)で判定
  - state.json から前回状態を読み、変化時のみ Discord 通知

Playwright は楽天商品があるときだけ遅延 import される。
未インストールでも Webike/Yahoo! は動く(フェイルソフト)。
"""
import json
import re
import sys
import datetime
from pathlib import Path

# 標準出力をUTF-8に（Windowsで親プロセスがUTF-8で受信するため）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except ImportError:
    print("[ERROR] 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"

IN_STOCK = "IN_STOCK"
SOLD_OUT = "SOLD_OUT"
UNKNOWN = "UNKNOWN"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def detect_encoding(content, resp):
    """charset を自動判定する。Webike は Shift_JIS。"""
    # 1) HTTP ヘッダの charset
    enc = (resp.encoding or "").lower()
    if enc and enc not in ("iso-8859-1",):  # requests のデフォルト fallback は無視
        return enc
    # 2) HTML メタタグ
    head = content[:2048].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?\s*([a-z0-9_\-]+)', head)
    if m:
        return m.group(1)
    return "utf-8"


def fetch_html(url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    content = resp.content
    encoding = detect_encoding(content, resp)
    # Webike 等の Shift_JIS 対策。代表的なエイリアスを正規化。
    encoding = encoding.replace("shift-jis", "shift_jis").replace("x-sjis", "shift_jis")
    try:
        html = content.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        html = content.decode("utf-8", errors="replace")
    return html


def check_yahoo(html, size):
    """Yahoo!ショッピング: JSON(choiceName + stockText) でサイズの在庫を判定"""
    m = re.search(
        r'"choiceName":"' + re.escape(size) + r'".*?"stockText":"([^"]*)"',
        html, re.S,
    )
    if not m:
        return UNKNOWN, "{} の在庫情報が見つかりません".format(size)
    stock_text = m.group(1)
    detail = "{}: {}".format(size, stock_text or "(在庫あり)")
    soldout = ("在庫なし", "品切れ", "売切れ", "販売終了", "販売停止")
    if any(kw in stock_text for kw in soldout):
        return SOLD_OUT, detail
    return IN_STOCK, detail


def fetch_og_image(url):
    """商品ページの og:image を取得（無ければ空文字）"""
    try:
        html = fetch_html(url)
    except Exception:
        return ""
    for pat in (
        r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def fetch_yahoo_variants(url):
    """Yahoo!ショッピング商品のサイズ・カラー一覧を取得。
    戻り値: {"sizes": [...], "colors": [...]} または {"error": "..."}
    """
    try:
        html = fetch_html(url)
    except Exception as e:
        return {"error": "取得失敗: {}".format(e)}
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S,
    )
    if not m:
        return {"error": "Yahoo!商品ページのデータが見つかりません"}
    try:
        data = json.loads(m.group(1))
        item = data.get("props", {}).get("pageProps", {}).get("item", {})
        name = item.get("name", "")
        stock = item.get("stockTableTwoAxis", {})
        first = stock.get("firstOption", {})
        sizes = []
        colors = []
        for ch in first.get("choiceList", []):
            size = ch.get("choiceName")
            if size and size not in sizes:
                sizes.append(size)
            second = ch.get("secondOption", {})
            for sc in second.get("choiceList", []):
                color = sc.get("choiceName")
                if color and color not in colors:
                    colors.append(color)
        # 1軸目がダミー(―等)のみの場合、2軸目(カラー)を実バリエーションとして使う
        if sizes and all(s in ("―", "-", "", None) for s in sizes):
            sizes = colors
            colors = []
        if not sizes:
            return {"error": "サイズ選択肢が見つかりません"}
        return {"name": name, "sizes": sizes, "colors": colors}
    except Exception as e:
        return {"error": "データの解析に失敗: {}".format(e)}


# ==================== 楽天市場 (Playwright) ====================
def _playwright_import():
    """Playwright を遅延 import。未インストール時は ImportError。"""
    from playwright.sync_api import sync_playwright
    return sync_playwright


def _balance_end(text, start):
    """text[start] は '[' or '{'。対応する閉じ括弧のインデックスを返す(文字列内は無視)。"""
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    i = start
    in_str = False
    esc = False
    n = len(text)
    while i < n:
        ch = text[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _rakuten_parse(html):
    """楽天商品ページのレンダリング後HTMLから itemInfoSku 相当のデータを取り出す。
    戻り値(dict) または None:
      name           : 商品名
      selectors      : variantSelectors (サイズ/カラー軸)
      first_variant  : {"variantId":..., "selectorValues":[...]} (選択中バリアント)
      vid_stocks     : {variantId: {"stockCondition","quantity","deliveryMessage"}}
    """
    vs = html.find('"variantSelectors"')
    if vs < 0:
        return None
    script_start = html.rfind("<script", 0, vs)
    if script_start < 0:
        return None
    content_start = html.find(">", script_start) + 1
    content_end = html.find("</script>", vs)
    script_txt = html[content_start:content_end]
    fb = script_txt.find("{")
    if fb < 0:
        return None
    end = _balance_end(script_txt, fb)
    if end < 0:
        return None
    try:
        obj = json.loads(script_txt[fb:end + 1])
    except Exception:
        return None
    iis = obj.get("api", {}).get("data", {}).get("itemInfoSku", {})
    if not iis:
        return None

    name = iis.get("title", "") or ""
    selectors = iis.get("variantSelectors", []) or []

    first = {}
    sku_list = iis.get("sku", []) or []
    if sku_list and isinstance(sku_list[0], dict):
        e0 = sku_list[0]
        first = {
            "variantId": e0.get("variantId"),
            "selectorValues": e0.get("selectorValues") or [],
        }

    vid_stocks = {}
    pi = iis.get("purchaseInfo", {}) or {}
    for e in pi.get("sku", []) or []:
        vid = e.get("variantId")
        nps = e.get("newPurchaseSku") or {}
        if vid and nps:
            vid_stocks[vid] = {
                "stockCondition": nps.get("stockCondition"),
                "quantity": nps.get("quantity"),
                "deliveryMessage": nps.get("deliveryMessage", "") or "",
            }
    # fallback: newPurchaseSku が無ければ variantMappedInventories の quantity を使う
    if not vid_stocks:
        for e in pi.get("variantMappedInventories", []) or []:
            vid = e.get("sku")
            if vid:
                vid_stocks[vid] = {
                    "stockCondition": None,
                    "quantity": e.get("quantity"),
                    "deliveryMessage": "",
                }

    return {
        "name": name,
        "selectors": selectors,
        "first_variant": first,
        "vid_stocks": vid_stocks,
    }


def _render_html(url, wait_text=None, wait_ms=4000, engine="chromium", block_resources=True):
    """Playwright でページをレンダリングし HTML を返す。失敗時は例外を投げる。

    wait_text に文字列を指定すると、ページ内にその文字列が出現するまで待つ
    （例: 楽天は 'itemInfoSku'、ヨドバシは 'salesInfo'）。
    タイムアウト時は wait_ms 待ってから HTML を返す。

    engine: 'chromium' (デフォルト) または 'firefox'
    block_resources: True なら画像・CSS等をブロックして高速化。
      BOT対策が厳しいサイト(Akamai等)では False にすることで
      リソースブロックのbot特徴を隠す。
    """
    sync_playwright = _playwright_import()
    with sync_playwright() as p:
        launcher = p.chromium if engine == "chromium" else p.firefox
        launch_kwargs = {"headless": True}
        if engine == "chromium":
            launch_kwargs["args"] = [
                "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--blink-settings=imagesEnabled=false",
            ]
        browser = launcher.launch(**launch_kwargs)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="ja-JP")
        page = ctx.new_page()
        if block_resources:
            def _block(route):
                if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", _block)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if wait_text:
            try:
                page.wait_for_function(
                    "document.documentElement.outerHTML.includes('{}')".format(wait_text),
                    timeout=10000,
                )
            except Exception:
                page.wait_for_timeout(wait_ms)
        else:
            page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
    return html


def _rakuten_render(url, wait_ms=4000):
    """楽天ページ用ラッパ（後方互換）。"""
    return _render_html(url, wait_text="itemInfoSku", wait_ms=wait_ms)


def fetch_rakuten_variants(url):
    """楽天市場商品のサイズ・カラー一覧を取得。
    戻り値: {"name":..., "sizes":[...], "colors":[...]} または {"error":"..."}
    ※ variantSelectors から全サイズラベルを取得する。variantId とサイズの自動対応は
       楽天側が埋め込まないため取得できない(選択中バリアントのみ)。
    """
    try:
        html = _rakuten_render(url)
    except ImportError:
        return {"error": "Playwrightが未インストール (pip install playwright && playwright install chromium)"}
    except Exception as e:
        return {"error": "取得失敗: {}".format(e)}
    data = _rakuten_parse(html)
    if not data:
        return {"error": "楽天商品ページのデータが見つかりません"}
    sizes = []
    colors = []
    for axis in data.get("selectors") or []:
        vals = [v.get("value") for v in axis.get("values", []) if v.get("value")]
        if not sizes:
            sizes = vals
        elif not colors:
            colors = vals
    name = data.get("name", "")
    if not sizes:
        return {"error": "サイズ選択肢が見つかりません"}
    return {"name": name, "sizes": sizes, "colors": colors}


def check_rakuten(url, size):
    """楽天市場: URLの variantId に対応するサイズの在庫を判定。
    戻り値: (state, detail)  state: IN_STOCK / SOLD_OUT / UNKNOWN
    """
    from urllib.parse import urlparse, parse_qs
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        qs = {}
    url_vid = None
    for k in ("variantId", "variantid", "variant_id"):
        if qs.get(k):
            url_vid = qs[k][0]
            break

    try:
        html = _rakuten_render(url)
    except ImportError:
        return UNKNOWN, "Playwrightが未インストール（楽天監視には必要）"
    except Exception as e:
        return UNKNOWN, "楽天ページ取得失敗: {}".format(e)

    data = _rakuten_parse(html)
    if not data:
        return UNKNOWN, "楽天商品データの解析に失敗"

    vid_stocks = data.get("vid_stocks", {})
    first = data.get("first_variant", {})

    target_vid = url_vid
    if not target_vid and first.get("variantId"):
        target_vid = first.get("variantId")
    if (not target_vid or target_vid not in vid_stocks) and len(vid_stocks) == 1:
        target_vid = list(vid_stocks.keys())[0]

    if not target_vid or target_vid not in vid_stocks:
        return UNKNOWN, "variantIdの在庫が特定できません（URLに ?variantId= を含めてください）"

    st = vid_stocks[target_vid] or {}
    cond = (st.get("stockCondition") or "").lower()
    qty = st.get("quantity")
    delivery = st.get("deliveryMessage") or ""
    size_label = size or ""
    if not size_label and first.get("selectorValues"):
        size_label = first["selectorValues"][0]
    if not size_label:
        size_label = target_vid

    detail = "{}: {}".format(size_label, delivery or cond or ("qty=" + str(qty)))
    if cond == "sold-out" or qty == 0:
        return SOLD_OUT, detail
    return IN_STOCK, detail


def check_yodobashi(url, stock_keyword=""):
    """ヨドバシカメラ: div.salesInfo のテキストで在庫を判定。

    ヨドバシはAkamaiのBOT対策で requests/Playwright を弾くため、
    curl_cffi (TLS指紋偽装ライブラリ) でHTMLを取得する。
    サイズ選択は不要（URL=商品単位）。

    戻り値: (state, detail)
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return UNKNOWN, "curl_cffiが未インストール（pip install curl_cffi）"

    try:
        r = cffi_requests.get(url, impersonate="chrome", timeout=30)
        html = r.text
    except Exception as e:
        return UNKNOWN, "ヨドバシページ取得失敗: {}".format(e)

    # div.salesInfo を正規表現で抽出（class名に salesInfo を含む div）
    m = re.search(
        r'<div[^>]*class="[^"]*salesInfo[^"]*"[^>]*>(.*?)</div>',
        html, re.S | re.IGNORECASE,
    )
    if not m:
        # フォールバック: id="salesInfo"
        m = re.search(
            r'<div[^>]*id="[^"]*salesInfo[^"]*"[^>]*>(.*?)</div>',
            html, re.S | re.IGNORECASE,
        )
    if not m:
        return UNKNOWN, "salesInfo が見つかりません（ページ構造変更の可能性）"

    raw = m.group(1)
    # HTMLタグを除去してテキスト化
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()

    # 売切れキーワード（複数パターンに対応）
    soldout_keywords = (
        "予定数の販売を終了しました",
        "在庫切れ",
        "品切れ",
        "入荷時期未定",
        "販売終了",
        "販売を終了しました",
    )
    for kw in soldout_keywords:
        if kw in text:
            return SOLD_OUT, text

    # ユーザー指定の在庫キーワードがあれば、それが含まれていれば在庫あり
    if stock_keyword and stock_keyword in text:
        return IN_STOCK, text

    # salesInfo があり、売切れキーワードがなければ在庫ありとみなす
    if text:
        return IN_STOCK, text

    return UNKNOWN, "salesInfo のテキストが空です"


def check_product(product):
    """
    戻り値: (state, detail)
      state: IN_STOCK / SOLD_OUT / UNKNOWN
      detail: option テキスト（サイズ + 在庫情報）
    """
    url = product.get("url", "")
    size_pattern = product.get("size_pattern", "")
    stock_keyword = product.get("stock_keyword", "在庫")

    # 楽天市場: Playwright でレンダリング後のJSONを読む（requestsでは取れない）
    if "rakuten.co.jp" in url:
        return check_rakuten(url, size_pattern)

    # ヨドバシカメラ: Akamai BOT対策のため Playwright でレンダリング
    if "yodobashi.com" in url:
        return check_yodobashi(url, stock_keyword)

    try:
        html = fetch_html(url)
    except Exception as e:
        print("  [!] Fetch failed: {}".format(e))
        return UNKNOWN, "取得失敗: {}".format(e)

    # Yahoo!ショッピング: JSON の choiceName + stockText で判定
    if "yahoo.co.jp" in url:
        if not size_pattern:
            return UNKNOWN, "サイズパターン未設定"
        return check_yahoo(html, size_pattern)

    # Webike等: <option> タグでサイズ別在庫判定
    if not size_pattern:
        return UNKNOWN, "サイズパターン未設定"

    # 指定サイズパターンを含む <option>...</option> を検索
    pat = re.compile(
        r"<option\b[^>]*>(.*?)</option>",
        re.IGNORECASE | re.DOTALL,
    )
    target = None
    for m in pat.finditer(html):
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        if size_pattern in text:
            target = text
            break

    if not target:
        return SOLD_OUT, "{} の option が見つかりません".format(size_pattern)

    if stock_keyword and stock_keyword in target:
        return IN_STOCK, target
    return SOLD_OUT, target


def send_discord(webhook_url, content):
    if not webhook_url:
        return False
    try:
        payload = {"content": content}
        r = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10,
        )
        ok = 200 <= r.status_code < 300
        if not ok:
            print("  [!] Discord error {} : {}".format(r.status_code, r.text[:200]))
        return ok
    except Exception as e:
        print("  [!] Discord notify failed: {}".format(e))
        return False


def notify_poizon_delist(product, config, detail):
    """売切れ検知時にPOIZON出品取り下げ窓口(/webhook/oos)へPOST。

    商品に poizon_sku_id が設定されている場合のみ動作(未設定なら何もしない)。
    仕入元が売切れたらPOIZON出品を即座に取り下げる連動。
    詳細は opencodetest/docs/STOCK_DELIST_DESIGN.md 参照。
    """
    sku_id = str(product.get("poizon_sku_id") or "").strip()
    url = (config.get("poizon_delist_url") or "").strip()
    token = (config.get("poizon_delist_token") or "").strip()
    if not sku_id:
        print("    -> (poizon_sku_id 未設定: POIZON連動スキップ)")
        return False
    if not url or not token:
        print("    -> (poizon_delist_url/token 未設定: POIZON連動スキップ)")
        return False
    try:
        r = requests.post(
            url,
            json={"skuId": sku_id, "event": "sold_out",
                  "url": product.get("url", ""), "source": "stock_watch"},
            headers={"X-Webhook-Token": token, "Content-Type": "application/json"},
            timeout=20,
        )
        ok = 200 <= r.status_code < 300
        msg = ""
        try:
            msg = r.json().get("message", "")
        except Exception:
            pass
        print("    -> POIZON delist {}: HTTP {} {}".format(
            "OK" if ok else "FAIL", r.status_code, msg))
        return ok
    except Exception as e:
        print("    -> POIZON delist error: {}".format(e))
        return False


def state_label(state):
    return {IN_STOCK: "在庫あり", SOLD_OUT: "売切れ", UNKNOWN: "未確認"}.get(state, state)


def main():
    products = load_json(PRODUCTS_FILE, [])
    config = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {})
    webhook = config.get("discord_webhook_url", "")

    print("=== checker start {} ===".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("products: {}  (webhook: {})".format(len(products), "set" if webhook else "NOT set"))

    changed = False
    for p in products:
        if not p.get("enabled", True):
            print("- [skip] {} (disabled)".format(p.get("name")))
            continue

        pid = str(p.get("id"))
        name = p.get("name", "?")
        url = p.get("url", "")
        print("- checking: {}".format(name))

        new_state, detail = check_product(p)
        prev = state.get(pid, {})
        prev_state = prev.get("state")

        print("    state: {} / detail: {}".format(state_label(new_state), detail))

        # 通知判定
        content = None
        if prev_state is None:
            # 初回
            content = "ℹ️ 監視開始: {name}\n状態: {st} ({detail})\n{url}".format(
                name=name, st=state_label(new_state), detail=detail, url=url
            )
        elif prev_state != new_state:
            if prev_state == IN_STOCK and new_state == SOLD_OUT:
                content = "🚨 売り切れました: {name}\n{detail}\n{url}".format(
                    name=name, detail=detail, url=url
                )
            elif new_state == IN_STOCK:
                content = "🎉 再入荷しました: {name}\n{detail}\n{url}".format(
                    name=name, detail=detail, url=url
                )
            else:
                content = "ℹ️ 状態変化: {name}\n{prev} → {st}\n{detail}\n{url}".format(
                    name=name,
                    prev=state_label(prev_state),
                    st=state_label(new_state),
                    detail=detail,
                    url=url,
                )

        if content:
            if webhook:
                send_discord(webhook, content)
                print("    -> Discord notified")
            else:
                print("    -> (Discord webhook not configured, skip notify)")

        # 売切れ時のみ POIZON出品取り下げ連動(在庫通知くん→POIZON API)
        if prev_state == IN_STOCK and new_state == SOLD_OUT:
            notify_poizon_delist(p, config, detail)

        state[pid] = {
            "name": name,
            "state": new_state,
            "detail": detail,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        changed = True

    if changed:
        save_json(STATE_FILE, state)
    print("=== checker done ===")


if __name__ == "__main__":
    main()
