# -*- coding: utf-8 -*-
"""auto_link楽天対応の検証（実ページHTML + モックPOIZON出品一覧）。

検証内容:
  1. 実楽天ページ(EUC-JP)の fetch_html_auto → _rakuten_parse（EUC-JP復帰）
  2. auto_link: 品番 EE9033 抽出 + バリアント(サイズ/カラー/価格)抽出
  3. 照合: POIZON側 articleNumber=EE9033・cm/JP規格のSKUにヒットすること
  4. クエリ付きURL（?variantId=...）でも品番抽出が落ちないこと
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- モックPOIZON API: 実データ風の出品一覧（Five Ten ハイアングル EE9033）----
MOCK_LISTINGS = [
    {"skuId": "900000001", "spuTitle": "FIVE TEN ハイアングル EE9033 クライミングシューズ",
     "skuSaleProp": json.dumps([{"name": "Size", "value": "40"},
                                {"name": "Color", "value": "ホワイト"}]),
     "regionSalePvInfoList": [
         {"name": "サイズ", "localValue": "40", "skuManySizeInfos": [
             {"sizeKey": "JP", "sizeValue": "25"},
             {"sizeKey": "EU", "sizeValue": "40"},
         ]},
     ]},
    {"skuId": "900000002", "spuTitle": "FIVE TEN ハイアングル EE9033 クライミングシューズ",
     "skuSaleProp": json.dumps([{"name": "Size", "value": "42.5"},
                                {"name": "Color", "value": "ホワイト"}]),
     "regionSalePvInfoList": [
         {"name": "サイズ", "localValue": "42.5", "skuManySizeInfos": [
             {"sizeKey": "JP", "sizeValue": "26.5"},
             {"sizeKey": "EU", "sizeValue": "42.5"},
         ]},
     ]},
    {"skuId": "900000099", "spuTitle": "SHOEI Z8 ヘルメット",
     "skuSaleProp": json.dumps([{"name": "Size", "value": "M"}]),
     "regionSalePvInfoList": []},
]

import poizon_api
def _mock_get_active_listings(config):
    return list(MOCK_LISTINGS)
poizon_api.get_active_listings = _mock_get_active_listings

def _mock_sku_info_batch(sku_ids, app_key, app_secret):
    m = {"900000001": {"article_number": "EE9033", "brand_name": "FIVE TEN"},
         "900000002": {"article_number": "EE9033", "brand_name": "FIVE TEN"},
         "900000099": {"article_number": "Z8", "brand_name": "SHOEI"}}
    return {sid: m.get(str(sid), {}) for sid in sku_ids}
poizon_api.fetch_poizon_sku_info_batch = _mock_sku_info_batch

import app as appmod

# ユーザーデータを検証用一時ファイルへ退避
import tempfile, shutil
tmpdir = tempfile.mkdtemp()
for attr, fname in [("PRODUCTS_FILE", "products.json"), ("POIZON_LINKS_FILE", "poizon_links.json"),
                    ("CONFIG_FILE", "config.json"), ("STATE_FILE", "state.json")]:
    orig = getattr(appmod, attr, None)
    if isinstance(orig, str) and os.path.exists(orig):
        shutil.copy(orig, os.path.join(tmpdir, fname))
    setattr(appmod, attr, os.path.join(tmpdir, fname))
if not os.path.exists(appmod.CONFIG_FILE):
    open(appmod.CONFIG_FILE, "w").write(json.dumps({
        "password_hash": "x", "external_api_token": "テストトークン",
        "poizon_api_id": "", "poizon_api_key": ""}))
appmod.is_password_set = lambda: True

# fetch_html_auto を実ページ取得に向けてそのまま使う（requests経由）

client = appmod.app.test_client()
URL = "https://item.rakuten.co.jp/e-lodge-2/ften-ee9033/?s-id=top_normal_browsehist&xuseflg_ichiba01=10080005&variantId=ften-ee9033-wh-275"

results = []

# --- 検証1: 実ページ取得+EUC-JP復帰+_rakuten_parse ---
from checker import fetch_html_auto, _rakuten_parse
html = fetch_html_auto(URL)
ok1 = "EE9033" in html and "itemInfoSku" in html
results.append(("実ページ取得(EUC-JP復帰・itemInfoSku検出)", ok1))
data = _rakuten_parse(html)
ok2 = bool(data and data.get("variants") and len(data["variants"]) >= 15)
results.append(("itemInfoSkuからバリアント抽出(16サイズ想定)", ok2))
if data:
    print("  バリアント例:", data["variants"][:3])
    print("  バリアント数:", len(data["variants"]))

# --- 検証2: auto_link実行 ---
resp = client.post("/api/external/auto_link",
                   json={"url": URL, "token": "テストトークン"},
                   environ_base={"HTTP_X_API_TOKEN": "テストトークン"})
body = resp.get_json()
print("  auto_link応答:", json.dumps({k: body.get(k) for k in ("ok", "count", "product_code", "page_title", "error")}, ensure_ascii=False))
ok3 = body.get("ok") is True and body.get("product_code") == "EE9033"
results.append(("品番EE9033抽出+自動紐付け成功", ok3))

# --- 検証3: 紐付け先SKUとsize_pattern・cost_price ---
linked = body.get("linked") or []
print("  紐付けSKU:", [(l["sku_id"], l["size"], l["cost_price"]) for l in linked])
ok4 = any(str(l["sku_id"]) == "900000001" for l in linked) and \
      any(str(l["sku_id"]) == "900000002" for l in linked)
results.append(("JP25cm→EU40・JP26.5cm→EU42.5の両SKUにヒット", ok4))
ok5 = not any(str(l["sku_id"]) == "900000099" for l in linked)
results.append(("SHOEI Z8（別品番）は混入しない", ok5))

# size_patternが楽天ページ表記のcmか
products = json.load(open(appmod.PRODUCTS_FILE))
sp = {str(p.get("poizon_sku_id")): p.get("size_pattern") for p in products}
print("  size_pattern:", sp)
ok6 = sp.get("900000001") == "25cm" and sp.get("900000002") == "26.5cm"
results.append(("size_patternが楽天表記cm(25cm/26.5cm)で保存", ok6))

links = json.load(open(appmod.POIZON_LINKS_FILE))
cp = {k: v.get("cost_price") for k, v in links.items()}
print("  cost_price:", cp)
ok7 = cp.get("900000001") == 17820
results.append(("cost_price=17820(税込価格)保存", ok7))

# --- 検証4: クエリ付きURL品番フォールバック（楽天ブランチ無効化シナリオはスキップ、URL正規化のみ直接確認）---
import re as _re
_url_clean = _re.sub(r"[?#].*$", "", URL)
mm = _re.search(r"/([A-Za-z0-9]+-[A-Za-z0-9]+|[A-Z]{1,3}\d{3,6})(?:-[A-Za-z0-9]+)?(?:\.html)?/?$", _url_clean)
ok8 = mm is not None
results.append(("URLフォールバック正規表現がクエリ付きURLでも一致", ok8))

# --- 検証5: 登録URLからクエリ文字列が除去されていること ---
_link0 = links.get("900000001") or {}
ok9 = "?" not in _link0.get("url", "") and "variantId" not in _link0.get("url", "")
results.append(("登録URLからvariantIdクエリが除去されている", ok9))
_p0 = [p for p in products if str(p.get("poizon_sku_id")) == "900000001"][0]
ok10 = "?" not in _p0.get("url", "")
results.append(("products.json側URLもクエリ除去", ok10))

# --- 検証6: checkerでサイズ別在庫判定（実ページ・size_pattern=楽天cm表記）---
from checker import check_product as _cp
_p_in = {"url": _link0.get("url"), "size_pattern": "22cm", "stock_keyword": "", "name": "検証22cm"}
_st22, _dt22 = _cp(_p_in, {})
_p_in2 = {"url": _link0.get("url"), "size_pattern": "25cm", "stock_keyword": "", "name": "検証25cm"}
_st25, _dt25 = _cp(_p_in2, {})
print("  checker判定 22cm:", _st22, "/", _dt22, " 25cm:", _st25, "/", _dt25)
ok11 = (_st22 == "IN_STOCK") and (_st25 == "SOLD_OUT")
results.append(("checker実ページ判定: 22cm=在庫あり・25cm=売切れが正しく分かれる", ok11))

# --- 結果表示 ---
print()
fail = 0
for name, ok in results:
    print(("合格" if ok else "不合格"), "|", name)
    if not ok:
        fail += 1
print()
print("合計: {}件中 {}件合格".format(len(results), len(results) - fail))
sys.exit(1 if fail else 0)
