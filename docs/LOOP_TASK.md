# LOOP_TASK: 在庫通知くん 改良 第3弾（2026-08-21）

## 実行スコープ

| カード | 選択 |
|--------|------|
| Card A: Paid API / proxy | **1. Free tier + official APIs only** — 新規APIコストなし。既存のPOIZON API・GLM API・Discord webhookのみ |
| Card B: Production data writes | **4. Limited production writes** — state.json への checked_at/last_cost_alert フィールド追加、poizon_links.json の cost_price 更新（値下がり時のみ）、config.json への新設定キー追加。既存フィールドは破壊しない。POIZON API書き込みは今回触らない |

## フェーズ構成

### Phase 1: checker.py（#10 → #4 → #11 → #6 → #3 の順で依存を解決）
- [ ] #10: fetch_html_auto に一時エラー時リトライ（4xx除外）
- [ ] #4: 汎用JSON-LD判定 `_check_json_ld()` 追加＋Coach専用コード統合
- [ ] #11: check_interval スキップ判定（state.checked_at 参照）＋ checked_at 記録
- [ ] #6: 仕入先値下がり検知→Discord通知＋cost_price更新（24h抑制）
- [ ] #3: ThreadPoolExecutor 並列化（結果処理はメインスレッド順次）

### Phase 2: app.py
- [ ] #11: add/edit ルートで check_interval 保存
- [ ] #6: settings に cost_alert_enabled トグル
- [ ] #1: listings API の profit を実質利益に変更＋profit_rate 追加
- [ ] #8: /api/backup エンドポイント（backup_token認証 or login）

### Phase 3: テンプレート
- [ ] poizon.html: #9 クライアントページング（50件/頁）
- [ ] poizon.html: #1 実質利益+利益率表示・列名変更
- [ ] poizon.html: #11 ⏱間隔バッジ表示
- [ ] edit.html: #11 監視間隔入力欄
- [ ] settings.html: #6 値下がり通知トグル

### Phase 4: CI
- [ ] .github/workflows/backup.yml（#8・毎日22:00 JST）

## テストマトリクス

| 機能 | 単体 | 統合 | 本番E2E |
|------|------|------|---------|
| #10 リトライ | mock Timeout→成功 / 403→即raise | fetch全体 | LV 403はUNKNOWN維持 |
| #4 JSON-LD | InStock/OutOfStock/混在/無し | Coach実HTML | Coach出品の監視ログ |
| #11 間隔 | interval=30で10分経過→skip | products読込 | UI⏱表示確認 |
| #6 値下がり | mock価格比較 | 実Yahoo!価格抽出 | Discord通知文面 |
| #3 並列化 | 5商品mock遅延→短縮確認 | 全商品チェック完了 | 出力順序・通知回数 |
| #9 ページング | JSローカル | 120件データ | 3ページ遷移 |
| #1 実質利益 | 計算式単体 | listings応答 | UI表示 |
| #8 バックアップ | 応答JSON検証 | — | Actions初回実行 |

## Definition of Done

- [ ] 全テストマトリクス合格
- [ ] 既存機能の回帰なし（adidas auto_link・在庫チェック・Discord通知）
- [ ] push済み・ConoHa反映済み・本番E2E合格
- [ ] 一時検証スクリプト削除済み

## 進捗台帳

| 日時 | 項目 | 結果 |
|------|------|------|
| 2026-08-21 12:00 | SETUP完了 | 既存コード精査済み・SPEC更新 |

## 承認記録

- Card A: （承認待ち）
- Card B: （承認待ち）
