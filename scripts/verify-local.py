#!/usr/bin/env python3
"""helmet-watch-manager ローカル検証スクリプト

Mac環境でFlaskアプリのインポート・ルート・新規APIの動作を検証する。
Hermesのvenvパス汚染を回避するため、sys.pathフィルタリングを使用。

使い方:
    cd /tmp/helmet-watch-manager  # リポジトリクローン先
    /path/to/clean/venv/bin/python scripts/verify-local.py

前提:
    - クリーンなvenv に flask, requests がインストール済み
    - クリーンvenv = Hermesのvenvパスを含まない独立venv
      作成: python3 -m venv /tmp/hwm_venv && /tmp/hwm_venv/bin/pip install flask requests
    - node がインストール済み（JS構文チェック用・未インストール時はスキップ）
"""
import sys
import os
import json
import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Hermes venvパスを除外（汚染防止）
sys.path = [p for p in sys.path if 'hermes' not in p.lower()]

# リポジトリルートを特定
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_DIR))

os.chdir(str(REPO_DIR))

results = []

def chk(name, cond, detail=""):
    status = "合格" if cond else "不合格"
    results.append((name, status, detail))
    print("[%s] %s" % (status, name) + (" — %s" % detail if detail else ""))


# === 1. Python構文チェック ===
for f in ['app.py', 'checker.py', 'poizon_api.py']:
    try:
        ast.parse(open(f).read())
        chk("Python構文: %s" % f, True)
    except SyntaxError as e:
        chk("Python構文: %s" % f, False, str(e))

# === 2. HTML括弧バランス + JS構文チェック(node --check) ===
for tmpl in ['poizon.html', 'settings.html']:
    path = 'templates/' + tmpl
    html = open(path).read()
    scripts_js = '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', html, re.S))
    chk("%s JS中括弧" % tmpl, scripts_js.count('{') == scripts_js.count('}'),
        "%d/%d" % (scripts_js.count('{'), scripts_js.count('}')))
    chk("%s JS丸括弧" % tmpl, scripts_js.count('(') == scripts_js.count(')'),
        "%d/%d" % (scripts_js.count('('), scripts_js.count(')')))

    # node --check でJS構文を検証（Jinja2構文を置換してから）
    try:
        js_clean = re.sub(r"\{\{[^}]+\}\}", "'/test/'", scripts_js)
        tmp_js = tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False)
        tmp_js.write(js_clean)
        tmp_js.close()
        r = subprocess.run(['node', '--check', tmp_js.name], capture_output=True, text=True)
        chk("%s JS構文(node --check)" % tmpl, r.returncode == 0,
            r.stderr[:150] if r.returncode else "OK")
        os.unlink(tmp_js.name)
    except FileNotFoundError:
        chk("%s JS構文(node --check)" % tmpl, True, "node未インストール・スキップ")

# === 3. インラインonclick排除チェック（pitfall #50） ===
for tmpl in ['poizon.html', 'settings.html']:
    html = open('templates/' + tmpl).read()
    inline_count = len(re.findall(r'onclick="', html))
    chk("%s インラインonclick排除" % tmpl, inline_count == 0, "残存%d件" % inline_count)

# === 4. Flask アプリ・ルート検証 ===
# config.json をテスト用に一時書き換え
cfg_bak = open('config.json').read()
open('config.json', 'w').write(json.dumps({
    "password_hash": "test_hash",
    "discord_webhook_url": "",
    "poizon_api_id": "",
    "poizon_api_key": "",
}))

try:
    import app as A
    client = A.app.test_client()
    with client.session_transaction() as s:
        s['logged_in'] = True

    # ルート存在確認
    all_rules = [r.rule for r in A.app.url_map.iter_rules()]
    expected_routes = [
        '/api/poizon/listings', '/api/poizon/update_price', '/api/poizon/link',
        '/api/history', '/api/check_status', '/api/test_discord',
        '/api/external/link', '/api/external/state',
        '/poizon', '/run', '/settings', '/update',
    ]
    for route in expected_routes:
        chk("ルート存在: %s" % route, route in all_rules)

    # API応答確認
    resp = client.get('/api/poizon/listings')
    data = resp.get_json()
    chk("出品一覧API: JSON応答", data is not None)
    chk("出品一覧API: 未設定エラー", data is not None and 'error' in data)

    resp = client.get('/api/history')
    data = resp.get_json()
    chk("履歴API: JSON応答 + history配列", data is not None and 'history' in data)

    resp = client.get('/api/check_status')
    data = resp.get_json()
    chk("チェック状態API: idle初期値", data is not None and data.get('status') == 'idle')

    resp = client.post('/api/test_discord')
    data = resp.get_json()
    chk("Discord テストAPI: 応答", data is not None)

    resp = client.get('/run')
    data = resp.get_json()
    chk("非同期チェックAPI: running返却", data is not None and data.get('status') == 'running')

    # /poizon レンダリング確認
    resp = client.get('/poizon')
    body = resp.data.decode('utf-8')
    chk("/poizon レンダリング成功", resp.status_code == 200)
    chk("/poizon addEventListener含む", 'addEventListener' in body)
    chk("/poizon onclick=含まない", 'onclick="' not in body)

finally:
    open('config.json', 'w').write(cfg_bak)

# === 5. checker.py 履歴記録 ===
import checker as K
td = tempfile.mkdtemp(prefix='hermes-verify-')
K.STATE_HISTORY_FILE = Path(td + '/state_history.json')
K.save_json(K.STATE_HISTORY_FILE, [])

K.append_history(1, "テスト商品", "IN_STOCK", "SOLD_OUT", "売切れ検知")
K.append_history(1, "テスト商品", "SOLD_OUT", "IN_STOCK", "再入荷")
history = K.load_json(K.STATE_HISTORY_FILE, [])
chk("履歴記録: 2件", len(history) == 2)
chk("履歴記録: 最新が再入荷", history[-1]['new_state'] == 'IN_STOCK')
chk("履歴記録: タイムスタンプあり", all('timestamp' in h for h in history))

# 1000件上限
for _ in range(1005):
    K.append_history(99, "bulk", "", "IN_STOCK", "")
chk("履歴記録: 1000件上限", len(K.load_json(K.STATE_HISTORY_FILE, [])) == 1000)

shutil.rmtree(td, ignore_errors=True)

# === 6. ページネーション（モック・ad067ce後のcursor方式） ===
from poizon_api import get_active_listings
import poizon_api as P

chk("ページング: 未設定エラー", 'error' in get_active_listings({"poizon_api_id": "", "poizon_api_key": ""}))

# query_listings は {"items": [...], "last_offset_id": int} を返す（ad067ce以降）
cc = [0]
def mock_query(ak, as_, **kw):
    cc[0] += 1
    if cc[0] == 1:
        return {"items": [{"skuId": i} for i in range(100)], "last_offset_id": 500}
    elif cc[0] == 2:
        return {"items": [{"skuId": i} for i in range(100, 150)], "last_offset_id": 900}
    return {"items": [], "last_offset_id": 0}

orig_q = P.query_listings
P.query_listings = mock_query
result = get_active_listings({"poizon_api_id": "k", "poizon_api_key": "s"})
chk("ページング: 全150件取得", isinstance(result, list) and len(result) == 150, "len=%d" % len(result) if isinstance(result, list) else str(result)[:60])
chk("ページング: API 2回呼出", cc[0] == 2)

# lastOffsetIdが返らない → 1ページで終了（無限ループしない）
cc2 = [0]
def mock_query2(ak, as_, **kw):
    cc2[0] += 1
    return {"items": [{"skuId": i} for i in range(100)], "last_offset_id": 0}
P.query_listings = mock_query2
get_active_listings({"poizon_api_id": "k", "poizon_api_key": "s"})
chk("ページング: lastOffsetId無し→1ページ終了", cc2[0] == 1, "calls=%d" % cc2[0])

# 同じlastOffsetIdが返る異常 → seen_offsetsで2ページ目で停止
cc3 = [0]
def mock_query3(ak, as_, **kw):
    cc3[0] += 1
    return {"items": [{"skuId": i} for i in range(100)], "last_offset_id": 500}
P.query_listings = mock_query3
get_active_listings({"poizon_api_id": "k", "poizon_api_key": "s"})
chk("ページング: 同offset異常→停止", cc3[0] == 2, "calls=%d" % cc3[0])

# 100件未満 → 即終了
cc4 = [0]
def mock_query4(ak, as_, **kw):
    cc4[0] += 1
    return {"items": [{"skuId": i} for i in range(75)], "last_offset_id": 999}
P.query_listings = mock_query4
r4 = get_active_listings({"poizon_api_id": "k", "poizon_api_key": "s"})
chk("ページング: 75件→1ページ終了", cc4[0] == 1 and len(r4) == 75)

P.query_listings = orig_q

# === 7. 外部連携API（d5bc45d・トークン認証） ===
import app as A2  # 再import（セクション4と同一モジュール）
client2 = A2.app.test_client()

# external_api_token を設定してからテスト（configバックアップはセクション4のfinallyで復元済み）
cfg_bak2 = open('config.json').read()
open('config.json', 'w').write(json.dumps({"password_hash": "x", "external_api_token": "testtoken123"}))

pl_path = 'poizon_links.json'
pl_bak = open(pl_path).read() if os.path.exists(pl_path) else None
pr_bak = open('products.json').read()

try:
    with client2.session_transaction() as s:
        s['logged_in'] = True

    # トークンなし・不正 → 403
    r = client2.post('/api/external/link', data={'sku_id': '1', 'url': 'https://x.com/'})
    chk("外部連携: トークンなし403", r.status_code == 403)
    r = client2.post('/api/external/link', data={'sku_id': '1', 'url': 'https://x.com/', 'token': 'wrong'})
    chk("外部連携: 不正トークン403", r.status_code == 403)

    # form登録
    r = client2.post('/api/external/link', data={
        'sku_id': '111222333', 'url': 'https://example.com/p/1',
        'name': '検証商品', 'token': 'testtoken123'})
    d = r.get_json()
    chk("外部連携: form登録OK", r.status_code == 200 and d and d.get('ok') is True)

    # JSON+ヘッダー登録
    r = client2.post('/api/external/link', json={
        'sku_id': '444555666', 'url': 'https://example.com/p/2', 'name': '検証商品B'},
        headers={'X-Api-Token': 'testtoken123'})
    d = r.get_json()
    chk("外部連携: JSON登録OK", r.status_code == 200 and d and d.get('ok') is True)

    # データ反映
    links = json.load(open(pl_path))
    prods = json.load(open('products.json'))
    skus = [str(p.get('poizon_sku_id', '')) for p in prods]
    chk("外部連携: poizon_links.json反映", '111222333' in links and '444555666' in links)
    chk("外部連携: products.json監視追加", '111222333' in skus and '444555666' in skus)

    # 再登録は上書き
    client2.post('/api/external/link', data={
        'sku_id': '111222333', 'url': 'https://example.com/p/1-NEW', 'token': 'testtoken123'})
    prods2 = json.load(open('products.json'))
    m = [p for p in prods2 if str(p.get('poizon_sku_id', '')) == '111222333']
    chk("外部連携: 再登録は上書き", len(m) == 1 and m[0]['url'] == 'https://example.com/p/1-NEW')

    # state API
    r = client2.get('/api/external/state?sku_ids=111222333,444555666&token=testtoken123')
    d = r.get_json()
    chk("外部連携: state API応答+linked", r.status_code == 200 and d and
        d.get('states', {}).get('111222333', {}).get('linked') is True)

    # パラメータ欠落 → 400
    chk("外部連携: sku_id欠落400", client2.post('/api/external/link', data={'url': 'https://x.com/', 'token': 'testtoken123'}).status_code == 400)
    chk("外部連携: url欠落400", client2.post('/api/external/link', data={'sku_id': '9', 'token': 'testtoken123'}).status_code == 400)

    # トークン未設定 → 403+メッセージ
    open('config.json', 'w').write(json.dumps({"password_hash": "x"}))
    r = client2.post('/api/external/link', data={'sku_id': '1', 'url': 'https://x.com/', 'token': 'any'})
    d = r.get_json()
    chk("外部連携: トークン未設定403", r.status_code == 403 and d and '未設定' in d.get('error', ''))

finally:
    open('config.json', 'w').write(cfg_bak2)
    open('products.json', 'w').write(pr_bak)
    if pl_bak is not None:
        open(pl_path, 'w').write(pl_bak)
    elif os.path.exists(pl_path):
        os.remove(pl_path)

# === 結果 ===
print("\n" + "=" * 60)
total = len(results)
passed = sum(1 for _, s, _ in results if s == "合格")
failed = sum(1 for _, s, _ in results if s == "不合格")
print("検証結果: %d/%d 合格 (%d 不合格)" % (passed, total, failed))
if failed:
    print("\n不合格項目:")
    for name, status, detail in results:
        if status == "不合格":
            print("  x %s: %s" % (name, detail))
else:
    print("\n全項目合格 — ローカル検証完了")
print("=" * 60)
sys.exit(1 if failed else 0)
