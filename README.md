# 📦 在庫通知くん

Webike 等の商品在庫を **ブラウザで管理・監視** する Web アプリ（Python / Flask）。
複数商品を一覧管理し、定期的に在庫状況をチェック。状態が変化したときだけ **Discord 通知** を送ります。

Windows サーバー（ConoHa for Windows 等）上で常駐稼働することを想定しています。

---

## ✨ 特徴

- 🖥️ **ブラウザUI** で商品の追加・削除・有効切替（スマホからも操作可）
- 🔄 **複数商品の一括監視**（Webike 以外のサイトもURL + サイズ + キーワードで追加可能）
- 📣 **Discord Webhook 通知**（在庫→売切れ / 売切れ→再入荷 / 監視開始時のみ通知、無駄に叩かない）
- 🔐 **パスワード認証**（ハッシュ保存、初回アクセスで設定）
- 🪟 **Windows サービス + タスクスケジューラ** で自動常駐・定期監視（`setup.ps1` が全部設定）
- 🇯🇵 **Webike（Shift_JIS）対応**（charset 自動判定）
- 🛒 **楽天市場のサイズ別在庫監視**（Playwright ヘッドレスブラウザで JS レンダリング後のデータを取得。`setup.ps1` が Chromium の導入も自動化）

---

## 🧩 構成

```
helmet-watch-manager/
├── app.py              # Flask Web UI (ポート 8080)
├── checker.py          # 在庫監視エンジン（定期実行）
├── templates/
│   ├── index.html      # 商品一覧 + 追加フォーム
│   ├── settings.html   # Discord URL / パスワード変更
│   ├── login.html      # ログイン画面
│   └── setup.html      # 初回パスワード設定
├── products.json       # 商品リスト（編集可能・初期1件）
├── config.json         # 設定（パスワードハッシュ / Webhook / port / interval）
├── state.json          # 直近の状態（実行時に自動生成、git対象外）
├── requirements.txt
├── setup.ps1           # Windows セットアップ（全自動）
└── README.md
```

---

## 🚀 セットアップ（ConoHa Windows サーバー）

管理者権限の PowerShell を開き、**1行** 実行するだけです。

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\helmet-manager" | Out-Null; Invoke-WebRequest "https://raw.githubusercontent.com/hishoxwx-lang/helmet-watch-manager/main/setup.ps1" -OutFile "$env:USERPROFILE\helmet-manager\setup.ps1" -UseBasicParsing; powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\helmet-manager\setup.ps1"
```

`setup.ps1` が自動で以下を行います:

1. Python 3.12 の確認（無ければ winget でインストール）
2. 最新ファイルを GitHub からダウンロード → `%USERPROFILE%\helmet-manager`
3. `pip install -r requirements.txt`
4. **Playwright Chromium をインストール**（楽天市場監視用・数分）
5. Discord Webhook URL の入力（スキップ可。後でUI設定可）
6. 管理者パスワード（2回入力）→ ハッシュ化して `config.json` に保存
7. Web アプリを **NSSM で Windows サービス化**（無ければタスクスケジューラでスタートアップ起動にフォールバック）
8. 監視ジョブを **タスクスケジューラに5分間隔で登録**
9. ファイアウォールで **TCP 8080 受信許可**
10. パブリックIPを取得してアクセスURLを表示

完了すると以下が表示されます:

```
=== Setup complete ===
Access      : http://<PUBLIC_IP>:8080/
Password    : (the one you entered)
Discord     : (webhook URL)
Web service : HelmetManager (auto-start on boot)
Watch task  : HelmetWatcher (every 5 min)
```

> 💡 Discord URL をコマンドライン引数で渡すこともできます:
> `powershell -ExecutionPolicy Bypass -File setup.ps1 -WebhookUrl "https://discord.com/api/webhooks/..."`

---

## 📖 使い方

1. ブラウザで `http://<サーバーIP>:8080/` を開く
2. セットアップで決めたパスワードでログイン
3. 「商品を追加」フォームから商品を登録
   - **名前**: 任意（例: SHOEI X-Fifteen ルミナスホワイト）
   - **URL**: 商品ページURL（例: `https://www.webike.net/sd/26464072/`）
   - **サイズパターン**: 監視したいサイズの option テキスト（例: `サイズ：L`）
   - **在庫キーワード**: 在庫ありを判定するキーワード（例: `在庫`）
   - **有効**: チェックを入れると監視対象
4. 「▶ 今すぐチェック」で手動実行、状態を確認
5. ⚙ **設定** で Discord Webhook URL の変更・パスワード変更

監視は5分ごとに自動実行され、**状態が変化したときだけ** Discord 通知が飛びます。

### 通知パターン

| 変化 | 通知 |
|---|---|
| 初回 / 新規追加 | ℹ️ 監視開始: {商品名} |
| 在庫あり → 売切れ | 🚨 売り切れました: {商品名} |
| 売切れ → 在庫あり | 🎉 再入荷しました: {商品名} |

---

## 🧪 ローカル（Mac/Linux）での動かし方

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # 楽天市場監視に必要（数分・約180MB）

# 監視を1回だけ実行（Discord未設定ならログのみ出力）
python3 checker.py

# Web UI 起動
python3 app.py
# -> http://localhost:8080/
```

初回はパスワード未設定なので `/setup` 画面でパスワードを設定 → ログイン。

---

## ⏹ 停止・削除方法（Windows）

```powershell
# Web サービス停止・削除（NSSM の場合）
nssm stop HelmetManager
nssm remove HelmetManager confirm

# タスクスケジューラの監視ジョブを無効化・削除
Unregister-ScheduledTask -TaskName "HelmetWatcher" -Confirm:$false

# フォールバック版（タスクで常駐させた場合）
Unregister-ScheduledTask -TaskName "HelmetManager" -Confirm:$false

# ファイアウォール ルール削除
Get-NetFirewallRule -DisplayName "HelmetManager" | Remove-NetFirewallRule
```

---

## 🔧 カスタマイズ（他サイトの追加）

Webike 以外でも、**HTML の `<option>` タグ内** にサイズパターンと在庫キーワードが含まれる構造のサイトなら監視できます。

1. 商品ページのHTMLを確認（ブラウザの開発者ツールで `<option>` を探す）
2. UIの「商品を追加」から入力:
   - **サイズパターン**: 監視したい option の一意に判別できる文字列
   - **在庫キーワード**: option テキスト内で在庫ありを示す文字列

サイト毎の文字コード（Shift_JIS / UTF-8）は `checker.py` が自動判定します。

---

## 🛒 楽天市場の監視について

楽天市場はサイズ別の在庫データを JavaScript で動的に取得するため、**Playwright（ヘッドレス Chromium）** でページをレンダリングしてから在庫を読み取ります。

### 使い方

1. 商品追加フォームの URL 欄に楽天商品URLを入力
2. 「📋 サイズ/カラー選択肢を取得」でサイズ一覧を取得し、監視したいサイズを選択
3. 追加して監視開始

### ⚠️ variantId について（重要）

楽天はサイズごとに異なる `variantId` で在庫を管理します。**正確なサイズ監視には、URL に `?variantId=XXXXX` を含めてください。**

- 楽天の商品ページで目的のサイズを選ぶと、URL に `?variantId=` が付与されます。その URL をそのまま登録してください。
- variantId 無しの URL では、代表バリアントの在庫のみ判定できる場合があります。

### Playwright が未インストールのとき

楽天商品の監視時のみ Playwright が必要です。未インストールでも Webike / Yahoo! は動作し、楽天商品は `UNKNOWN`（未確認）になります。

```bash
pip install playwright
playwright install chromium
```

---

## 🔒 セキュリティ上の注意

- パスワードは `werkzeug.security` でハッシュ化して保存（平文では持ちません）
- Web UI は `0.0.0.0:8080` で公開します。**必ず強いパスワード** を設定してください
- 本アプリは **HTTP（暗号化なし）** です。インターネット直接公開ではなく、VPN / リバースプロキシ（HTTPS）の併用を推奨します
- Discord Webhook URL は秘密情報です。公開リポジトリには **含めない** でください（本リポジトリの `config.json` は初期値空です）

---

## 📝 ライセンス・免責

MIT License。Webike のサーバーに負荷をかけないよう、監視間隔は常識的な範囲（5分〜）で運用してください。スクレイピングは各サイトの利用規約に従ってください。
