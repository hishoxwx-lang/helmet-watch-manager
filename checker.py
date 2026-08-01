# -*- coding: utf-8 -*-
"""
checker.py - Webike 等の商品在庫監視エンジン

products.json を読み、有効な全商品をチェック。
各商品:
  - URL を取得（charset を自動判定し Shift_JIS の場合はデコード）
  - HTML から「サイズパターン」を含む <option> タグを正規表現で検索
  - option テキストに「在庫キーワード」が含まれれば IN_STOCK、無ければ SOLD_OUT、option 無しも SOLD_OUT
  - state.json から前回状態を読み、変化時のみ Discord 通知
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
        return {"sizes": sizes, "colors": colors}
    except Exception as e:
        return {"error": "データの解析に失敗: {}".format(e)}


def check_product(product):
    """
    戻り値: (state, detail)
      state: IN_STOCK / SOLD_OUT / UNKNOWN
      detail: option テキスト（サイズ + 在庫情報）
    """
    url = product.get("url", "")
    size_pattern = product.get("size_pattern", "")
    stock_keyword = product.get("stock_keyword", "在庫")

    try:
        html = fetch_html(url)
    except Exception as e:
        print("  [!] Fetch failed: {}".format(e))
        return UNKNOWN, "取得失敗: {}".format(e)

    if not size_pattern:
        return UNKNOWN, "サイズパターン未設定"

    # Yahoo!ショッピング: JSON の choiceName + stockText で判定
    if "yahoo.co.jp" in url:
        return check_yahoo(html, size_pattern)

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
