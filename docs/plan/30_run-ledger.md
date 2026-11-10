# 実行台帳（run ledger）

実行の記録を時系列で残す。費用は `outputs/cost_ledger.sqlite` が正本で、ここには要約を書く。

## 2026-11-10

### 疎通確認（phase `test`）
- integrate テスト `test_foundry_luna_roundtrip` を 2 回実行（Chat Completions 版と Responses 版）。費用は合計 $0.00001 未満。

### E1 dev（phase `dev`、run `dev-20261110T130643Z`、中断）
- ストリーム生成: dev / pilot / pilot_inj / pilot_rev / pilot_inj_rev の全ストリームが `validate_stream` を通過。`stream.json` の SHA-256 は各 run の `manifest.json` に記録される。
- 1 回目の実行（run `dev-20261110T130459Z`、削除済み）: `gpt-6-luna` が Chat Completions で function tools と `reasoning_effort` の併用を拒否（HTTP 400）。全ジョブが 0 セッションで失敗。費用 $0.007（予約の解放のみ、課金なし）。→ トランスポートを Responses API に変更（commit `fix: call gpt-6-luna via the Responses API`）。
- 2 回目の実行: A4 の analyzer フェーズで Azure のコンテンツフィルタによる 400 が 1 件発生しジョブが失敗。→ フィルタ拒否を「ループの停止」として扱う変更を入れて `--resume` で再開（commit `fix: treat content-filter rejections ...`）。
- R1 レビューの指摘でプロンプトと A1 の構成を変えるため、dev を途中で停止した（A0: 5 セッション、A1: 1〜3 セッション、A4: 1 セッション完了）。dev の費用は約 $0.10。
- 実測（機械的指標）: オンライン 1 問あたり約 $0.0007〜0.001、consolidation 1 回あたり約 $0.007。設計 §8.1 の見積（1 問 $0.01、1 回 $0.06）より 1 桁小さい。
- **手順からの逸脱**: dev のログに、問題ごとの採点区分とセッション別の正答数が INFO で出ていた。監視中に A1 のセッション 1 の正答数（6/6）などを目にした。プロンプトの調整にはこの情報を使っていない。再発防止として、採点に関するログを DEBUG に下げた（R1 修正 10）。

### R1 レビュー（実装一式）
- Opus（xhigh）と Codex gpt-6.1-sol（xhigh）が並行レビュー。指摘 16 項目を一括修正し、再レビューで両者承認。
- 最終ターンの提出ツール強制（`tool_choice`）が Responses API と reasoning の併用で通ることを integrate テスト `test_foundry_luna_forced_tool_choice` で確認。
- 中断した dev run `dev-20261110T130643Z` は旧形式（`injected_updated` なし）のため解析対象外として削除した。ストリームは新しい偽メモ形式で全て生成し直した。
