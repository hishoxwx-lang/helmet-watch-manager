# -*- coding: utf-8 -*-
"""Parr Mark対応・自動取り下げ直接API化の検証（実ページHTML）。

検証内容:
  1. 実parrmarkページ(Shift_JIS)の解析: 品番X000010274・5SKU(Black S/M/L + Habitat S/M)
  2. checker: Black L=在庫あり・Black S=品切れ・Habitat S=品切れが正しく判定
  3. auto_link: POIZON出品(Black L)1SKUに紐付け・size/color保存
  4. cancel_listing モック: 売切れ検知→直接取り下げが呼ばれる
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- モックPOIZON API ----
MOCK_LISTINGS = [
    {"skuId": "1070014377", "spuTitle": "Arcteryx THERME Jackets Men's Hooded",
     "sellerBiddingNo": "151220035038200105",
     "skuSaleProp": json.dumps([{"name": "Color", "value": "Black"}, {"name": "Size", "value": "L"}]),
     "regionSalePvInfoList": [
         {"name": "カラー", "localValue": "ブラック/ブラック", "skuManySizeInfos": []},
         {"name": "サイズ", "localValue": "L", "skuManySizeInfos": [{"sizeKey": "SIZE", "sizeValue": "L"}]},
     ]},
    {"skuId": "999999999", "spuTitle": "SHOEI Z8",
     "skuSaleProp": json.dumps([{"name": "Size", "value": "M"}]),
     "regionSalePvInfoList": []},
]

import poizon_api
poizon_api.get_active_listings = lambda config: list(MOCK_LISTINGS)
poizon_api.fetch_poizon_sku_info_batch = lambda ids, k, s: {
    "1070014377": {"article_number": "X000010274-服", "brand_name": "Arc'teryx"},
    "999999999": {"article_number": "Z8", "brand_name": "SHOEI"}}

# cancel_listing は呼ばれたら記録
CANCEL_CALLS = []
def _mock_cancel(app_key, app_secret, bidding_no, access_token=""):
    CANCEL_CALLS.append(bidding_no)
    return {"code": 200, "msg": "success"}
poizon_api.cancel_listing = _mock_cancel

import app as appmod
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

# checker側も一時データファイルへ向ける（poizon_apiのモックが効くよう checkerも同一プロセス）
import checker
checker.CONFIG_FILE = appmod.CONFIG_FILE
checker.STATE_FILE = appmod.STATE_FILE

results = []
URL = "https://www.parrmark.co.jp/web/shop_item_2.asp?id=81779&cat=&make="

# --- 検証1: 実ページ解析 ---
from checker import fetch_html_auto, _parrmark_parse
html = fetch_html_auto(URL)
data = _parrmark_parse(html)
print("  品番:", data["code"] if data else None)
if data:
    for v in data["variants"]:
        print("  ", v)
ok1 = bool(data and data["code"] == "X000010274" and len(data["variants"]) == 5)
results.append(("実ページ解析: 品番X000010274・5SKU(Black S/M/L+Habitat S/M)", ok1))

# --- 検証2: checker サイズ+カラー別判定 ---
for size, color, expect in [("L", "Black", "IN_STOCK"), ("S", "Black", "SOLD_OUT"), ("S", "Habitat", "SOLD_OUT")]:
    p = {"url": URL, "size_pattern": size, "color_pattern": color, "stock_keyword": "", "name": "検証"}
    st, dt = checker.check_product(p, {})
    print("  {} {}: {} / {}".format(color, size, st, dt))
    ok = st == expect
    results.append(("checker判定 {} {}={}".format(color, size, expect), ok))

# --- 検証3: auto_link実行 ---
client = appmod.app.test_client()
resp = client.post("/api/external/auto_link",
                   json={"url": URL, "token": "テストトークン"},
                   environ_base={"HTTP_X_API_TOKEN": "テストトークン"})
body = resp.get_json()
print("  auto_link応答:", json.dumps({k: body.get(k) for k in ("ok", "count", "product_code", "error")}, ensure_ascii=False))
for l in body.get("linked") or []:
    print("  紐付け:", l.get("sku_id"), "|", l.get("size"), "|", l.get("cost_price"))
ok3 = body.get("ok") is True and body.get("count") == 1
results.append(("auto_link: Black Lの1SKUのみ紐付け", ok3))

products = json.load(open(appmod.PRODUCTS_FILE))
p0 = [p for p in products if str(p.get("poizon_sku_id")) == "1070014377"]
print("  登録された商品:", json.dumps(p0[0] if p0 else None, ensure_ascii=False))
ok4 = bool(p0 and p0[0]["size_pattern"] == "L" and p0[0].get("color_pattern", "").upper() == "BLACK")
results.append(("size_pattern=L・color_pattern=Black 保存", ok4))
links = json.load(open(appmod.POIZON_LINKS_FILE))
ok5 = links.get("1070014377", {}).get("cost_price") == 69300
results.append(("cost_price=69300（税込セール価格）保存", ok5))

# --- 検証3.5: 登録URLに必須クエリ(?id=)が保持されていること ---
_l0 = links.get("1070014377") or {}
ok35 = "id=81779" in _l0.get("url", "")
results.append(("登録URLに必須クエリ(id=81779)が保持", ok35))

# --- 検証4: 売切れ検知→直接取り下げ ---
# state.json を IN_STOCK に偽装 → checker の判定で Parrmark ページは実ページ（Black Lはまだ在庫あり）
# → 取り下げ発火を検証するため、sizeを品切れサイズ(S)に変えてモックでOK確認
cfg = json.load(open(appmod.CONFIG_FILE))
cfg["poizon_api_id"] = "キー"
cfg["poizon_api_key"] = "シークレット"
json.dump(cfg, open(appmod.CONFIG_FILE, "w"))
# checker.poizon_api は checker内 import なので checkerモジュールから差し替え
import poizon_api as _pa
# checker は「from poizon_api import ...」を関数内で行うためモジュール参照のまま有効

prod = p0[0]
prod["size_pattern"] = "S"  # Black S は品切れ
prod["color_pattern"] = "Black"
state = {str(prod["id"]): {"state": "IN_STOCK", "detail": "前回在庫あり", "updated_at": "x"}}
json.dump(state, open(appmod.STATE_FILE, "w"))

cfg2 = dict(cfg)
cfg2["discord_webhook"] = ""
ok6 = checker.notify_poizon_delist(prod, cfg2, "Black S: 品切れ")
print("  取り下げAPI呼び出し:", CANCEL_CALLS)
ok6 = ok6 and CANCEL_CALLS == ["151220035038200105"]
results.append(("売切れ検知→POIZON API(apiId=26)直接取り下げ発火", ok6))

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
