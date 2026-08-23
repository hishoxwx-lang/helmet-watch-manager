#!/usr/bin/env python3
"""在庫通知くん templates/poizon.html のpush前検証スクリプト。

使い方（リポジトリのチェックアウト先で実行。リポジトリ内にコピーして使うのが確実）:
    cp <skill_dir>/scripts/verify_poizon_html.py /tmp/helmet-watch-manager/scripts/
    cd /tmp/helmet-watch-manager
    env -u PYTHONPATH PYTHONNOUSERSITE=1 /tmp/hwm_venv/bin/python scripts/verify_poizon_html.py

または --target でHTMLパスを直接指定（リポジトリ配置不要）:
    python3 <skill_dir>/scripts/verify_poizon_html.py --target /tmp/helmet-watch-manager/templates/poizon.html --no-flask

検証内容（全日本語出力）:
  1. <script>ブロック抽出 → Jinja2 {{ }} をダミー文字列に置換 → node --check でJS構文検証
  2. インラインイベントハンドラ（onclick= / onchange= / oninput=）の残存件数 = 0件
  3. event delegation に必要な関数・data属性の存在
  4. JS括弧バランス（{} と ()）
  5. Flask test_client で /poizon をGET → 200 + addEventListener 含む + onclick=" 含まない
     （config.json を一時書き換え・終了時に復元する。--no-flask でスキップ可）

前提: node がインストール済みであること。Flask検証にはリポジトリルートに
app.py と templates/ が必要（--repo で指定可）。
"""
import sys, os, re, json, ast, subprocess, argparse

BASE = os.path.dirname(os.path.abspath(__file__))  # scripts/ を想定
REPO = os.path.dirname(BASE)                        # リポジトリルート
TARGET = os.path.join(REPO, 'templates', 'poizon.html')

R = []
def chk(name, ok, detail=""):
    s = "合格" if ok else "不合格"
    R.append((name, s, detail))
    print("[%s] %s%s" % (s, name, " — " + detail if detail else ""))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-flask', action='store_true', help='Flask レンダリング検証をスキップ')
    parser.add_argument('--target', default=TARGET, help='検証対象のHTMLファイルパス')
    parser.add_argument('--repo', default=None, help='リポジトリルート（Flask検証用。未指定時はスクリプト配置場所から推定）')
    args = parser.parse_args()

    repo = args.repo or REPO
    html = open(args.target, encoding='utf-8').read()

    # 1. JS構文検証（Jinja2置換込み）
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
    js = '\n'.join(scripts)
    js_clean = re.sub(r"\{\{[^}]+\}\}", "'/test/'", js)
    tmp_js = os.path.join(os.environ.get('TMPDIR', '/tmp'), '_verify_poizon_js.js')
    with open(tmp_js, 'w') as f:
        f.write(js_clean)
    r = subprocess.run(['node', '--check', tmp_js], capture_output=True, text=True)
    os.remove(tmp_js)
    chk("JS構文（node --check・Jinja2置換後）", r.returncode == 0,
        r.stderr.strip().splitlines()[0][:120] if r.returncode else "OK")

    # 2. インラインイベントハンドラ排除
    for attr in ['onclick="', 'onchange="', 'oninput="']:
        cnt = html.count(attr)
        chk("インライン%s排除" % attr.rstrip('="'), cnt == 0, "残存%d件" % cnt)

    # 3. event delegation 要素
    for pat in ['addEventListener', 'handleTableClick', 'data-sku', 'data-bidding', 'data-action', 'data-col']:
        chk("存在: %s" % pat, pat in html)

    # 4. 括弧バランス
    chk("JS中括弧バランス", js.count('{') == js.count('}'), "%d/%d" % (js.count('{'), js.count('}')))
    chk("JS丸括弧バランス", js.count('(') == js.count(')'), "%d/%d" % (js.count('('), js.count(')')))

    # 5. Flask レンダリング検証
    if not args.no_flask:
        cfg_path = os.path.join(repo, 'config.json')
        bak = open(cfg_path).read()
        with open(cfg_path, 'w') as f:
            json.dump({"password_hash": "verify"}, f)
        try:
            sys.path.insert(0, repo)
            import app as A
            A.app.config['TESTING'] = True
            c = A.app.test_client()
            with c.session_transaction() as s:
                s['logged_in'] = True
            resp = c.get('/poizon')
            body = resp.data.decode('utf-8')
            chk("/poizon レンダリング200", resp.status_code == 200, "status=%d" % resp.status_code)
            chk("本文にaddEventListener含む", 'addEventListener' in body)
            chk("本文にonclick=含まない", 'onclick="' not in body)
        finally:
            open(cfg_path, 'w').write(bak)

    # 結果
    t = len(R); p = sum(1 for _, s, _ in R if s == "合格")
    print("\n" + "=" * 50)
    print("検証結果: %d/%d 合格" % (p, t))
    if p < t:
        for n, s, d in R:
            if s != "合格":
                print("  x %s: %s" % (n, d))
    print("=" * 50)
    sys.exit(0 if p == t else 1)

if __name__ == '__main__':
    main()
