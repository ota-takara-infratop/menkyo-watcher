# menkyo-watcher

警視庁 仮免許 予約サイト（府中・鮫洲）で 10/15〜10/31 に空きが出たら、スマホに通知します。
GitHub Actions 上で1分ごとに確認するので、PCを起動しておく必要はありません。
（1回の実行が約5時間40分ループし、終わるとすぐ次の実行が始まります）

## セットアップ

1. **スマホに ntfy アプリを入れる**（iOS / Android、無料・登録不要）
   - アプリで「+」→ 他人に推測されにくいトピック名で購読（例: `menkyo-a8f3k2x9q`）
2. **GitHub にこのフォルダを push する**（public リポジトリ推奨。private だと無料枠を超えます）
3. リポジトリの **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `NTFY_TOPIC` / Value: 1で決めたトピック名
4. **Actions タブ → check-vacancy → Run workflow** で手動実行し、成功するか確認

## 動作

- 新しく空いた枠だけを通知します（同じ枠で何度も通知されません）
- 通知をタップすると予約サイトが開きます
- 監視範囲や試験場は `.github/workflows/check.yml` の `DATE_FROM` / `DATE_TO` / `PLACES`（270=府中, 280=鮫洲）で変更できます
- 確認間隔は同じファイルの `INTERVAL_SECONDS` で変更できます（相手サイトの負荷を考え60秒以上を推奨）
- 予約が取れたら Actions タブで workflow を Disable してください

## ローカルで試す

```
python check.py
```

## iPhone 自動入力スクリプト（karimen-autofill.user.js）

通知 → 利用規約に同意 のあと、「仮免許学科試験」選択・予約者情報入力・鮫洲の選択までを自動で進め、カレンダーを表示します。
日付・時間の選択と最終確定は手動です。個人情報はスマホ内のスクリプトにだけ保存し、このリポジトリには入れません。
