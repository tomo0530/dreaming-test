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
