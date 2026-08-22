# 在庫通知くん 改良スペック（2026-08-21・第3弾: 6機能）

前回（2026-08-14）の7項目改良に続く、第3弾改良。ユーザー選択: **#4, #6, #8, #9, #10, #11**。

## 改良項目

### #4 汎用JSON-LD対応（checker.py）

**現状**: Coach Outletの在庫判定は `coachoutlet.com` 専用コード（check_generic内1.4節）。同様のschema.org構造を持つサイトは個別実装が必要。

**仕様**:
- check_generic に「汎用JSON-LD判定」を追加（Coach専用コードの一般化）
- 対象: `<script type="application/ld+json">` 内の Product / Offer
  - `"availability": "https://schema.org/InStock|OutOfStock|LimitedAvailability|PreOrder|SoldOut"`
  - size_patternがある場合: JSON-LDの sku/name/color に size_pattern が含まれるSKUを優先判定
  - size_patternなし: 全Offerが同一状態ならその状態、混在ならUNKNOWN
- 適用条件: ドメイン問わず（Yahoo!/楽天の専用ルートより後・キーワード判定より前）
- Coach専用コードは残しつつ、共通関数 `_check_json_ld(html, size_pattern)` に統合

**受け入れ基準**: coachoutlet.comの実HTMLで現行と同じ判定結果。JSON-LDモックでInStock/OutOfStock/混在/無しの4分岐が正しく返る。

### #6 仕入先値下がり通知（checker.py + app.py）

**現状**: cost_priceはauto_link時のみ保存・以後更新されない。仕入先の値下がりに気付けない。

**仕様**:
- checker main() の各商品チェック後、poizon_links.json から該当商品のURL+cost_priceを取得
- cost_price設定済みなら仕入先ページを再fetchして現在価格を抽出
- 現在価格 < 前回cost_price → Discord通知「📉 仕入先が値下がり: {name} ¥{old} → ¥{new}」+ poizon_links.json更新
- 通知頻度制御: 同一商品の値下がり通知は state.json に last_cost_alert を記録し24時間以内は再通知しない
- 価格抽出: 汎用パターン（JSON-LD price / Yahoo applicablePrice / メタタグ og:price:amount）を順に試行
- 取得失敗時は静かにスキップ（監視本体に影響させない）
- 設定トグル: settings に「仕入先値下がり通知」ON/OFF（デフォルトOFF＝既存動作を壊さない）

**受け入れ基準**: 実Yahoo!ページ（adidas wf945-jz8731）で現在価格15999を抽出できる。mockで旧価格より安い場合に通知文面が生成される。

### #10 fetch失敗の1回リトライ（checker.py）

**現状**: fetch_html_auto 失敗時は即UNKNOWN。一時的なネットワークエラーでも未確認になる。

**仕様**:
- fetch_html_auto 内部でリトライを実装（呼び出し元を変更せず全体に適用）
- requests失敗→curl_cffi失敗→ **3秒待って最初から再試行（1回のみ）**
- リトライ対象: ネットワーク系エラー（Timeout, ConnectionError, 5xx）。403/404等の4xxはリトライしない（ブロックは再試行しても無駄）
- 合計タイムアウトを考慮し、最大試行2回（初回+リトライ1回）
- ログ: リトライ時 "    -> retrying once after transient error..."

**受け入れ基準**: mockで1回目Timeout・2回目成功の場合に正常HTMLが返る。403は即raise。

### #11 商品別監視間隔（products.json + checker.py + edit.html）

**現状**: 全商品が毎回チェック（5分トリガー毎に全件）。

**仕様**:
- products.json に新フィールド `check_interval`（分・0=デフォルト=毎回）
- checker main(): 各商品チェック前に最終チェック時刻を確認し、interval未満ならスキップ
  - 最終チェック時刻: state.json の checked_at フィールド（新規追加・既存state破壊しない）
  - interval=0 or 未設定 → 毎回チェック（既存動作）
- edit.html に「監視間隔(分)」入力欄追加（空欄=毎回）
- app.py add/edit ルートで check_interval を保存
- 表示: 出品一覧の紐付けバッジ付近に「⏱N分」表示（interval設定時のみ）

**受け入れ基準**: interval=30の商品は前回チェックから10分では スキップログが出る。interval=0は毎回チェックされる。

### #3 監視の並列化（checker.py main）

**現状**: 商品ループが直列。120件×数秒 = 全周10分超えの恐れ。

**仕様**:
- concurrent.futures.ThreadPoolExecutor(max_workers=6) で並列fetch
- 並列対象: check_product() の呼び出し（ネットワークI/O主体なのでGIL影響小）
- 結果処理（Discord通知・state更新・履歴記録・POIZON取下げ）は**メインスレッドで順次実行**（競合防止）
- max_workers は config の `parallel_workers` で変更可（デフォルト6）
- 出力順序: 完了順でなく元のproducts順に整形してprint（ログ可読性維持）

**受け入れ基準**: mockで5商品×2秒遅延の場合、直列10秒→並列約4秒に短縮。通知・state保存は従来通り1回ずつ。

### #9 出品一覧ページング（templates/poizon.html）

**現状**: 120件一括描画。300件超でDOM重くなる見込み。

**仕様**:
- クライアント側ページング: 1ページ50件
- UI: テーブル下部に「‹ 前 | 1 2 3 … | 次 ›」（現在ページ強調・省略記号付き）
- ブランドタブ・検索・ソート変更時にページ番号を1へリセット
- ページ切替時はスクロール位置をテーブル上部へ
- resultCount表示に「(N-M件目)」追記

**受け入れ基準**: 120件データで3ページ生成・各ページ50/50/20件。検索で絞り込むとページ1に戻る。

### #1 実質利益常時表示（app.py listings API + poizon.html）

**現状**: 利益列は「販売価格−仕入値」。手数料等を含まない粗利。

**仕様**:
- listings API の profit 計算式を変更: `net_profit = round(price * 0.94 - 1500 - cost)`
  （POIZON手数料5%+決済1%+作業費1500円 = 自動調整APIと同一式）
- 利益率も追加: `profit_rate = net_profit / price * 100`（小数1桁）
- UI: 利益セルを「+¥12,345 (27.2%)」形式に。利益率<15%は黄色警告、<0%は赤
- 列名を「利益」→「実質利益」に変更
- ソートはnet_profitで従来通り

**受け入れ基準**: price=50400, cost=30360 → net=¥10,536, rate=20.9%。UIに両方表示。

### #8 ユーザーデータ自動バックアップ（app.py + GitHub Actions）

**現状**: config/products/poizon_links/state はConoHaローカルのみ。サーバー障害で消失リスク。

**仕様**:
- app.py: `/api/backup` エンドポイント追加（login必須）
  - 5ファイル（config/products/poizon_links/state/state_history）を1つのJSON（タイムスタンプ付き）にまとめる
  - **APIキー類はマスクせず実値を含む**（復元用）※レスポンスには含めない
- GitHub Actions（.github/workflows/backup.yml）:
  - 毎日22:00 JST（cron: '0 13 * * *' UTC）に ConoHa の `/api/backup` をcurl
  - トークンはGitHub Secrets（BACKUP_URL, BACKUP_USER, BACKUP_PASS）
  - 取得したJSONを artifacts としてアップロード（保持90日）
  - 失敗時はworkflow失敗（メール通知）
- バックアップトークン: config.json に `backup_token` を新設（settings画面で生成・外部連携トークンとは別管理）
- `/api/backup` は X-Api-Token ヘッダーの backup_token でも通す（Actions用・login不要経路）

**受け入れ基準**: ローカルで `/api/backup` が5ファイル内容を含むJSONを返す。Actions YAMLの構文検証合格。

## 影響範囲

| ファイル | 変更種別 |
|---|---|
| checker.py | #4 #6 #10 #11 #3 |
| app.py | #6(設定) #11(add/edit) #1(listings) #8(/api/backup) |
| templates/poizon.html | #9(ページング) #1(実質利益) #11(⏱表示) |
| templates/edit.html | #11(監視間隔欄) |
| templates/settings.html | #6(値下がり通知トグル) |
| .github/workflows/backup.yml | #8(新規) |

## 制約（Card B準拠）

- 既存データ（products/state/poizon_links/config）への変更はフィールド追加のみ
- POIZON API書き込み（価格変更・取り下げ）は今回触らない
- Discord通知の新規種別は2つ（#6値下がり・#8はActions側）
