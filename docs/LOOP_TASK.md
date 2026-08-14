# LOOP_TASK: 在庫通知くん 改良（2026-08-14）

## 実行スコープ

| カード | 選択 |
|--------|------|
| Card A: Paid API / proxy | **1. Free tier + official APIs only** — 新規APIコストなし。既存のPOIZON API・GLM APIを使用 |
| Card B: Production data writes | **3. Production read-only + 4. Limited production writes** — state_history.json（新規ファイル）への書き込み。既存データ(products.json等)は破壊しない。POIZON価格変更APIは既存機能の拡張（確認ダイアログあり） |

## フェーズ構成

### Phase 1: バックエンド（Python）
- [ ] E: `poizon_api.py` — `query_listings()`のページネーション対応
- [ ] C: `checker.py` — 状態変化履歴記録（state_history.json）
- [ ] G: `app.py` — `/run`非同期化、`/api/check_status`追加
- [ ] F: `app.py` — `/api/test_discord`追加
- [ ] C: `app.py` — `/api/history/<product_id>`、`/api/history`追加

### Phase 2: フロントエンド（HTML/JS）
- [ ] A: `poizon.html` — 自動取得＋60秒リフレッシュ
- [ ] B: `poizon.html` — 列ソート機能
- [ ] D: `poizon.html` — チェックボックス＋一括価格操作
- [ ] F: `settings.html` — Discordテスト送信ボタン
- [ ] C: `poizon.html` — 履歴モーダル
- [ ] G: `poizon.html` — 非同期チェック実行UI

### Phase 3: 検証
- [ ] Mac環境で全ルートのインポート・起動確認
- [ ] ページネーション動作確認（モック）
- [ ] ソート・一括操作のロジック確認（モックデータ）
- [ ] GitHub push

## Definition of Done

1. 出品一覧が自動表示＋60秒更新される
2. 列ヘッダークリックでソート可能
3. 在庫変化履歴が記録・表示される
4. 一括価格調整が動作する
5. 100件超の出品が全件取得される
6. Discordテスト通知が送れる
7. チェック実行でUIが固まらない
8. ConoHaで「最新版に更新」→全機能動作

## 進行記録

（作業開始時に追記）

## 承認記録

- Card A: Free tier + official APIs only（2026-08-14）
- Card B: Production read-only + Limited writes（state_history.jsonのみ新規、既存データ破壊なし）（2026-08-14）
