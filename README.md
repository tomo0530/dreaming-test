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

## ドキュメント

[`docs/plan/`](docs/plan/) に設計 [`00_design.md`](docs/plan/00_design.md)、実装計画 [`10_implementation-plan.md`](docs/plan/10_implementation-plan.md)、事前登録 [`20_preregistration.md`](docs/plan/20_preregistration.md) がある。背景調査は `docs/research/`、AI エージェント向けの開発規約は [`AGENTS.md`](AGENTS.md) を参照する。
