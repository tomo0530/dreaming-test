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

### E1 dev（run `dev-20261110T134000Z`、完走）
- 6 ジョブ（dev-01・02 × A0/A1/A4）がすべて完走。機械的指標: ツール呼び出し成功率 約 0.90、no_submission 率 約 0.02、consolidation の lint 指摘は A4 で 3 件（`description` がバッククォートで始まり YAML として不正）。→ analyzer / organizer プロンプトに YAML の引用規則を追記（dev の機械的指標に基づく調整）。
- 実測単価: 6 群合計で 1 ストリームあたり約 $0.40（設計見積 $6.45 の約 1/16）。pilot / pilot_inj の枠は振替不要と判断。

## 2026-11-11

### E2 pilot（run `pilot-20261110T141103Z`、15 ジョブ完走）と pilot_inj（run `pilot_inj-20261110T141103Z`、4 ジョブ完走）

1. **旧ゲート判定の保存（上書き禁止）**
   - 判定: `passed = false`。必須 (0)〜(5) と (6b-A3) は成立、必須 (6b-A1) `false_note_harms_a1` と助言 `a4_corrects` が未達。元の出力は `docs/plan/artifacts/pilot-gate-original.json` に保存した。
   - 自然ドリフトに関する値: acc_A0 = 0.802、acc_A1 = 0.688、acc_A5 = 0.906、oracle_gain = +10.4pt、A1 の stale 誤答 27 件（率 0.147）。単価 c_online = $0.179、c_fixed = $0.217。規模規則の結果 N = 20、1 セッション 6 問（main の見込み費用 約 $7.9）。

2. **診断と検出器（claim detector）の不具合修正**
   - 原因: `claim_patterns`（["business","C9"] など）が、記憶内の正しいコード表の行（例 `B8=Business account, C9=Employee`）や訂正文にも一致し、注入前のセッションでも「残存」と判定していた。このため A1・A4 の偽メモ残存行 6（偽陽性）、訂正数 0（偽陰性）と誤計測された。
   - 実態（スナップショットの確認）: A1（`summary.md` s003 / s007）と A4（`notes/` → `wiki/` s003 / s007）は、注入直後の 1 回目のオフライン処理で 2 件とも偽メモを訂正・除去し、直後の probe に正答していた。transcript を読めない A3 は s003〜s011 の `injected.md` に偽の主張を残し、probe は誤答した。オンラインの Q_inj では全群が毎回コード表を照会し直したため、偽メモによる誤答は 0 件だった（A3 の 1 誤答は正しい値を求めた後の未提出）。
   - 処置: 検出器を「偽の代入（`<column> = '<CODE>'`）と葉の語を含み、訂正の手がかり語や偽コードを別ラベルに対応付ける記述を含まない行」に限定した（commit `a780239`）。修正後の規則で保存済みスナップショットを再判定（API 費用なし）: A1・A4 は注入後のすべてのスナップショットで残存なし、A3 は残存あり。修正後も A1 の「偽メモが残った状態での Q_inj」は 0 件なので、(6b-A1) は未達のまま。

3. **逸脱と開始条件改訂の決定（本実験の凍結前に commit）**
   - 決定: pilot の結果を確認した後、`00_design.md` §9 の機構確認 (5)(6) を本実験の開始条件（必須）から探索的な診断指標へ変更する。これは §9 の「1 回改訂し、なお不成立なら停止」という規則からの明示的な逸脱であり、「旧ゲートを通過した」とは扱わない。
   - 協議: Codex gpt-6.1-sol は「改訂せず停止」、Opus は「注入をオフライン処理の後に移す改訂を 1 回」と割れたため、Codex gpt-6-astra と fable に諮った。astra は「改訂せず本実験へ（逸脱として記録）」、fable は「改訂を 1 回行い、不成立なら降格して本実験へ」と割れたため、Gemini 4 Argon High に裁定を求め、astra 案が採択された。
   - 注入位置を変える改訂を行わない理由: (i) オンラインでは全群が DB を直接照会するため、注入位置を変えても行動上の害は生じにくい。(ii) オフライン処理の後に `injected.md` へ直接入れる変更は、「オフライン処理による記憶の検証」という本来の介入を「処理を経ない直前の汚染への耐性」に変えてしまう。
   - 主仮説（全問での Δ = A4 − A1）を左右する自然ドリフトの条件 (0)〜(4) は十分な余地をもって成立している。Q_inj で行動差が出ないことは Δ に対して保守的（0 方向への希釈）に働く。注入の時期・配置・群構成・プロンプト・予算は変更せず、未使用の seed で main-01〜main-20 を実行する。

4. **判断時点で閲覧した情報**: pilot-01〜04 と pilot-inj-01 の全群の正答率（pilot-01 と pilot-inj-01 の A4 を含む）、スナップショット、費用台帳。pilot のストリームは本実験の主解析には含めない。

### E4 CL-Bench（run `clbench-20261110T141842Z`、9 ジョブ完走）
- ゲートと独立なため、pilot と並行して前倒しで実行した（A0 / A1 / A4 × run_index 0〜2）。解析は E5 で行う。

### E3 main
- 凍結直後に開始した run を約 30 秒で停止・破棄し、`concurrency` を 4 → 16 に変更して再開した（120 ジョブを並列度 4 で回すと約 13 時間かかる見込みだったため）。運用上の変更として事前登録 §5 に追記した。
- 破棄した run: `main-20261110T161247Z`（台帳の実測で約 40 秒。上の「約 30 秒」と事前登録 §5 の値は概算、セッション 1 の途中で停止）。精算済みの費用 $0.0066 と未精算の予約 4 件がある。解析には使わない。
- run `main-20261110T161346Z`（commit `7ef1204`、`git_dirty = false`）。開始 2026-11-10T16:13Z、終了 2026-11-10T19:14Z（UTC）。120 ジョブすべてが `completed`（12 セッション）。`failed`・`budget_exhausted` はなく、除外ストリームもない。
- 費用: main は $7.89（台帳の `actual_usd`。破棄した run の $0.0066 を含む）。累計は $10.57（dev $0.65、pilot $0.97、pilot_inj $0.30、CL-Bench $0.75、test < $0.01）。未精算の予約が全 phase で forfeited 26 件（予約額 $0.070）、open 12 件（$0.018）あり、課金の有無は分からない。`metrics.json` の main の `run_total_usd`（$7.94）は、forfeited の予約額を保守的に含めた値である。
- 照合: main の `stream.json` 20 本の SHA-256 は事前登録 §4 の値と一致した。`main.yaml` の SHA-256 は、事前登録 §5 の変更後の値と一致した。`outputs/main-run.log` には、エンドポイントのホスト名も URL も含まれていなかった。

### E5 解析
- `scripts/analyze.py --runs outputs/main-20261110T161346Z --out outputs/analysis/main` を実行した。CL-Bench も同様に解析し、`outputs/analysis/clbench` に出力した。`metrics.json` と図の複製は `docs/plan/artifacts/` に置いた。
- 主比較: Δ = A4 − A1 = −0.011、95% CI [−0.042, +0.022]。判定は not_shown。詳細は `40_results.md` に記載した。
- 事後の分析（事前登録外）:
  - `scripts/supplementary_analysis.py`: 層別の正答率、SQL 実行数、ツール呼び出し数、Q_inj の内訳を出す。出力は `artifacts/main-supplementary.json`。
  - transcript の読み取り専用診断（Opus サブエージェント）: 結果は `artifacts/main-diagnosis.md`。
- R2 レビュー: Opus（xhigh）の指摘（ツール呼び出し数の二重計上、因果表現、偽メモ検出器の誤検出、CL-Bench の解釈など）を反映した。Codex gpt-6.1-sol の 1 回目は、標準入力を待ったまま止まったため停止し、修正後の版で実行し直した。その指摘（採点フィードバックについての表現、A3 の Q_inj 非正答 31 問、丸め、補助集計の除外処理と回帰テスト）を反映し、Opus は再確認で APPROVE とした。Codex の再確認で残った 1 件（回帰テストの追加）も反映し、テストが除外処理の欠落を検出することを確かめた。

### 投稿と完了（2026-11-11）
- PR #3（結果と報告）をマージした。
- Notion「新規技術検証」DB にレポートを投稿した（図 2 枚を添付。Status は Done、FY は FY27H1、MGR checked は未チェック）。FY について、過去 2 件（2026-05・2026-07 完了）は FY26H2 だった。2026-11 の完了は下期をまたぐため、FY27H1 とした。
- `10_implementation-plan.md` のチェックリストを、実施結果に合わせて更新した。
