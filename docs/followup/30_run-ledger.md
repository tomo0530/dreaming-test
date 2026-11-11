# 追試 実行記録（run ledger）

- 追試（`docs/followup/`）の実行を、日付ごとに時系列で記録する。
- 費用の正本は台帳 `outputs/cost_ledger.sqlite`（前回と共有。累計の絶対上限 $200）。追試の phase は `fu_dev`、`fu_pilot`、`fu_pilot_rev`、`fu_main`、`fu_aux`、`fu_clbench`。
- 追試開始時点の累計費用: $11.94（前回の全 phase の合計）。

## 2026-11-11

### 実装（T0〜T6）

- ブランチ `feature/followup-harness`。commit `2e2cb3f`（T0）、`58a4821`（T1〜T5）、`6182ecf`（T6）。
- `make test` は 1005 件成功、`make all-lint` は 0 件。
- 前回の 7 run の manifest は、新しいコードでも `matches_config` ですべて一致することを確認した（`pilot_rev` は、意図どおり旧 `d71c2f4` 形式の経路で一致）。

### E1 準備

- dev ストリームを生成した。3 本とも検証を通った（問題 0 件）。
  - `fu-dev-01`（seed 4201）: drift は join_key・soft_delete（s5）、stale_table・status_code（s9）
  - `fu-dev-02`（seed 4202）: drift は join_key・soft_delete（s5）、column_name・status_code（s9）
  - `fu-dev-null-01`（seed 4211）: migration なし

### E1 dev

- run `fu_dev-20261111T024710Z`（全スイッチ有効、10 ジョブ）と `fu_dev_null-20261111T024710Z`（2 ジョブ）。12 ジョブすべて完走し、traceback は 0 件。tool のエラーは 1 セッションあたり 0〜3 件（呼び出し 20〜34 回中）。
- 機械的な指標（群別の正答率は見ていない）:
  - G2 相当: fb1 で採点 720 問すべてに `EVENT feedback:` が 1 行ずつあり、transcript と結果行が一致。drift なしの run（feedback なし）では 0 行。
  - G3 相当: A1・A4 とも `note_written` 率 100%（各 216 問）。メモのあるセッションの統合 33 件すべてで、生きているメモを読んでいる。
  - G4 相当: A2 のセッション 2 以降 132 問で `retrieved_tokens > 0` が 100%。
  - G5 相当: `no_submission` 率は各群 0〜3.5%。
  - **G7 相当: 不合格**。A4 の統合 22 件で、analyzer は `query` を 1 回も実行しなかった。tool は渡っていた（ログに `with query`）、実 DB への照会も直接呼び出しで動くことを確認した。原因は `consolidator_sql_addendum.md` の照会指示が条件付きの推奨にとどまり、analyzer の手順に組み込まれていなかったこと。
- 修正（commit `1f6c6ce`、設計 §7 が dev で認める「統合役の断片の文面」の範囲）: 断片に「Step 3: query the current values」を加え、検証の前に 1 回以上照会する手順として明示した。閾値は変えていない。
- 再確認 run `fu_dev_probe`（A4 のみ、dev ストリーム 2 本、phase `fu_dev`）: run `fu_dev_probe-20261111T042052Z`、2 ジョブとも完走。統合 22 件すべてで照会を 1 回以上実行した（1 回が 18 件、2 回が 2 件、3 回が 2 件）。G7 相当は 100%。
- E1 の費用（phase `fu_dev`）: 合計 $1.73（枠 $3）。
- 条件別の実測単価（1 ジョブあたり、ドル）: fb1 構成の A0 0.038、A1 0.170、A2 0.062、A4 0.203、A5 0.027。drift なしの A1 0.142、A4 0.188。この単価で粗く見積もると、`fu_main` は約 $50.6（係数 1.3 込み、枠 $52）、`fu_aux` は約 $20（枠 $25）。正式な判定は pilot の G6 で行う。

### E2 pilot（gate）

- 5 本を同時に実行: `fu_pilot_{fb0,fb1,verify,probe,null}-20261111T043206Z`（コード `1f6c6ce`）。全ジョブ完走、traceback 0 件。
- gate: **合格（G0〜G7 すべて）**。結果は `docs/followup/artifacts/followup-gate.json`。
  - G1: 53 ストリームすべて問題なし。G2: fb1 の採点 576 問すべてに feedback 行、fb0 は 0 行。G3: A1・A4 とも note 率 100%、メモ読み率 100%。G4: 100%（264 問）。G5: 最大は verify/A0 の 6.3%。G7: 22 件中 22 件で照会を実行（100%）。
  - G6: 見込み `fu_main` $47.64（枠 $52）、`fu_aux` $20.34（枠 $25）。
- 改訂（`fu_pilot_rev`）は不要。
- 費用: `fu_pilot` $3.38。累計 $16.95（絶対上限 $200）。

### E3 凍結

- 事前登録 §6 を埋めて凍結した（commit SHA は次の節に記す）。
