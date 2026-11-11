# dreaming-test

Anthropic「Components of agent memory」の Dreaming（セッション間にオフラインで transcript を再生し、メモを検証・統合する処理）が、同じモデル・同じ予算の Rolling Summary より、ドリフトする逐次タスクで効くかを検証する。Azure Foundry の `gpt-6-luna` を使い、API 費用の総額は $200 未満に収める。

## セットアップ

```bash
uv sync
cp .env.example .env   # AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT / HF_TOKEN を設定する
```

`.env` はコミットしない。実行はホストで `uv run` を使い、Docker は使わない。

## 開発

- `make test`: テストを実行する。有料 API を呼ぶ `integrate` マーカー付きテストは既定で除外される。
- `make all-lint`: format、型検査、lint をこの順に実行する。`make fix` で format と lint を自動修正できる。

## 実験の実行

```bash
uv run --env-file .env python scripts/run_experiment.py --config configs/experiments/<x>.yaml ...
```

実行パラメータの正本は `configs/`。結果は `outputs/<run_id>/` に出力され（git 管理外）、API 費用は `outputs/cost_ledger.sqlite` の台帳に集計される。

## 追試（Follow-up study）

採点フィードバックやメモ作成の強制などの処置を足した追試。設計は [`docs/followup/`](docs/followup/)、run の設定は `configs/experiments/followup/` にある。ストリームは run 間で共有するので、`fu_fb0.yaml` と `fu_null.yaml`（pilot は `fu_pilot_fb0.yaml` と `fu_pilot_null.yaml`）から一度だけ生成する。生成済みの ID があると `FileExistsError` で止まる（上書きしない）。

```bash
uv run python scripts/generate_streams.py --config configs/experiments/followup/fu_fb0.yaml --out data/streams
uv run --env-file .env python scripts/run_experiment.py --config configs/experiments/followup/fu_pilot_fb0.yaml --streams data/streams
# pilot の 5 run（fb0, fb1, verify, probe, null）がそろったら gate G0-G7 を判定する（不合格なら終了コード 1）
uv run python scripts/analyze_followup.py --gate --out outputs/followup-gate --streams data/streams \
  --run fb0=<pilot_fb0_dir> --run fb1=<pilot_fb1_dir> --run verify=<pilot_verify_dir> --run probe=<pilot_probe_dir> --run null=<pilot_null_dir> \
  --main-config fb0=configs/experiments/followup/fu_fb0.yaml --main-config fb1=configs/experiments/followup/fu_fb1.yaml \
  --main-config verify=configs/experiments/followup/fu_verify.yaml --main-config probe=configs/experiments/followup/fu_probe.yaml \
  --main-config null=configs/experiments/followup/fu_null.yaml
# 本番の解析。--run は fb0, fb1, verify, probe, null, sens、--clbench は前回と追試の CL-Bench run
uv run python scripts/analyze_followup.py --out outputs/followup-analysis --run fb0=<dir> --run fb1=<dir> --clbench <dir> --clbench <dir>
```

## ドキュメント

[`docs/plan/`](docs/plan/) に設計 [`00_design.md`](docs/plan/00_design.md)、実装計画 [`10_implementation-plan.md`](docs/plan/10_implementation-plan.md)、事前登録 [`20_preregistration.md`](docs/plan/20_preregistration.md) がある。背景調査は `docs/research/`、AI エージェント向けの開発規約は [`AGENTS.md`](AGENTS.md) を参照する。
