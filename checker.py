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
STATE_HISTORY_FILE = BASE_DIR / "state_history.json"

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


def append_history(product_id, name, prev_state, new_state, detail):
    """状態変化履歴を state_history.json に追記（最新1000件まで）。"""
    try:
        history = load_json(STATE_HISTORY_FILE, [])
        history.append({
            "product_id": product_id,
            "name": name,
            "prev_state": prev_state or "",
            "new_state": new_state,
            "detail": detail,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        # 最新1000件まで保持
        if len(history) > 1000:
            history = history[-1000:]
        save_json(STATE_HISTORY_FILE, history)
    except Exception as e:
        print("  [!] 履歴保存エラー: {}".format(e))


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


def fetch_html_auto(url):
    """HTML を取得する。requests → curl_cffi の順でフォールバック。

    requests が弾かれた場合(Akamai/Cloudflare等)、curl_cffi の
    impersonate=chrome で再試行する。
    """
    # 1st: 通常の requests
    try:
        return fetch_html(url)
    except Exception as req_err:
        print("    -> requests failed, trying curl_cffi...")
        # 2nd: curl_cffi (TLS指紋偽装)
        try:
            from curl_cffi import requests as cffi_requests
            r = cffi_requests.get(url, impersonate="chrome", timeout=30)
            r.raise_for_status()
            return r.text
        except ImportError:
            raise req_err  # curl_cffi未インストールなら元のエラー
        except Exception:
            raise


# ==================== 汎用在庫判定エンジン ====================

# 共通の売切れキーワード（日本のECサイトで広く使われる）
SOLDOUT_KEYWORDS = (
    "在庫切れ", "在庫なし", "品切れ", "売切れ", "売り切れ",
    "販売終了", "販売を終了しました", "販売休止", "販売停止",
    "入荷時期未定", "入荷未定", "一時的に在庫切れ",
    "予定数の販売を終了しました",
    "在庫がございません", "ご注文いただけません",
    "out of stock", "sold out", "unavailable",
)

# 共通の在庫ありキーワード
INSTOCK_KEYWORDS = (
    "在庫あり", "在庫有り", "在庫しています",
    "in stock", "available",
)


def check_by_keywords(html, stock_keyword="", size_pattern=""):
    """キーワードベースの汎用在庫判定。

    1. size_pattern があればそれを含む要素付近を探す
    2. 売切れキーワードがあれば SOLD_OUT
    3. 在庫ありキーワード or ユーザー指定キーワードがあれば IN_STOCK

    戻り値: (state, detail) or None（判定できなかった場合）
    """
    # サイズ指定がある場合: そのサイズ周辺のテキストを抽出
    search_text = html
    if size_pattern:
        # <option> タグ内を探す（Webike方式）
        pat = re.compile(r"<option\b[^>]*>(.*?)</option>", re.IGNORECASE | re.DOTALL)
        target = None
        for m in pat.finditer(html):
            text = re.sub(r"\s+", " ", m.group(1)).strip()
            if size_pattern in text:
                target = text
                break
        if target:
            # option内でキーワード判定
            for kw in SOLDOUT_KEYWORDS:
                if kw in target:
                    return SOLD_OUT, target
            if stock_keyword and stock_keyword in target:
                return IN_STOCK, target
            for kw in INSTOCK_KEYWORDS:
                if kw in target:
                    return IN_STOCK, target
            return SOLD_OUT, target  # option見つかったがキーワード無し→売切れ扱い
        # option無しはsize_pattern周辺のテキストで判定
        idx = html.find(size_pattern)
        if idx >= 0:
            start = max(0, idx - 500)
            end = min(len(html), idx + 500)
            search_text = html[start:end]

    # テキスト化（タグ除去）
    text = re.sub(r"<[^>]+>", " ", search_text)
    text = re.sub(r"\s+", " ", text).strip()

    # 売切れキーワード
    for kw in SOLDOUT_KEYWORDS:
        if kw in text:
            return SOLD_OUT, kw

    # 在庫ありキーワード
    for kw in INSTOCK_KEYWORDS:
        if kw in text:
            return IN_STOCK, kw

    # ユーザー指定キーワード
    if stock_keyword and stock_keyword in text:
        return IN_STOCK, stock_keyword

    return None  # 判定できず


def check_by_glm(html, product, glm_api_key):
    """GLM API でHTMLを解析して在庫判定。

    HTML全体ではなく、商品名・価格・カートボタン周辺など
    重要な部分を抽出して投げる（トークン節約）。
    """
    # HTMLから重要部分を抽出（最大3000文字）
    # 商品名、カート周辺、在庫表示周辺
    important = []

    # <title> タグ
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        important.append("TITLE: " + m.group(1).strip()[:200])

    # 在庫・カート関連キーワード周辺
    for kw in ("在庫", "売切", "品切", "cart", " Cart", "カート", "購入", "販売", "stock", "sold", "salesInfo", "btnCart"):
        idx = html.lower().find(kw.lower())
        if idx >= 0:
            start = max(0, idx - 200)
            end = min(len(html), idx + 300)
            snippet = re.sub(r"<[^>]+>", " ", html[start:end])
            snippet = re.sub(r"\s+", " ", snippet).strip()[:300]
            important.append(snippet)
            if len(" ".join(important)) > 3000:
                break

    excerpt = "\n".join(important[:8])[:3000]

    # 重要部分が抽出できなくても、HTML全体の先頭部分をフォールバックとして使う
    if not excerpt.strip():
        # HTMLタグを除去した全文テキストの先頭3000文字
        all_text = re.sub(r"<[^>]+>", " ", html)
        all_text = re.sub(r"\s+", " ", all_text).strip()
        excerpt = all_text[:3000]
    if not excerpt.strip():
        return None, "GLM判定スキップ: HTMLにテキストが見つかりません"

    name = product.get("name", "")
    size = product.get("size_pattern", "")

    prompt = (
        "以下はECサイトの商品ページのHTML抜粋です。"
        "この商品の在庫状態を判定してください。\n\n"
        "商品名: {}\nサイズ: {}\n\n"
        "HTML抜粋:\n{}\n\n"
        "判定基準:\n"
        "- 「在庫あり」「購入可能」「カートに入る」等 → IN_STOCK\n"
        "- 「在庫切れ」「売切れ」「品切れ」「販売終了」「予定数の販売を終了」等 → SOLD_OUT\n"
        "- 「Access denied」「アクセス拒否」「ボット対策」「CAPTCHA」「Privacy」等、"
        "アクセス保護ページで商品情報が確認できない → UNKNOWN\n\n"
        "重要: 商品情報が確認できないだけではSOLD_OUTにしないこと。"
        "売切れと確定できる根拠がない限りUNKNOWNを返してください。\n\n"
        "回答は以下のJSON形式のみ（他のテキストは不要）:\n"
        '{{"state": "IN_STOCK" or "SOLD_OUT" or "UNKNOWN", "reason": "判定理由（日本語・簡潔に）"}}'
    ).format(name, size or "(指定なし)", excerpt)

    try:
        resp = requests.post(
            "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
            json={
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 2000,
            },
            headers={
                "Authorization": "Bearer {}".format(glm_api_key),
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None, "GLM API エラー: HTTP {}".format(resp.status_code)

        data = resp.json()
        content = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()

        if not content:
            finish = data.get("choices", [{}])[0].get("finish_reason", "")
            return None, "GLM応答が空（finish={}）".format(finish)

        # JSON部分を抽出（```json ... ``` で囲まれている場合も対応）
        json_match = re.search(r'\{[^}]*"state"[^}]*\}', content, re.S)
        if json_match:
            result = json.loads(json_match.group())
            state_str = result.get("state", "").upper()
            reason = result.get("reason", "")
            if "IN_STOCK" in state_str:
                return IN_STOCK, "GLM判定: {}".format(reason)
            elif "UNKNOWN" in state_str:
                return UNKNOWN, "GLM判定: {}".format(reason)
            elif "SOLD_OUT" in state_str or "SOLD" in state_str:
                # 誤検知ガード: 理由にアクセス保護系ワードがあればUNKNOWNに差し戻す
                guard_words = ("アクセス保護", "Access denied", "アクセス拒否", "ボット対策",
                               "CAPTCHA", "確認できない", "確認できません", "判定できない")
                if any(w in reason for w in guard_words):
                    return UNKNOWN, "GLM判定(保護ガード): {}".format(reason)
                return SOLD_OUT, "GLM判定: {}".format(reason)
            return None, "GLM判定不明: {}".format(content[:100])

        # JSONパース失敗 — 生テキストから推測
        low = content.lower()
        if "sold" in low or "売切" in content or "品切" in content or "在庫切" in content:
            return SOLD_OUT, "GLM判定(テキスト): {}".format(content[:100])
        if "in_stock" in low or "在庫あり" in content or "購入" in content:
            return IN_STOCK, "GLM判定(テキスト): {}".format(content[:100])

        return None, "GLM判定解析失敗: {}".format(content[:100])
    except Exception as e:
        return None, "GLM API通信エラー: {}".format(e)


def check_generic(url, product, config):
    """汎用在庫判定エンジン（任意のサイトに対応）。

    処理フロー:
    1. HTML取得: requests → curl_cffi (自動フォールバック)
    2. キーワード判定（コストゼロ・高速）
    3. ダメなら GLM API で判定（設定済みの場合）

    戻り値: (state, detail)
    """
    # 1. HTML取得
    try:
        html = fetch_html_auto(url)
    except Exception as e:
        print("  [!] Fetch failed: {}".format(e))
        return UNKNOWN, "取得失敗: {}".format(e)

    size_pattern = product.get("size_pattern", "")
    stock_keyword = product.get("stock_keyword", "")

    # 1.5 アクセス保護ページ検知（Akamai等の403ページをGLMに投げない）
    #    「Access denied」等が含まれる場合、商品ページではなくブロックページ。
    #    ※ブロック文言はページ末尾付近にあることもあるため全文スキャン（正規表現1回・軽量）
    if any(w in html for w in ("Access denied", "Access Denied", "アクセスが拒否",
                                "Too Many Requests", "unusual traffic")):
        return UNKNOWN, "アクセス保護ページ（ボット対策）: サーバーサイド取得不可"

    # 2. キーワード判定
    result = check_by_keywords(html, stock_keyword, size_pattern)
    if result:
        return result

    # 3. GLM API 判定（フォールバック）
    glm_key = (config.get("glm_api_key") or "").strip()
    if glm_key:
        print("    -> キーワード判定できず、GLM APIで判定中...")
        state, detail = check_by_glm(html, product, glm_key)
        if state:
            return state, detail
        return UNKNOWN, detail or "GLM判定でも判定できませんでした"

    # GLM未設定ならUNKNOWN
    return UNKNOWN, "キーワードで判定できず（設定画面でGLM API Keyを追加するとAI判定が有効になります）"


def _yodobashi_variants(html):
    """ヨドバシ専用: relatedSku から全バリエーション（サイズ・カラー）を抽出。

    ヨドバシはサイズ/カラーごとに別URL（別SKU）になっており、
    ページ内の JavaScript変数 relatedSku に全バリエーションのSKUが入っている。
    それらのリンクテキストからサイズ・カラーを抽出する。

    戻り値: {"name":..., "sizes":[...], "colors":...} または None
    """
    # relatedSku を取得
    sku_m = re.search(r"var relatedSku\s*=\s*'([^']+)'", html)
    if not sku_m:
        return None
    related_skus = set(sku_m.group(1).split(","))

    # 現在の商品名からベース名を抽出
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = title_m.group(1).strip() if title_m else ""
    # "ヨドバシ.com - " を削除、末尾の "通販..." を削除
    name = re.sub(r"^ヨドバシ\.com\s*-\s*", "", title)
    name = re.sub(r"\s*通販.*$", "", name).strip()
    # 現在のサイズ・カラー部分を削除してベース名を作る
    base_name = re.sub(r"\s+(?:US\d+|リリー|Ivory|Black|Glacier|ブラック|ホワイト).*", "", name)
    base_name = re.sub(r"\s+\d+[A-Z]{2}\d+.*$", "", base_name).strip()

    # relatedSkuに含まれるリンクのテキストからサイズ・カラーを抽出
    sizes = []
    colors = []
    seen_sizes = set()
    seen_colors = set()

    for m in re.finditer(r'href="/product/(\d+)/"[^>]*>(.*?)</a>', html, re.S):
        sku = m.group(1)
        if sku not in related_skus:
            continue
        text = re.sub(r"<[^>]+>", " ", m.group(2))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        # サイズを抽出（US数字（cm数字）形式）
        size_m = re.search(r'(US\s*\d+(?:\.\d+)?)\s*[（(]\s*(\d+(?:\.\d+)?)\s*cm\s*[）)]', text)
        if size_m:
            size_label = "US{}({}cm)".format(size_m.group(1).replace("US", "").strip(),
                                              size_m.group(2))
            if size_label not in seen_sizes:
                seen_sizes.add(size_label)
                sizes.append(size_label)
        else:
            # US数字のみ
            size_m2 = re.search(r'(US\s*\d+(?:\.\d+)?)', text)
            if size_m2:
                sl = size_m2.group(1).strip()
                if sl not in seen_sizes:
                    seen_sizes.add(sl)
                    sizes.append(sl)

        # カラーを抽出（スラッシュ区切り or 日本語）
        # 商品名の一部から色を推定: "リリー/ライム" "Ivory/Peony" "Black/Ivory" 等
        color_m = re.search(r'((?:[A-Za-z]+/[A-Za-z]+|[\u3040-\u309F\u30A0-\u30FF]+/[\u3040-\u309F\u30A0-\u30FF]+))', text)
        if color_m:
            color_label = color_m.group(1)
            if color_label not in seen_colors:
                seen_colors.add(color_label)
                colors.append(color_label)

    # サイズを番号順にソート
    def _size_key(s):
        m = re.search(r'(\d+(?:\.\d+)?)', s)
        return float(m.group(1)) if m else 0
    sizes.sort(key=_size_key)

    if sizes or colors:
        print("    [バリアント] ヨドバシ: sizes={0}, colors={1}".format(sizes[:5], colors[:5]))
        return {"name": base_name, "sizes": sizes[:30], "colors": colors[:20]}

    return None


def fetch_variants_fast(url, glm_api_key=""):
    """高速サイズ/カラー抽出。サイト別に最適化。

    ヨドバシ: relatedSkuから全バリエーションを抽出
    その他: 正規表現優先、ダメならGLMフォールバック

    戻り値: {"name":..., "sizes":[...], "colors":[...]} または {"error":"..."}
    """
    try:
        html = fetch_html_auto(url)
    except Exception as e:
        return {"error": "ページ取得失敗: {}".format(e)}

    # --- ヨドバシ専用: relatedSku からバリエーション抽出 ---
    if "yodobashi.com" in url:
        result = _yodobashi_variants(html)
        if result:
            return result

    # --- 正規表現でサイズ抽出（瞬時） ---
    sizes = []

    # パターン1: US数字（cm数字）
    for m in re.finditer(r'US\s*(\d+(?:\.\d+)?)\s*[（(]\s*(\d+(?:\.\d+)?)\s*cm\s*[）)]', html):
        label = "US{}({}cm)".format(m.group(1), m.group(2))
        if label not in sizes:
            sizes.append(label)

    # パターン1b: US数字（カッコ内にcmなし）
    if not sizes:
        for m in re.finditer(r'US\s*(\d+(?:\.\d+)?)\s*[（(]([^)）]*?)[）)]', html):
            label = "US{}({})".format(m.group(1), m.group(2))
            if label not in sizes and len(label) < 30:
                sizes.append(label)

    # パターン2: 数字cm
    if not sizes:
        for m in re.finditer(r'(\d{2}(?:\.\d+)?)\s*cm', html):
            label = "{}cm".format(m.group(1))
            if label not in sizes:
                sizes.append(label)

    # パターン3: S/M/L/XL/XXL
    if not sizes:
        for m in re.finditer(r'\b(X{0,2}[SML])\b', html):
            label = m.group(1)
            if label not in sizes:
                sizes.append(label)

    # パターン4: サイズ：XX
    if not sizes:
        for m in re.finditer(r'サイズ[：:]\s*([^\s<<]+)', html):
            sizes.append(m.group(1))

    # パターン5: <option>タグ内
    if not sizes:
        for m in re.finditer(r'<option[^>]*>([^<]+)</option>', html, re.I):
            text = m.group(1).strip()
            if text and text not in ("選択してください", "サイズを選択", "---"):
                sizes.append(text)

    # --- 2. 正規表現でカラー抽出 ---
    colors = []
    color_match = re.search(r'カラー[：:]\s*(.+?)(?:<[^>]+>|</\w+>|\Z)', html, re.S | re.I)
    if color_match:
        raw = re.sub(r'<[^>]+>', '', color_match.group(1))
        colors = [c.strip() for c in re.split(r'[/／・、,]', raw) if c.strip() and 1 <= len(c.strip()) <= 20]
        colors = colors[:20]  # 上限

    # 商品名
    name = ""
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
    if m:
        name = re.sub(r'\s*-\s*.*$', '', m.group(1).strip())[:100]
        name = re.sub(r'\s*\|.*$', '', name)

    # --- 3. 正規表現で取れたら即返す ---
    if sizes or colors:
        print("    [バリアント] 正規表現で取得: sizes={0}, colors={1}".format(sizes[:5], colors[:5]))
        return {"name": name, "sizes": sizes[:30], "colors": colors[:20]}

    # --- 4. 取れなければGLMフォールバック ---
    if not glm_api_key:
        return {"error": "サイズ/カラーが自動検出できませんでした（手入力でサイズを指定してください）"}

    # GLMには最小限のテキストだけ投げる
    all_text = re.sub(r"<[^>]+>", " ", html)
    all_text = re.sub(r"\s+", " ", all_text).strip()
    excerpt = all_text[:1500]

    prompt = 'HTMLからサイズとカラーをJSONで抽出(最大20件):\n{}\n{{"sizes":[],"colors":[]}}'.format(excerpt[:500])

    try:
        resp = requests.post(
            "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
            json={
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 2000,
            },
            headers={
                "Authorization": "Bearer {}".format(glm_api_key),
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return {"error": "GLM API エラー: HTTP {}".format(resp.status_code)}

        data = resp.json()
        content = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not content:
            return {"error": "GLM応答が空（サイズ抽出失敗）"}

        json_match = re.search(r'```json\s*(\{.*?\})\s*```', content, re.S)
        if not json_match:
            json_match = re.search(r'\{[^{}]*"sizes"[^{}]*\}', content, re.S)
        if not json_match:
            json_match = re.search(r'\{.*?"sizes".*?\}', content, re.S)

        if json_match:
            raw_json = json_match.group(1) if json_match.lastindex else json_match.group(0)
            try:
                result = json.loads(raw_json)
            except json.JSONDecodeError:
                cleaned = raw_json.replace("'", '"').replace("\n", "")
                result = json.loads(cleaned)

            sizes = result.get("sizes", [])
            colors = result.get("colors", [])
            if not isinstance(sizes, list): sizes = []
            if not isinstance(colors, list): colors = []
            if not sizes and not colors:
                return {"error": "サイズ/カラーが見つかりませんでした"}
            print("    [バリアント] GLMで取得: sizes={0}, colors={1}".format(sizes[:5], colors[:5]))
            return {"name": name or result.get("name", ""), "sizes": sizes[:30], "colors": colors[:20]}

        return {"error": "GLM応答の解析に失敗: {}".format(content[:200])}
    except Exception as e:
        return {"error": "GLM API通信エラー: {}".format(e)}


def check_product(product, config=None):
    """
    戻り値: (state, detail)
      state: IN_STOCK / SOLD_OUT / UNKNOWN
      detail: 在庫情報テキスト

    サイト別ルーティング:
    - 楽天市場 → check_rakuten (Playwright・JSON)
    - Yahoo!ショッピング → check_yahoo (JSON)
    - その他すべて → check_generic (汎用エンジン)
    """
    url = product.get("url", "")
    size_pattern = product.get("size_pattern", "")
    stock_keyword = product.get("stock_keyword", "")
    if config is None:
        config = load_json(CONFIG_FILE, {})

    # 楽天市場: Playwright でレンダリング後のJSONを読む（requestsでは取れない）
    if "rakuten.co.jp" in url:
        return check_rakuten(url, size_pattern)

    # Yahoo!ショッピング: JSON の choiceName + stockText で判定
    if "yahoo.co.jp" in url:
        if not size_pattern:
            return UNKNOWN, "サイズパターン未設定"
        try:
            html = fetch_html_auto(url)
        except Exception as e:
            return UNKNOWN, "取得失敗: {}".format(e)
        return check_yahoo(html, size_pattern)

    # その他すべてのサイト: 汎用判定エンジン
    return check_generic(url, product, config)


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

        new_state, detail = check_product(p, config)
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

        # 状態変化履歴を記録（初回・変化時すべて）
        if prev_state is None or prev_state != new_state:
            append_history(p.get("id"), name, prev_state, new_state, detail)

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
