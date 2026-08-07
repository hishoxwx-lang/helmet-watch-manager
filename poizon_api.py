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
                   exclusive_start_offset_id=0, bidding_type=20):
    """apiId=51: 出品一覧を取得。

    Args:
        trade_status: 2=出品中（デフォルト）, 4=取消済, 6=成約済
        bidding_type: 20=通常出品（デフォルト）, 25=事前入庫, 90=その他
                     0またはNoneで全件取得
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
    biz = {
        "tradeStatus": trade_status,
        "region": region,
        "pageSize": page_size,
        "exclusiveStartOffsetId": exclusive_start_offset_id,
    }
    if bidding_type:
        biz["biddingType"] = bidding_type
    data = client.post(LISTING_LIST_PATH, biz)
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


def fetch_poizon_image(spu_id, color="", sku_id=0, app_key="", app_secret=""):
    """POIZON API（by-sku）で商品画像を取得。

    apiId=140 の by-sku エンドポイントを使い、spuInfo.logoUrl から画像URLを取得。
    skuIdが分かれば個別に取得できる。

    Returns:
        str: 画像URL（取得失敗時は空文字）
    """
    if not sku_id or not app_key or not app_secret:
        return ""

    import hashlib as _hashlib
    import time as _time
    import json as _json

    params = {
        "app_key": app_key,
        "timestamp": int(_time.time() * 1000),
        "language": "ja",
        "timeZone": "Asia/Tokyo",
        "skuIds": [int(sku_id)],
        "region": "JP",
    }

    # 署名
    sign_str = "&".join(
        "{}={}".format(quote_plus(str(k)), quote_plus(_normalize_value(params[k])))
        for k in sorted(params.keys())
        if params[k] is not None and not (isinstance(params[k], str) and params[k] == "")
    ) + app_secret
    params["sign"] = _hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    try:
        resp = requests.post(
            BASE_URL + "/dop/api/v1/pop/api/v1/intl-commodity/intl/sku/sku-basic-info/by-sku",
            json=params,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()
        if data.get("data") and isinstance(data["data"], list):
            for item in data["data"]:
                spu_info = item.get("spuInfo", {})
                logo_url = spu_info.get("logoUrl", "")
                if logo_url:
                    return logo_url
    except Exception:
        pass

    return ""


