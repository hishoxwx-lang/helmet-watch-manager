# -*- coding: utf-8 -*-
"""
poizon_api.py - POIZON Open API クライアント（requests版）

helmet-watch-manager（在庫通知くん）用に、httpx に依存せず
標準 requests のみで動く POIZON API クライアント。

主な機能:
- apiId=51: 出品一覧取得（Query Listing List）
- apiId=26: 出品取り下げ（Cancel Listing）
- MD5署名認証（app_key + timestamp + sign）

認証情報は config.json から読み込む（設定画面で入力）。
"""
import hashlib
import json
import time
from urllib.parse import quote_plus

import requests

# POIZON API ベースURL
BASE_URL = "https://open.poizon.com"

# apiId=51: Query Listing List
LISTING_LIST_PATH = "/dop/api/v1/pop/api/v1/retrieve-bid/general-type-bidding-list"

# apiId=26: Cancel Listing
CANCEL_PATH = "/dop/api/v1/pop/api/v1/cancel-bid/cancel-bidding"


# ==================== 署名 ====================

def _now_timestamp_ms():
    """現在のエポックミリ秒"""
    return int(time.time() * 1000)


def _normalize_value(obj):
    """署名用に値を正規化"""
    if isinstance(obj, dict):
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if isinstance(obj, (list, tuple)):
        return ",".join(_normalize_value(x) for x in obj)
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if obj is None:
        return "null"
    return str(obj)


def _build_sign_string(params, app_secret):
    """署名対象文字列を構築"""
    items = []
    for key in sorted(params.keys()):
        value = params[key]
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        vs = _normalize_value(value)
        items.append("{}={}".format(quote_plus(str(key)), quote_plus(vs)))
    return "&".join(items) + app_secret


def _calculate_sign(params, app_secret):
    """MD5(32bit)署名を大文字で返す"""
    sign_str = _build_sign_string(params, app_secret)
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


# ==================== クライアント ====================

class PoizonClient:
    """POIZON API クライアント（requests版）"""

    def __init__(self, app_key, app_secret, access_token="", timeout=30):
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.timeout = timeout

    def _build_params(self, biz):
        """共通パラメータ + 業務パラメータをマージして署名"""
        params = {
            "app_key": self.app_key,
            "timestamp": _now_timestamp_ms(),
            "language": "ja",
            "timeZone": "Asia/Tokyo",
        }
        if self.access_token:
            params["access_token"] = self.access_token
        # 業務パラメータをマージ
        for k, v in (biz or {}).items():
            if k not in params:
                params[k] = v
        params["sign"] = _calculate_sign(params, self.app_secret)
        return params

    def post(self, path, biz=None):
        """API を POST で呼び出し、JSON レスポンスを返す"""
        url = BASE_URL + path
        body = self._build_params(biz or {})
        resp = requests.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        code = data.get("code")
        if code not in (200, "200", 0):
            msg = data.get("msg") or data.get("message") or "unknown error"
            return {"error": "POIZON API error code={} msg={}".format(code, msg), "raw": data}

        return data


# ==================== API ラッパ ====================

def query_listings(app_key, app_secret, access_token="",
                   trade_status=2, region="JP", page_size=100,
                   exclusive_start_offset_id=0):
    """apiId=51: 出品一覧を取得。

    Args:
        trade_status: 2=出品中（デフォルト）, 4=取消済, 6=成約済
        region: JP（デフォルト）
        page_size: 1〜100
        exclusive_start_offset_id: ページング用

    Returns:
        list: 出品リスト。各要素は dict:
            - sellerBiddingNo: 出品ID
            - skuId: SKU ID
            - spuId: SPU ID
            - price: 価格
            - currency: 通貨
            - tradeStatus: 取引状態
            - tradeSubStatus: 取引サブ状態
            - quantity: 数量
    エラー時: {"error": "..."}
    """
    client = PoizonClient(app_key, app_secret, access_token)
    data = client.post(LISTING_LIST_PATH, {
        "tradeStatus": trade_status,
        "region": region,
        "pageSize": page_size,
        "exclusiveStartOffsetId": exclusive_start_offset_id,
    })
    if "error" in data:
        return data

    listing_data = data.get("data", {})
    items = listing_data.get("list", [])
    return items


def cancel_listing(app_key, app_secret, seller_bidding_no, access_token=""):
    """apiId=26: 出品を取り下げ。

    Args:
        seller_bidding_no: 出品ID（query_listingsで取得）

    Returns:
        dict: APIレスポンス
    """
    client = PoizonClient(app_key, app_secret, access_token)
    return client.post(CANCEL_PATH, {
        "sellerBiddingNo": seller_bidding_no,
    })


def get_active_listings(config):
    """config.json から認証情報を読んで出品一覧を取得。

    config に poizon_api_id / poizon_api_key が必要。
    設定画面で入力済みの前提。

    Returns:
        list or {"error": "..."}
    """
    app_key = (config.get("poizon_api_id") or "").strip()
    app_secret = (config.get("poizon_api_key") or "").strip()
    if not app_key or not app_secret:
        return {"error": "POIZON API ID / KEY が未設定（⚙設定画面で入力）"}

    return query_listings(app_key, app_secret)


def _fetch_next_data(html):
    """HTMLから __NEXT_DATA__ のJSONをパースして返す（失敗時 None）"""
    import re as _re
    import json as _json
    m = _re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, _re.S,
    )
    if not m:
        return None
    try:
        return _json.loads(m.group(1))
    except Exception:
        return None


def _collect_images_from_obj(obj, out=None):
    """JSONオブジェクト内の cdn-img.poizon.com 画像URLを再帰的に全収集。

    Returns:
        list[str]: 重複除外した画像URLリスト（出現順）
    """
    import re as _re
    if out is None:
        out = []
    if isinstance(obj, str):
        for m in _re.finditer(
            r'https://cdn-img\.poizon\.com/[A-Za-z0-9/_.\-]+\.(?:jpg|jpeg|png|webp)',
            obj, _re.I,
        ):
            u = m.group(0)
            if u not in out:
                out.append(u)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_images_from_obj(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_images_from_obj(v, out)
    return out


def _find_color_image(next_data, color):
    """__NEXT_DATA__ 内でカラーに紐づく画像URLを探す（ベストエフォート）。

    POIZON/dewu の商品詳細JSON構造は非公開かつ変動するため、
    汎用的なヒューリスティックでカラー別画像を探す:
      1. color値と同じ文字列を値に持つエントリの近傍画像を優先
      2. キー名が colorImage / imgColor / colorUrl のエントリ
      3. 見つからなければ None（呼び出し元でフォールバック）

    Args:
        next_data: __NEXT_DATA__ のパース結果 dict
        color: カラー名（例: "Black", "黒", "WHITE"）

    Returns:
        str: 画像URL（見つからなければ空文字）
    """
    if not next_data or not color:
        return ""

    import re as _re
    color_norm = str(color).strip().lower()
    if not color_norm:
        return ""

    # 戦略1: "color": "<色名>" に最も近い画像URLを探す（距離ベース）
    # JSON文字列上で color値 の出現位置を探し、最も距離が近い画像URLを採用する。
    # 単純な前後N文字ウィンドウだと隣接SKUの画像が混入するため、
    # 各画像URLとcolor値出現位置の文字距離を計算して最小のものを選ぶ。
    try:
        import json as _json
        text = _json.dumps(next_data, ensure_ascii=False)
        text_lower = text.lower()
    except Exception:
        text = str(next_data)
        text_lower = text.lower()

    # color値の全出現位置
    color_positions = [m.start() for m in _re.finditer(_re.escape(color_norm), text_lower)]
    # 全画像URLの出現位置
    img_matches = list(_re.finditer(
        r'https://cdn-img\.poizon\.com/[A-Za-z0-9/_.\-]+\.(?:jpg|jpeg|png|webp)',
        text, _re.I,
    ))

    if color_positions and img_matches:
        best_img = None
        best_dist = None
        for im in img_matches:
            img_url = im.group(0)
            img_center = (im.start() + im.end()) // 2
            # 最も近いcolor出現位置との距離
            min_dist = min(abs(img_center - cp) for cp in color_positions)
            if best_dist is None or min_dist < best_dist:
                best_dist = min_dist
                best_img = img_url
        if best_img:
            return best_img

    # 戦略2: キー名ベース（colorImage / imgColorMap 等）
    found = []
    def _scan(o):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if kl in ("colorimage", "imgcolor", "colorurl", "colorimg", "imgcolormap"):
                    if isinstance(v, str) and "cdn-img.poizon.com" in v:
                        found.append(v)
                    elif isinstance(v, dict):
                        # {color: url} 形式を想定
                        for ck, cv in v.items():
                            if color_norm in str(ck).lower() and isinstance(cv, str):
                                found.append(cv)
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict):
                                # {"color": "Black", "img": "https://..."}
                                color_val = str(item.get("color") or item.get("name") or "").lower()
                                if color_norm in color_val:
                                    img_val = item.get("img") or item.get("url") or item.get("image")
                                    if isinstance(img_val, str):
                                        found.append(img_val)
            for v in o.values():
                _scan(v)
        elif isinstance(o, list):
            for v in o:
                _scan(v)
    _scan(next_data)
    if found:
        return found[0]

    return ""


def fetch_poizon_image(spu_id, color=""):
    """POIZON商品ページ（poizon.com/product/{spuId}.html）から商品画像URLを取得。

    apiId=51では画像が返らないため、商品ページの__NEXT_DATA__から抽出。
    color を指定した場合は該当カラーの画像を優先的に探す（ベストエフォート・
    見つからなければ従来通り最初の画像を返す）。

    注意: poizon.com の商品ページは301リダイレクトでホームに飛ぶことがある。
    その場合は dewu.com（中国版）にフォールバックし、それでもダメなら空文字。

    Args:
        spu_id: SPU ID
        color: カラー名（任意・カラー別画像優先探索に使用）

    Returns:
        str: 画像URL（取得失敗時は空文字）
    """
    import re as _re
    cache_key = "_poizon_img_cache"
    if not hasattr(fetch_poizon_image, cache_key):
        setattr(fetch_poizon_image, cache_key, {})
    cache = getattr(fetch_poizon_image, cache_key)
    spu_str = str(spu_id)
    color_norm = (color or "").strip().lower()
    cache_key_full = "{}|{}".format(spu_str, color_norm)

    if cache_key_full in cache:
        return cache[cache_key_full]

    found_url = ""

    # ---- 1) poizon.com（国際版）を試す ----
    url = "https://poizon.com/product/{}.html".format(spu_str)
    try:
        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36",
            "Accept-Language": "ja,en-US;q=0.9",
        }, timeout=15, allow_redirects=True)
        html = resp.text
        final_url = resp.url or ""

        # リダイレクトで商品ページ以外（ホーム等）に飛んでいないか確認
        # 商品ページのURLパターン: /product/{id}.html
        is_product_page = ("/product/" in final_url) or ("/product/" in url)

        if is_product_page:
            next_data = _fetch_next_data(html)
            if next_data:
                # カラー指定時: 該当カラー画像を優先探索
                if color_norm:
                    color_img = _find_color_image(next_data, color_norm)
                    if color_img:
                        found_url = color_img

                # カラー画像が見つからなければ __NEXT_DATA__ 内の最初の商品画像
                if not found_url:
                    imgs = _collect_images_from_obj(next_data)
                    if imgs:
                        found_url = imgs[0]
            else:
                # __NEXT_DATA__ がない場合のフォールバック
                imgs = _re.findall(
                    r'https://cdn-img\.poizon\.com/[A-Za-z0-9/_.\-]+\.(?:jpg|jpeg|png|webp)',
                    html, _re.I,
                )
                if imgs:
                    found_url = imgs[0]
    except Exception:
        pass

    # ---- 2) dewu.com（中国版）にフォールバック ----
    if not found_url:
        dewu_url = "https://www.dewu.com/product/{}.html".format(spu_str)
        try:
            resp2 = requests.get(dewu_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.dewu.com/",
            }, timeout=15, allow_redirects=True)
            html2 = resp2.text
            next_data2 = _fetch_next_data(html2)
            if next_data2:
                if color_norm:
                    color_img = _find_color_image(next_data2, color_norm)
                    if color_img:
                        found_url = color_img
                if not found_url:
                    imgs = _collect_images_from_obj(next_data2)
                    if imgs:
                        found_url = imgs[0]
        except Exception:
            pass

    cache[cache_key_full] = found_url
    return found_url
