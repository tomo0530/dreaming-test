# Dreaming 技術検証のための独立文献・実装調査

**調査基準日：2026-11-10**

本報告は `docs/research/00-brief.md` を前提に、追加の一次資料を調査したものである。Anthropic の既出結果や Google SLEEP の再要約は避け、検証設計に影響する追加研究、反証、公開実装、利用条件を中心に整理した。実験・モデルデプロイ・ファイル変更は行っていない。

表中の結果は、特記しない限り**著者報告値であり、本調査での再現値ではない**。「推論」「提案」は調査から導いた判断、「未確認」は採用可否を確定できない事項を表す。

---

## 1. エグゼクティブサマリー

- **最初に検証すべきなのは、重み固定の外部メモリ consolidation である。** 2026年には SimpleMem、Auto-Dreamer、MEMOIR、WikiSkill など直接関連する研究がある。M4 16GB＋Azure API で扱いやすい一方、SLEEP 型の重み更新とは別実験にすべきである。[SimpleMem](https://arxiv.org/html/2601.02553v1)、[Auto-Dreamer](https://arxiv.org/html/2605.20616v1)

- **consolidation は常に改善するわけではない。** SimpleMem には positive ablation があるが、LightMem は offline update 後に精度が低下する設定を報告している。2026年の *Useful Memories Become Faulty…* は、継続的な書き換えそのものによる退行を示す。[LightMem](https://arxiv.org/html/2510.18866v1)、[Memory-Collapse](https://arxiv.org/html/2605.12978v2)

- **主比較は「通常要約 vs. 根拠再検証を伴う整理」に置くのがよい。** no-memory だけでは、検索・要約・追加計算の効果と Dreaming 固有の効果が混ざる。固定 memory bank の整理前後比較と、逐次タスク stream の両方が必要である。Auto-Dreamer はこの二つを分離している。[Auto-Dreamer §5.3–5.4](https://arxiv.org/html/2605.20616v1#S5.SS3)

- **公開・ライセンス・費用のバランスでは LongMemEval-S が第一候補。** コードと公式 cleaned データは MIT。更新競合には MemoryAgentBench の FactConsolidation を追加できる。ただし後者は元データ由来の権利を subset ごとに確認する必要がある。[LongMemEval](https://github.com/xiaowu0162/LongMemEval)、[MemoryAgentBench データカード](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/raw/main/README.md)

- **CL-Bench は公開されているが、そのまま採用確定にはできない。** 対象は Berkeley 系 *Continual Learning Bench*。コードは Apache-2.0 だが、配布 DB のデータライセンスは未確認。また Anthropic の30問評価と、公開実装の40問 schema-drift 条件は同一ではない。[公式 repo](https://github.com/pgasawa/continual-learning-bench)、[DB README](https://raw.githubusercontent.com/pgasawa/continual-learning-bench/main/src/tasks/database_exploration/README.md)

- **ライセンス制約はベンチマーク選定を大きく変える。** LoCoMo は公式 LICENSE が CC BY-NC 4.0。STALE、MEME、Memory-Collapse の公開データも MIT／Apache／BSD ではないため、今回は研究上の参考とし、実験データには採用しない。[LoCoMo LICENSE](https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt)

- **正解率だけでは consolidation の品質を判断できない。** 訂正率と正→誤の退行率、日時・条件・出典の保持、stale memory 採用率、更新の波及、全 maintenance 費用を測るべきである。圧縮率向上と知識保持は別指標である。[MEMOIR](https://arxiv.org/html/2611.07631v1)、[State Contamination](https://arxiv.org/abs/2605.16746)

- **gpt-6-luna／sol／astra と 6.1 系は Azure Foundry の公式掲載を確認できた。** ただし公開 availability はユーザー subscription でのデプロイ成功を保証しない。料金は9月22日の Microsoft 公式公表値を取得できたが、11月10日の適用単価は未確定である。[Foundry catalog](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure?pivots=azure-direct-others)、[Azure 公式発表](https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/?trk=article-ssr-frontend-pulse_little-text-block)

- **ローカルの parametric consolidation は、1–4B の小規模 QLoRA を探索実験にする範囲なら妥当。** MLX-LM は対応するが、SLEEP 全体の再現可能性や M4 16GB での所要時間は未検証。Qwen3-1.7B を使う replay／distillation 比較と、本来の SLEEP 再現を区別すべきである。[MLX-LM LoRA](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md)

---

## 2. 関連研究・実装の一覧表

ライセンス欄は原則として**コード**のライセンスである。論文の公開ライセンスはコードやデータの利用許諾を意味しない。2025年以前の項目は、指定された比較対象または基礎方式として掲載した。

### 2.1 2026年の直接関連研究

| name | 年月 | 種別 paper/repo | メモリ形式 | consolidation の有無と方式 | 主な結果 | ライセンス | URL |
|---|---|---|---|---|---|---|---|
| SimpleMem | 2026-01 | paper＋repo | 自立した facts/events、時刻、複数 index | **あり**。非同期 recursive consolidation、元情報を archive | consolidation ablation で平均 F1 +5.00pp | MIT | [paper](https://arxiv.org/html/2601.02553v1)／[repo](https://github.com/aiming-lab/SimpleMem) |
| EverMemOS | 2026-01 | paper＋repo | MemCell、MemScene、profile | **あり**。episode の clustering・profile 化。夜間専用ではない | profile consolidation の寄与あり | Apache-2.0 | [paper](https://arxiv.org/html/2601.02163v1)／[repo](https://github.com/EverMind-AI/EverMemOS) |
| Memento-Skills | 2026-03 | paper＋repo | SKILL.md、scripts、prompts | **あり**。Dream Daemon。LLM 本体固定だが router の offline 学習あり | Daemon 単独 ablation は未確認 | Apache-2.0 | [paper](https://arxiv.org/abs/2603.18743)／[repo](https://github.com/Memento-Teams/Memento-Skills) |
| SleepGate | 2026-03 | paper | KV cache、temporal tags | **あり**。学習した gate による eviction・圧縮 | 793K parameter の合成課題。実用 LLM の結果ではない | 公開コード未確認 | [paper](https://arxiv.org/abs/2603.14517) |
| SCM | 2026-04 | paper | working memory＋concept graph | **あり**。NREM replay、REM random walk、forgetting | NREM 除去で22/22→20/22。REM の recall 利得なし | 公開コード未確認 | [paper](https://arxiv.org/html/2604.20943v1) |
| Auto-Dreamer | 2026-05 | paper | typed memory bank＋source trajectories | **あり**。offline 領域置換。consolidator を GRPO 学習 | writer-only／untrained／trained を比較 | 公開コード未確認 | [paper](https://arxiv.org/html/2605.20616v1) |
| Self-Consolidating Language Models：SCoL | 2026-05 | paper | LoRA を merge した重み | **あり**。更新 layer の選択を meta-RL 学習 | retention 改善。ただし長文では Batch TTT に劣る | 公開コード未確認 | [paper](https://arxiv.org/html/2605.07076v2) |
| Do Language Models Need Sleep? | 2026-05 | paper | SSM fast weights | **あり**。offline recurrent forward passes | sleep 回数増加で深い推論課題を改善 | 公開コード未確認 | [paper](https://arxiv.org/abs/2605.26099) |
| WikiSkill | 2026-08 | paper | immutable traces→wiki→skills | **あり**。wiki 整理、skill 提案、検証・rollback | wiki を実行 agent にも渡すと悪化する条件あり | 公開コード未確認 | [paper](https://arxiv.org/html/2608.27454v1) |
| CoDe-LoRA | 2026-10 | paper＋repo | task-specific／shared LoRA | **あり**。知識共通化と分離、replay-free | 4 backbones・3 CL benchmarks | Apache-2.0 | [paper](https://arxiv.org/abs/2610.08312)／[repo](https://github.com/Estrellajer/CoDe-LoRA) |
| Cortex | 2026-10 | paper | low-rank／structural FFN updates | **あり**。予想 query を作り context-conditioned 出力を蒸留 | 14B・50逐次編集で gain 回収88.9%、非対象性能保持99.8% | 公開コード未確認 | [paper](https://arxiv.org/abs/2610.20399) |
| Learning by Doing：Zep Agent Skills | 2026-11 | paper／service | verified traces→versioned SKILL.md | **あり**。Reflector／Curator 分離、非同期 compile | 強い ALFWorld baseline に +8.2pp | service 実装の公開許諾未確認 | [paper](https://arxiv.org/html/2611.01386v1) |
| MEMOIR | 2026-11 | paper＋repo | text store＋credit／importance | **あり**。periodic merge＋preservation certificate | recall ほぼ維持、active store 1.42%減 | MIT | [paper](https://arxiv.org/html/2611.07631v1)／[repo](https://github.com/AnandMayank/MEMOIR) |

### 2.2 基礎方式・主要 OSS

| name | 年月 | 種別 paper/repo | メモリ形式 | consolidation の有無と方式 | 主な結果 | ライセンス | URL |
|---|---|---|---|---|---|---|---|
| Letta Sleep-time Compute | 2025-04 | paper＋repo | query 前に加工する context／memory | **あり**。query 到来前の推論・整理、重み固定 | 一部課題で同精度の test-time compute を約1/5 | 研究 repo MIT、Letta 本体 Apache-2.0 | [paper](https://arxiv.org/html/2504.13171v1)／[研究 repo](https://github.com/letta-ai/sleep-time-compute)／[Letta](https://github.com/letta-ai/letta) |
| A-MEM | 2025-02 | paper＋repo | Zettelkasten notes、tags、links | **あり。ただし主に online**。新規 note による既存 memory evolution | evolution ablation あり | MIT | [paper](https://arxiv.org/html/2502.12110v1)／[repo](https://github.com/agiresearch/A-mem) |
| Mem0／Mem0g | 2025-04 | paper＋repo | facts／entity-relation graph | 主に write-time の ADD／UPDATE／DELETE | graph 追加で全 category が改善するわけではない | Apache-2.0 | [paper](https://arxiv.org/html/2504.19413v1)／[repo](https://github.com/mem0ai/mem0) |
| Zep／Graphiti | 2025-01 | paper＋repo | episodes、temporal KG、community summaries | incremental 更新・bi-temporal invalidation。sleep と同一ではない | LongMemEval の全体改善。整理単独 ablation ではない | Graphiti Apache-2.0 | [paper](https://arxiv.org/html/2501.13956v1)／[repo](https://github.com/getzep/graphiti) |
| MemOS | 2025-07、12改訂 | paper＋repo | plaintext／activation／parameter MemCube | lifecycle・scheduler。parametric memory も含む | 全体比較の改善であり sleep 単独効果ではない | Apache-2.0 | [paper](https://arxiv.org/html/2507.03724v4)／[repo](https://github.com/MemTensor/MemOS) |
| MemoryOS | 2025-05 | paper＋repo | STM／MTM／long-term persona | heat-based promotion、主に online | LoCoMo 全体比較 | Apache-2.0 | [paper](https://arxiv.org/html/2506.06326v1)／[repo](https://github.com/BAI-LAB/MemoryOS) |
| LangMem | 2025-02 | docs＋repo | semantic collections、profile、episodes、prompt | **background manager あり**。抽出・統合・更新 | consolidation 単独の定量評価は未確認 | MIT | [公式発表](https://www.langchain.com/blog/langmem-sdk-launch)／[repo](https://github.com/langchain-ai/langmem) |
| Cognee | 2026-11確認 | docs＋repo | graph＋vector＋session lessons | post-session distillation・feedback 更新 | 現行 `improve()`。旧 `memify()` と区別が必要 | Apache-2.0 | [公式 docs](https://docs.cognee.ai/core-concepts/main-operations/improve)／[repo](https://github.com/topoteretes/cognee) |
| LightMem | 2025-10 | paper＋repo | sensory／short／long-term memory | **offline parallel sleep-time update** | コスト削減。ただし update 後の精度悪化もある | MIT | [paper](https://arxiv.org/html/2510.18866v1)／[repo](https://github.com/zjunlp/LightMem) |
| Memento | 2025-08 | paper＋repo | episodic case memory | online memory-based RL。LLM 本体固定 | case memory の寄与。Dream Daemon の評価ではない | MIT | [paper](https://arxiv.org/abs/2508.16153)／[repo](https://github.com/Memento-Teams/Memento) |
| Reflexion | 2023-03 | paper＋repo | verbal reflection buffer | trial 間の reflection。夜間専用ではない | verbal feedback の全体効果 | MIT | [paper](https://arxiv.org/abs/2303.11366)／[repo](https://github.com/noahshinn/reflexion) |
| ExpeL | 2023-08 | paper＋repo | trajectories＋自然言語 insights | **offline insight extraction**、重み固定 | retrieve-only に対する insights の寄与 | Apache-2.0 | [paper](https://arxiv.org/html/2308.10144v3)／[repo](https://github.com/LeapLabTHU/ExpeL) |
| Agent Workflow Memory | 2024-09 | paper＋repo | 再利用可能 workflow | offline／online の workflow induction | Web task の全体改善 | Apache-2.0 | [paper](https://arxiv.org/abs/2409.07429)／[repo](https://github.com/zorazrw/agent-workflow-memory) |

---

## 3. 各トピックの詳細

### 3.1 外部メモリの offline／background consolidation

#### 「メモリ更新」と「Dreaming」を同一視しない

調査対象は少なくとも次の四群に分かれる。

| 群 | 代表例 | 検証上の意味 |
|---|---|---|
| 新規入力時の局所更新 | A-MEM、Mem0、Graphiti、MemoryOS | online memory baseline。offline replay の効果は分離されていない |
| 複数セッション後の整理 | LightMem、SimpleMem、LangMem、Auto-Dreamer | Anthropic 型 Dreaming に近い比較対象 |
| 経験から手順・skill への昇格 | ExpeL、AWM、Memento-Skills、WikiSkill、Zep Agent Skills | factual memory だけでなく procedural transfer を評価する必要 |
| 保存・merge 自体の検証 | MEMOIR、WikiSkill の validation gate | 誤統合、情報損失、退行の防止に直接関係する |

これは本調査による分類である。各方式の処理根拠は、[SimpleMem](https://arxiv.org/html/2601.02553v1)、[Auto-Dreamer](https://arxiv.org/html/2605.20616v1)、[WikiSkill](https://arxiv.org/html/2608.27454v1)、[MEMOIR](https://arxiv.org/html/2611.07631v1) にある。

#### 実装として特に参考になるもの

- **SimpleMem：MIT。** 日時・参照対象を解決した memory unit と background consolidation を持つため、検索だけの baseline と整理処理を分けやすい。ただし論文評価の LoCoMo データは今回のライセンス条件に適合しない。コードの利用と評価データの利用を分離する。[LICENSE](https://raw.githubusercontent.com/aiming-lab/SimpleMem/main/LICENSE)

- **LangMem：MIT。** hot-path と background manager が明示され、Azure LLM を差し替える実装候補になる。公式資料で確認できたのは機構であり、Dreaming 単独の優越性ではない。[公式 docs](https://langchain-ai.github.io/langmem/)、[LICENSE](https://raw.githubusercontent.com/langchain-ai/langmem/main/LICENSE)

- **LightMem：MIT。** sleep-time update の比較対象として重要。圧縮・topic segmentation・更新を一括導入すると寄与が混ざるため、update の有無を切り替えて使うのが適切である。[paper](https://arxiv.org/html/2510.18866v1)、[LICENSE](https://raw.githubusercontent.com/zjunlp/LightMem/main/LICENSE)

- **Memento-Skills：Apache-2.0。** 現行コードには Dream Daemon がある。しかし論文全体の GAIA／HLE 改善を Daemon の効果と解釈できない。基盤 LLM が固定でも router の学習を含むため、「完全 training-free」と一括表示しない。[consolidator 実装](https://raw.githubusercontent.com/Memento-Teams/Memento-Skills/main/daemon/dream/consolidator.py)、[LICENSE](https://raw.githubusercontent.com/Memento-Teams/Memento-Skills/main/LICENSE)

- **MEMOIR：MIT。** merge 前後で保持すべき情報を検査する設計が参考になる。ただし報告された圧縮は小さく、100 merge の無損失監査を「情報損失が起きない保証」とは扱えない。[paper](https://arxiv.org/html/2611.07631v1)、[LICENSE](https://github.com/AnandMayank/MEMOIR/blob/main/LICENSE)

#### 商用機能と OSS を分ける必要がある

Mem0 は2026年8月4日に、merge／supersede／synthesize を行う **Dream** を公表している。記事上では Pro／Enterprise 向けであり、OSS Mem0 の Apache-2.0 から hosted Dream の実装公開は導けない。また superseded memory は既定検索に残り、現行情報だけにするには `latest_only=true` が必要と説明されている。[Mem0 公式記事、9月28日更新](https://mem0.ai/blog/dream-background-memory-consolidation-for-ai-agents)

同様に、**Graphiti の Apache-2.0 は Zep 商用サービス全体や Agent Skills の許諾ではない**。Graphiti 自体を採用する場合も、graph backend のライセンスと運用負荷を別途確認する必要がある。[Graphiti LICENSE](https://raw.githubusercontent.com/getzep/graphiti/main/LICENSE)、[Zep Agent Skills 論文](https://arxiv.org/html/2611.01386v1)

Cognee は現行 API に注意が必要である。公式資料では旧 `memify()` が deprecated とされ、後継の `improve()` に session distillation、feedback、fact conflict 等が整理されている。古い記事の API をそのまま設計根拠にしない。[Cognee 公式 docs](https://docs.cognee.ai/core-concepts/main-operations/improve)

---

### 3.2 Parametric consolidation／continual learning と M4 16GB

#### 追加研究から分かること

| 研究 | SLEEP との関係 | 再現性・ローカル適合性 |
|---|---|---|
| SCoL、2026-05-08 | context を重みに書き、更新 layer の選択を学習 | 原実験は Qwen2.5-7B、96GB GPU。outer-loop 学習まで M4 で再現可能とはいえない |
| Offline Recurrence、2026-05-25 | sleep 中の反復処理で context を SSM fast weights に移す | 専用 architecture・学習が必要。通常の MLX LoRA と同じではない |
| CoDe-LoRA、2026-10-06 | shared knowledge と task-specific knowledge を分離 | Apache-2.0 コードあり。ただし MLX 移植・M4 実測は未確認 |
| Cortex、2026-10-14 | context-conditioned 出力を merge 可能な更新へ蒸留 | 4B から評価があるが、公開コード・ライセンス未確認 |
| Continual facts、2026-07-13、10月改訂 | 逐次書込み・merge による忘却を直接評価 | Qwen3-4B・rank16 LoRA の比較が小規模実験の参考になる |
| sleeping-llm、2026年公開実装 | MEMIT の短期編集を sleep 中に LoRA へ移す | MIT 表記。Mac 実績はあるが小規模・単発・RAG 比較なし |

出典：[SCoL](https://arxiv.org/html/2605.07076v2)、[Offline Recurrence](https://arxiv.org/abs/2605.26099)、[CoDe-LoRA](https://arxiv.org/abs/2610.08312)、[Cortex](https://arxiv.org/abs/2610.20399)、[Continual facts](https://arxiv.org/html/2607.11020v3)、[sleeping-llm](https://github.com/vbario/sleeping-llm)

**重要な negative result：** *Can a Language Model Learn Facts Continually in Its Weights?* は、20回の逐次書込み後、以前の事実への正答率が repeated statements では **1%**、多様な言い換え例では **46%** と報告する。同じ varied prompts でも distillation は SFT を上回らず、LoRA の直交化も万能ではなかった。これは「重みに入れば安定した長期記憶になる」という前提への反証である。[2026-10-05版](https://arxiv.org/html/2607.11020v3)

SCoL も一方的な成功例ではない。長い条件では SCoL **37.0%** に対し、全文へ同時アクセスする Batch TTT は **41.5%**。逐次適応と batch 適応では利用可能情報が違うため、強い上限対照として batch 条件を残す価値がある。[SCoL Table 12](https://arxiv.org/html/2605.07076v2)

#### 小型モデルなら SLEEP を再現できるか

**事実：** SLEEP 論文には Llama-3.2-1B の小規模 ARC 実験がある。ただし選別された train 11課題・held-out 8課題で、dream の生成・棄却・複数適応を含む。1B というパラメータ数だけを根拠に M4 16GB で容易と判断できない。今回、公式実験コードの配布・ライセンスは確認できなかった。[SLEEP 本文・付録](https://arxiv.org/html/2606.03979v2)

さらに Llama の独自ライセンスは今回の MIT／Apache／BSD 限定条件には入らない。したがって Qwen 系へ置換する場合は**縮小追試・方式検証**であり、原論文の厳密再現ではない。

**推論：現実的なローカル探索範囲**

- Qwen3-1.7B を第一候補、4B を追加比較。
- MLX-LM の QLoRA、batch 1、短い sequence、少数 LoRA layers、gradient checkpointing。
- 比較は「逐次 SFT」「過去事実 replay 付き SFT」「context distillation」「更新なし＋RAG」。
- 取得精度だけでなく、以前の事実、非対象能力、訂正・撤回後の挙動を測定。
- RL で dream generator／layer selector 自体を学習する実験は別枠。

MLX-LM はこれらの省メモリ設定を公式に説明しているが、**上記構成の M4 16GB での収容性・速度は未測定**である。[MLX-LM LoRA docs](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md)

`sleeping-llm` は MacBook Air M3 8GB、3B 4bit、15 facts で recall 0.60 を報告する。しかし単発実験、合成 person-city facts、RAG 比較なしという限界も自ら記載している。ローカル動作の参考にはなるが、一般的な consolidation の有効性の証拠には不足する。[公式 README](https://github.com/vbario/sleeping-llm)

---

### 3.3 ベンチマークと評価方法

#### 主要 benchmark

| benchmark／初出 | 規模・測るもの | 公開状況・ライセンス | runtime／費用・local＋API 適合性 |
|---|---|---|---|
| **LongMemEval**、2024-10、ICLR 2025 | 500問。S：約115K tokens／問。M：約1.5M tokens・500 sessions／問。時間、更新、複数 session、abstention | [コード MIT](https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/LICENSE)、[公式 cleaned データ MIT](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) | **第一候補**。S を層化抽出可能。総ドル・runtime の再現可能な確定値は未確認 |
| **LongMemEval-V2**、2026-05-12 | 451問、1,870 trajectories。最大500 trajectories／115M tokens の履歴。workflow、gotchas、動的状態 | [コード Apache-2.0](https://github.com/xiaowu0162/LongMemEval-V2)、[データ Apache-2.0](https://huggingface.co/datasets/xiaowu0162/longmemeval-v2)、約7.12GB | **第二段階向け**。Small tier、local endpoint 対応。著者の query latency は方式間0.1–177.2秒だが ingestion を含まない |
| **LoCoMo**、2024-02 | 論文全体50会話・7,512 QA。現行公開ファイルは10会話 | [公式 LICENSE は CC BY-NC 4.0](https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt) | 小さく実行しやすいが**今回不採用**。公開 subset と論文全体の規模を混同しない |
| **MemoryAgentBench**、2025-07、ICLR 2026 | 論文版130 sequences・2,071 QA。現行 HF は146 rows。retrieval、test-time learning、理解、conflict | [コード MIT](https://github.com/HUST-AI-HYZ/MemoryAgentBench)、[データカード MIT](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/raw/main/README.md)。元データの条件は別確認 | **FactConsolidation SH／MH が有望**。論文版は各1 sequence・100 QA、約262K context。inject-once/query-many で償却可能。総費用未確認 |
| **MemBench**、2025-06-20 | factual／reflective、participation／observation。約53K QA、500 user graphs | [公式 repo・配布リンク公開](https://github.com/import-myself/Membench)。コード／データ LICENSE 未確認 | 短い sampled subset はあるが**採用保留**。operation 時間と全体 API 費用は別 |
| **Evo-Memory**、2025-11-25 | 10 source datasets を continual streams 化。search→synthesis→evolve | [論文公開](https://arxiv.org/html/2511.20857v1)。公式固定 stream、コード、LICENSE を確認できず | 評価設計の参考。報告された steps 削減は wall-clock／ドル削減ではない |
| **CL-Bench 1.0**、2026-06-04 | 6 domains。DB は30問または40問 drift schedule | [コード Apache-2.0](https://github.com/pgasawa/continual-learning-bench)。配布 DB の data LICENSE 未確認 | DB40問の著者費用 **$0.4–$8.8／run**。今回のモデル・単価への保証ではない。データ条件解決まで採用保留 |

一次論文：[LongMemEval](https://arxiv.org/abs/2410.10813)、[LongMemEval-V2](https://arxiv.org/abs/2605.12493)、[LoCoMo](https://arxiv.org/abs/2402.17753)、[MemoryAgentBench](https://arxiv.org/abs/2507.05257)、[MemBench](https://arxiv.org/abs/2506.21605)、[CL-Bench](https://arxiv.org/abs/2606.05661)

#### CL-Bench の正体・公開性

Anthropic が参照するのは Berkeley 系の **Continual Learning Bench** である。Tencent の *CL-bench: A Benchmark for Context Learning* などの同名 benchmark とは区別する。[Berkeley Sky Lab](https://sky.cs.berkeley.edu/project/continual-learning-bench/)

公開 DB task の schedule は以下である。

- `multi_group`：元の DB に対する30問。
- `schema_drift`：20問の後に migration、さらに20問。
- 現行 default は `schema_drift`。
- reward は正答だけでなく exploratory SQL queries による regret を含む。

したがって、**「正答率」と「reward」は同じ指標ではない**。また Anthropic の図は30問であり、公開 default の40問 drift 実験を再現したとは確認できない。[公式 DB README](https://raw.githubusercontent.com/pgasawa/continual-learning-bench/main/src/tasks/database_exploration/README.md)、[Anthropic 原文](https://www.anthropic.com/research/components-of-agent-memory)

DB は公開 HF から取得できるが dataset card／LICENSE を確認できず、元データは Amazon Reviews 2023 と明記されている。**コードの Apache-2.0 を DB データへ適用してはいけない。**[配布 DB](https://huggingface.co/datasets/continual-learning-bench/database-exploration)

#### 2026年の staleness／drift／consolidation 評価

| benchmark／日付 | 規模・評価の焦点 | 利用条件・費用・適合性 |
|---|---|---|
| **STALE**、05-07 | 400 scenarios、1,200 probes、最大150K context。暗黙の conflict、stale premise、policy adaptation | [コード MIT](https://github.com/icedreamc/STALE)、[データ CC BY 4.0](https://huggingface.co/datasets/STALEproj/STALE)。今回はデータ不採用。総費用未確認 |
| **MEME**、05-12 | 100 episodes。32K filler、128K stress 40ep。Tracking、Deletion、Cascade、Absence | [コード MIT](https://github.com/SeokwonJung-Jay/MEME-public)、[データ CC BY 4.0](https://huggingface.co/datasets/meme-benchmark/MEME)。今回はデータ不採用。ingest／inference 費用を分離する方法が参考 |
| **MemEvoBench**、04-17 | 108 QA cases、workflow 83 cases。3 rounds の書戻し、biased feedback、noise | [コード・データ公開](https://github.com/xiewwee11/MemEvoBench)、LICENSE 未確認。小規模に向くが採用保留 |
| **AuthMem-Bench**、08-03 | 350 pairs／700 histories。authority・適用制約が整理で消えるか | [論文公開](https://arxiv.org/abs/2608.01679)。公式 artifact／LICENSE 未確認。consolidation の意味保存に直接関係 |
| **ERRAND**、09-02 | 26 consolidated items、2,000 steps／run、4 drift schedules。再検証予算 | [論文公開](https://arxiv.org/abs/2609.29545)。公開 repo／LICENSE 未確認。著者実験約900 GPU-hoursで軽量初手には不向き |
| **ConsolidationBench**、09-26 | retention、compression、composite reuse、rollback／provenance | [本文](https://arxiv.org/html/2609.33013v1)は generator 等を controlled release と記載。公開再現 suite としては扱えない |
| **MemoryDojo**、11-06 | 155 trajectories、4 domains、5 memory systems、2,162 task sessions。poison 保存→検索→行動 | [本文](https://arxiv.org/html/2611.09721v1)はコード公開予定と記載。公開コード／データ／LICENSE 未確認。総費用未確認 |

関連論文：[STALE](https://arxiv.org/abs/2605.06527)、[MEME](https://arxiv.org/abs/2605.12477)、[MemEvoBench](https://arxiv.org/abs/2604.15774)

MEME の公式評価ページは、例として MD-flat **$0.04 ingestion＋$0.01 inference／episode**、Opus4.7 internal の20ep subset **$3.87＋$0.66／episode** を報告している。比較条件は異なるが、**回答費用だけを測ると memory maintenance の大部分を落とす**ことを示す。[MEME 公式評価](https://seokwonjung-jay.github.io/meme-eval/)

2025年の **HaluMem** も extraction→update→QA の error propagation を分ける点が参考になる。ただしコード・データは CC BY-NC-ND 4.0 で、今回の採用条件外である。[公式 repo](https://github.com/MemTensor/HaluMem)、[LICENSE](https://raw.githubusercontent.com/MemTensor/HaluMem/main/LICENSE.txt)

#### コスト算定の出発点

LongMemEval-S の平均115K tokens を使うと、full-context reader の履歴入力だけで次になる。

| 評価規模 | 1 arm あたりの履歴入力 |
|---|---:|
| S 50問 | 約5.75M tokens |
| S 500問 | 約57.5M tokens |
| M 500問 | 約750M tokens |

出力、judge、memory construction、consolidation、embedding、retry は別計上する。M の平均履歴は後述する GPT-6 の単一 context を超えるため、全文一括投入を前提にできない。[LongMemEval 本文](https://arxiv.org/html/2410.10813v2)

---

### 3.4 定量効果、negative results、failure modes

#### consolidation の寄与を比較しやすい結果

| 研究 | 対照・結果 | 読み取れること／限界 |
|---|---|---|
| **Auto-Dreamer** | Writer-only→untrained→trained SR：ALFWorld **48.0→59.2→60.2**、ScienceWorld **29.6→31.4→41.1**、WebArena **44.2→46.6→52.3** | untrained 整理でも効果があり、学習寄与と整理寄与を分けられる。具体的 location を抽象化で失う失敗もある |
| **SimpleMem** | full vs w/o consolidation：平均 F1 **43.24 vs38.24**、multi-hop **43.46 vs29.85** | background consolidation の直接 ablation。ただし LoCoMo 上の著者評価 |
| **EverMemOS** | PersonaMem v2：episode＋profile **53.25** vs episode-only **43.93** | profile consolidation の寄与。夜間処理や誤記憶訂正の寄与そのものではない |
| **LightMem** | 同一設定で offline update 前後：GPT-4o-mini **67.78→65.39%**、Qwen3-30B **70.20→65.14%** | **整理後に悪化する直接例**。方式全体の高速化と精度利得を混同できない |
| **WikiSkill** | proposer が wiki を使う条件 **48.7→63.7%**。実行 agent にも渡すと **60.9%** | 整理した知識を「誰に・いつ渡すか」も独立した要因 |
| **MEMOIR** | evidence recall **58.82→59.08%**、active store **1.42%減** | 品質保持を重視するが、圧縮効果は小さい。100 merge 無損失でも損失率の片側95%上限は2.95% |
| **SCM** | full22/22、NREMなし20/22、REMなし22/22 | NREM の小規模寄与。**REM dreaming の recall 利得は示せていない** |

出典：[Auto-Dreamer Table 2](https://arxiv.org/html/2605.20616v1#S5.SS4)、[SimpleMem Table 4](https://arxiv.org/html/2601.02553v1)、[EverMemOS](https://arxiv.org/html/2601.02163v1)、[LightMem Table 1](https://arxiv.org/html/2510.18866v1)、[WikiSkill](https://arxiv.org/html/2608.27454v1)、[MEMOIR](https://arxiv.org/html/2611.07631v1)、[SCM Table 4](https://arxiv.org/html/2604.20943v1)

Letta の sleep-time compute も対象課題と指標を限定して読む必要がある。SWE case study は33例で、主指標はコードの正しさではなく**変更ファイル集合の F1**。高い計算予算では test-time-only が上回る条件もある。[Letta 論文](https://arxiv.org/html/2504.13171v1)

#### 既知の失敗

**1. 継続書換えによる記憶崩壊**

*Useful Memories Become Faulty When Continuously Updated by LLMs* は、正しい solution を与えても、GPT-5.4 が**元々解けていた ARC 問題の54%で失敗**する条件を報告する。過一般化、異なる task family の誤統合、条件消失、失敗コードからの架空 strategy 抽出が原因として挙がる。raw episodic memory を残す条件や consolidation 禁止条件が強い対照になる。[2026-08-29版](https://arxiv.org/html/2605.12978v2)

公開実装は MIT だが、公開結果データは CC BY 4.0 と分離されている。[コード LICENSE](https://raw.githubusercontent.com/DylanZSZ/Memory-Collapse-Eval/main/LICENSE)、[データ LICENSE](https://raw.githubusercontent.com/DylanZSZ/Memory-Collapse-Eval/main/LICENSE-DATA)

**2. 日時・適用条件の選択的消失**

*The Sleeping Agent* は gist 圧縮で日時が落ちることを調べ、prompt の1文修正で temporal preservation **3.05→62.39%**、temporal judge accuracy **+0.314** を報告する。単なる長さ制約より「何を絶対に落とさないか」が重要である。ただし同研究の LoCoMo データは今回の採用条件外。[2026-08-12論文](https://arxiv.org/html/2608.11775v1)

**3. memory poisoning は保存・検索・実行を分ける**

*Hidden in Memory* は poisoned memory の保存率を最大 GPT-5.5 **99.8%**、Kimi-K2.6 **95%** と報告する。一方、attacker-intended action の **60–89%** は**検索成功を条件とした値**である。これらを無条件の end-to-end attack success rate として引用してはいけない。[2026-05-18版](https://arxiv.org/abs/2605.15338)

**4. 要約による memory laundering**

*State Contamination* は、毒性のある入力が要約されると detector 上は安全に見えても、後続挙動へ影響が残ることを示す。完成した summary の検査だけでなく、**圧縮前の入力段階と書込み後の行動段階**を評価する必要がある。[2026-05-16論文](https://arxiv.org/abs/2605.16746)

**5. agent 自身が生成した記憶も安全とは限らない**

*Self-Propagating Misalignment…* は、prompt で misalignment を模擬した20 scenarios で、自己生成した目標が後続 session へ伝播することを調べた。外部攻撃者がいない自然発生率の測定ではないが、自己生成 memory を無条件に信頼できない根拠になる。memory tool を除いても file system が代替経路になった。[2026-10-02論文](https://arxiv.org/abs/2610.04083)

---

### 3.5 Azure Microsoft Foundry とローカルモデル

#### Azure の掲載・仕様を確認できたモデル

| モデル | Azure version | context window | 最大出力 | 役割案〔推論〕 |
|---|---|---:|---:|---|
| `gpt-6-luna` | 2026-09-22 | 1,050,000 | 128,000 | analyzer、抽出・通常整理 |
| `gpt-6-sol` | 2026-09-22 | 1,050,000 | 128,000 | main、難しい organizer |
| `gpt-6-astra` | 2026-09-03 | 1,050,000 | 128,000 | judge、難しい検証 |
| `gpt-6.1-sol` | 2026-09-29 | 1,050,000 | 128,000 | main の比較候補 |
| `gpt-6.1-terra` | 2026-10-26 | 1,050,000 | 128,000 | 視覚入力を含む比較候補 |

公式表には入力上限922,000も記載される。入力・出力・reasoning は context 予算を共有するため、個別上限を独立して使い切れるという意味ではない。[Microsoft Learn、2026-10-29更新](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure?pivots=azure-direct-others)

公開 availability 表では、上記の Global Standard は Japan East 欄に掲載されている。ただし **Japan East resource＝日本国内での推論処理ではない**。Global deployment の処理地域、subscription quota、申請要否、capacity は別条件である。[地域表](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability)、[quota docs](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/quotas-limits)

#### Azure 公式公表価格

以下は **2026-09-22発表時の Global Standard 参考単価**。単位は USD／100万 tokens。**11月10日の適用価格を確定したものではない。**

| モデル | short：入力／cache読込／cache書込／出力 | long：入力／cache読込／cache書込／出力 |
|---|---|---|
| Luna | 0.10／0.01／0.125／0.50 | 0.20／0.02／0.25／0.75 |
| Sol | 2.00／0.20／2.50／10.00 | 4.00／0.40／5.00／15.00 |
| Astra | 10.00／1.00／12.50／50.00 | 20.00／2.00／25.00／75.00 |

出典：[Microsoft Azure 公式発表、2026-09-22](https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/?trk=article-ssr-frontend-pulse_little-text-block)

現行料金ページは今回取得が timeout、または数値欄が `$-` だった。short／long の Azure 固有 threshold、6.1 系、embedding、非 OpenAI 各モデルの現行単価は未確認。**OpenAI direct 価格による代用はしていない。**[Azure OpenAI 料金](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)

#### 非 OpenAI・検索系の候補

| 候補 | 一次資料で確認できたこと | 用途案〔推論〕 |
|---|---|---|
| Claude Sonnet 5 | context 1M、最大出力128K。Azure hosting と Anthropic hosting を区別して掲載 | main／異系列 judge |
| Claude Haiku 4.5 | context200K、最大出力64K | analyzer 比較 |
| DeepSeek-V3.2 | Azure 販売 catalog、tool calling／JSON | 異系列比較。日本語品質は別評価 |
| Mistral-Large-3 | catalog 掲載、日本語、tool calling、Preview | 探索比較 |
| text-embedding-3-small／large | 最大入力8,192、1,536／3,072次元 | embedding baseline |
| Cohere-rerank-v4.0-fast／pro | 日本語対応 reranker として掲載 | cloud reranker 比較 |

出典：[Claude in Foundry](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models)、[Azure 販売 catalog](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure?pivots=azure-direct-others)

これらは API サービスの候補であり、MIT／Apache／BSD の OSS として挙げているものではない。subscription 種類・hosting・商用条件を個別に扱う。

#### M4 16GB のローカル候補

model card／配布状態は2026-11-10確認。公開最大 context は、その長さを16GBで常用できるという意味ではない。

| モデル | 仕様・ライセンス | 想定役割 |
|---|---|---|
| [Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B) | 1.7B、32,768 context、Apache-2.0。LICENSE 本文確認 | analyzer 初期候補、小規模 QLoRA |
| [Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B) | 4B、native32,768、Apache-2.0。LICENSE 本文確認 | organizer／品質比較 |
| [Phi-4-mini-instruct](https://huggingface.co/microsoft/Phi-4-mini-instruct) | 3.8B、128K、MIT。LICENSE 本文確認 | 生成モデルの異系列比較 |
| [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | 0.6B、32K、32–1,024次元。公式 card は Apache-2.0 | local embedding |
| [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) | 0.6B、32K。公式 card は Apache-2.0 | top-k 候補の reranking |

embedding／reranker は公式 card の宣言まで確認したが、独立 LICENSE ファイルの取得は成功しなかった。厳密に LICENSE 本文の保存を要件にする場合は確認を残す。

Ollama 配布サイズは Qwen3-1.7B Q4_K_M **1.4GB**、4B Q4_K_M **2.5GB**、embedding0.6B Q8_0 **639MB**。これは**ファイルサイズであり RAM 使用上限ではない**。[1.7B](https://ollama.com/library/qwen3:1.7b)、[4B](https://ollama.com/library/qwen3:4b)、[embedding](https://ollama.com/library/qwen3-embedding:0.6b)

**推論：** 生成は concurrency 1、4K–8K context から測定し、embedding・reranker・生成を順番に実行する構成が妥当。小型モデルは抽出 schema 適合率だけでなく、誤記憶訂正と出典保持を確認してから organizer に使う。

---

### 3.6 Python uv／macOS の実装部品

以下は2026-11-10時点で公式 repo と LICENSE を確認した候補。**トップレベルのライセンス確認であり、全 transitive dependencies の監査完了ではない。**

| 部品 | ライセンス | 用途・適合性 | 注意 |
|---|---|---|---|
| Markdown／JSONL の自前 store | 新規外部 repo 不要 | immutable transcripts、derived notes、wiki、revision manifest | ファイル形式だけでは検索・訂正は実現しない |
| [LanceDB Embedded](https://github.com/lancedb/lancedb) | [Apache-2.0](https://raw.githubusercontent.com/lancedb/lancedb/main/LICENSE) | process 内で動く local retrieval。Python 対応 | Cloud／Enterprise と OSS を分ける |
| [sqlite-vec](https://github.com/asg017/sqlite-vec) | MIT OR Apache-2.0 | 小さい vector store。macOS 対応 | pre-v1。SQLite 本体は public domain で、指定3種とは別扱い |
| [Chroma](https://github.com/chroma-core/chroma) | [Apache-2.0](https://raw.githubusercontent.com/chroma-core/chroma/main/LICENSE) | local vector store の代替 | baseline 間で DB を変えると比較要因が増える |
| [BM25S](https://github.com/xhluca/bm25s) | [MIT](https://raw.githubusercontent.com/xhluca/bm25s/main/LICENSE) | lexical retrieval、mmap | 日本語 tokenization を固定する必要 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | [MIT](https://raw.githubusercontent.com/langchain-ai/langgraph/main/LICENSE) | 明示的な state／workflow、checkpoint | agent loop と memory policy を分離 |
| [PydanticAI](https://github.com/pydantic/pydantic-ai) | [MIT](https://raw.githubusercontent.com/pydantic/pydantic-ai/main/LICENSE) | typed output／tool loop | schema validation と factual validation は別 |
| [Sentence Transformers](https://github.com/huggingface/sentence-transformers) | [Apache-2.0](https://raw.githubusercontent.com/UKPLab/sentence-transformers/master/LICENSE) | embedding／reranking runtime | 使用モデルの weight license は別 |
| [MLX-LM](https://github.com/ml-explore/mlx-lm) | [MIT](https://raw.githubusercontent.com/ml-explore/mlx-lm/main/LICENSE) | Apple silicon inference／QLoRA | M4 の所要 RAM・速度は実測が必要 |
| [Ollama](https://github.com/ollama/ollama)／[llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | local API／GGUF inference | runtime の MIT は model weights の許諾ではない |

SQLite は公式に public domain とされるため、「MIT／Apache／BSD のみ」を文字通り適用するなら別途扱いを決める必要がある。無断で whitelist に含めない。[SQLite 公式説明](https://www.sqlite.org/copyright.html)

**推論：最小構成**は、file store＋BM25S＋LanceDB＋薄い tool loop で足りる。既存 agent memory framework を複数同時導入する必要はない。

macOS の実行境界にも注意が必要である。Docker 公式資料は、Metal の GPU passthrough が container にはなく、vllm-metal は host 上で動くと説明する。したがって「dev container 内の実験制御」と「native macOS の推論／学習 service」を分ける構成が現実的である。これは今回実行したものではなく、実装段階の環境上の検討事項である。[Docker 公式、2026-02-26](https://www.docker.com/blog/docker-model-runner-vllm-metal-macos/?trk=article-ssr-frontend-pulse_little-text-block)

---

## 4. 検証設計への示唆

以下は文献調査からの**提案**であり、実証済みの結論ではない。

### 4.1 比較群

| 比較群 | 役割 |
|---|---|
| No-memory | 下限参照。主対照にはしない |
| Raw transcript hybrid RAG | 整理せず原文を検索する強い対照 |
| Rolling summary＋同じ検索 | 一般要約による圧縮との比較 |
| Online notes only | offline consolidation の増分を測る |
| Notes＋offline merge／organize | 重複整理・構造化の寄与 |
| Notes＋source verification＋organize | 誤記憶訂正の追加寄与 |
| 既存 OSS 1方式 | SimpleMem または LangMem 等との外部比較 |

主比較には、**同じ予算の Rolling summary と source-verified consolidation**が適する。wiki 化と事実訂正を同時に変えると理由が分からないため、整理と再検証を分ける。

公平性には二つの評価を併用する。

1. **固定 bank 評価**：同じ notes と transcripts を各方式へ渡し、整理後に同じ reader／query を評価する。
2. **逐次 stream 評価**：過去だけを使って記憶を更新し、長期的な利得・退行・drift 回復を測る。

offline 処理の呼出回数・token cap・cadence を固定し、通常要約側にも比較可能な maintenance 予算を与える。回答予算だけを揃えて公平としない。

### 4.2 推奨 benchmark の組合せ

- **主評価：LongMemEval-S。** 最初の少数問は動作・費用確認用とし、主張には500問または検出力に基づく規模を使う。
- **更新競合：MemoryAgentBench FactConsolidation SH／MH。** 配布 revision と元データ条件を固定する。
- **agent 経験への transfer：LongMemEval-V2 Small。** factual QA だけでは見えない workflow／gotchas を補う。
- **制御した独自 stream。** 架空情報で日時、訂正、撤回、依存関係、誤メモを機械的に生成し、gold event ledger を持つ。STALE／MEME の評価軸を参考にするが、条件外データをコピーしない。
- **CL-Bench。** DB データの利用条件を確認できた場合のみ追加する。

LongMemEval の `has_answer`、`answer_session_ids`、benchmark の gold／grader は memory writer から隔離する。dreaming は評価 question を見る前に行い、将来 session や未来の正解を与えない。

### 4.3 指標

| 軸 | 推奨指標 |
|---|---|
| 最終性能 | accuracy、task success、abstention、transfer gain |
| 整理の修復・退行 | **誤→正率、正→誤率**、net gain |
| 意味保存 | fact／日時／条件／否定／出典の保持率、unsupported claim rate |
| drift | stale 採用率、更新反映遅延、更新後の回復 session 数 |
| 検索と reader の分離 | evidence recall、正しい evidence を与えた場合の reader error |
| 圧縮 | active tokens、archive tokens、index size、重複率 |
| 安全性 | poison 保存率→検索率→行動成功率、secret-canary 保存率 |
| 費用 | ingest／consolidation／query／judge 別 tokens、ドル、wall-clock、p95 latency |
| parametric 実験 | acquisition、旧知識 retention、非対象能力、更新後 rollback |

特に **active store が小さくなっただけで total storage が増える**場合を見落とさない。原文 archive を残す構成では active compression と全保存量を別報告する。

統計は同一問題の paired comparison を基本とし、同じ人物・履歴・task stream に属する query は cluster 単位で扱う。複数 seed／順序による変動と、問題数による変動を分ける。主仮説は一つ、他は探索的評価とする。

### 4.4 必要な failure-mode tests

| 条件 | 確認したい失敗 |
|---|---|
| 正しい原文＋誤った note | 原文へ戻って訂正できるか |
| 古い事実→明示的訂正 | 最新値と過去時点の値を区別できるか |
| 暗黙の環境変更 | 単なる timestamp 比較なしで再検証できるか |
| 例外・適用条件付き規則 | 要約で条件を落とさないか |
| 低頻度だが重要な事実 | importance／頻度ベース pruning の取りこぼし |
| location／数値／日時 | 抽象化しすぎによる詳細消失 |
| 誤った assistant 自己申告 | 「完了した」という発言を実行済み事実に昇格しないか |
| transcript 内の命令文 | memory policy や tool 権限を変更しないか |
| poison 後に多数の無害 session | 遅延した cross-session 伝播 |
| 同一記憶への反復整理 | 内容が変わらない条件での semantic drift |
| 依存先の撤回 | 派生した wiki／skill まで失効するか |
| organizer の中断・再実行 | 半端な index、重複、revision 不整合 |

ここで transcripts は「何が発言・観測されたか」の証拠であり、内容すべてが世界の真実ではない。user assertion、assistant hypothesis、tool observation、検証済み結果を区別して保存する必要がある。

### 4.5 M4 16GB＋Azure の実行可能範囲

| 項目 | 判断 |
|---|---|
| file memory、BM25、local vector DB、評価 harness | **適合性が高い** |
| Azure analyzer／organizer／main | **適合性が高い**。quota・適用価格は要確認 |
| local 0.6B embedding／reranker | **有力**。逐次実行で計測 |
| local 1.7B／4B analyzer | **探索可能**。訂正品質の事前評価が必要 |
| 1–4B QLoRA consolidation | **小規模な方式検証として可能性あり**。未実測 |
| SLEEP の architecture＋GKD＋dreaming RL 全体 | **再現可能とは現時点で言えない** |
| 14B Auto-Dreamer consolidator の GRPO 学習 | **M4 16GB の主実験には不適** |
| LongMemEval-V2 全最大履歴、全 agent environment suite | 初手には重い。subset／tier を明示する |

Azure の役割割当は、Luna＝analyzer、Sol＝main／organizer、Astra または異系列モデル＝一部 judge が初期候補。ただし主比較中はモデルを固定し、モデル選定と memory policy の比較を混ぜない。

費用は次の段階別に集計する。

`総費用＝ingest＋online write＋offline maintenance＋retrieval/embedding＋reader＋judge＋retry`

cache input／write の課金区分を保ち、offline 費用を将来何 query で償却できるかも報告する。

---

## 5. 不確実性・未確認事項

1. **本報告は再現実験ではない。** M4 の RAM、速度、API 課金、Azure deployment 成功を測定していない。社内2レポートも brief 記載のみを参照し、原レポートを再監査していない。

2. **Azure の現行適用価格が未確定。** 掲載した GPT-6 価格は9月22日の公式公表値。11月10日の価格、short／long threshold、6.1 系や非 OpenAI の単価は未確認。subscription の quota・権限・capacity も未確認。

3. **公開論文と公開実装を区別した。** Auto-Dreamer、WikiSkill、SCoL、Cortex、SLEEP 等は、今回公式コードと LICENSE を確認できなかった。存在しないと断定したのではなく、再利用候補へ確定していない。

4. **benchmark の版差がある。** MemoryAgentBench の論文版130 sequences と現行配布146 rows、LoCoMo の論文全体50会話と公開10会話を区別する。実験では data revision・subset・件数を記録する必要がある。

5. **CL-Bench のデータ条件と Anthropic 実行条件が未確定。** DB 配布物の LICENSE、および Anthropic が使った schedule／seed／replicate は特定できなかった。

6. **未公開・条件付き公開 benchmark が多い。** MemoryDojo は公開予定、ConsolidationBench は controlled release。論文を読めることを public benchmark の実行可能性と同一視しない。

7. **ライセンス確認には範囲差がある。** 本報告の OSS は主に root LICENSE、データは公式 card／専用 LICENSE を確認した。依存一式、vendored code、元データ、量子化配布物までの完全監査ではない。ライセンス不明は「利用禁止確定」とは異なるが、今回は採用保留とした。

8. **一部資料には内部不整合がある。** SCM の noise 除去数・割合には記述差があるため、 headline の90.9%を採用根拠にしていない。*Memory Contagion* も現行 abstract と HTML の authority-bias 記述に差があり、主要結論には使わなかった。[SCM](https://arxiv.org/html/2604.20943v1)、[Memory Contagion](https://arxiv.org/abs/2606.23195)

9. **既存結果の外挿には限界がある。** LoCoMo 上の改善は日本語業務 memory、最新 API、小型 local model、実際の環境 drift での改善を保証しない。低文脈圧・短い horizon では、強い RAG／通常要約に勝てない可能性を対立仮説として残す。

---

## 6. 参考文献

日付は論文初出または公式発表日。日付を特定していない repo／docs／model card は、**2026-11-10閲覧**の snapshot である。

### 6.1 外部メモリ・consolidation

- **SimpleMem: Efficient Lifelong Memory for LLM Agents**、2026-01-05  
  [論文 — https://arxiv.org/html/2601.02553v1](https://arxiv.org/html/2601.02553v1)  
  [コード — https://github.com/aiming-lab/SimpleMem](https://github.com/aiming-lab/SimpleMem)
- **EverMemOS: A Self-Organizing Memory Operating System for Structured Long-Horizon Reasoning**、2026-01-05  
  [論文 — https://arxiv.org/html/2601.02163v1](https://arxiv.org/html/2601.02163v1)  
  [コード — https://github.com/EverMind-AI/EverMemOS](https://github.com/EverMind-AI/EverMemOS)
- **Auto-Dreamer: Learning Offline Memory Consolidation for Language Agents**、2026-05-20  
  [論文 — https://arxiv.org/html/2605.20616v1](https://arxiv.org/html/2605.20616v1)
- **WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution**、2026-08-27  
  [論文 — https://arxiv.org/html/2608.27454v1](https://arxiv.org/html/2608.27454v1)
- **MEMOIR: A Training-Free Memory Lifecycle for Long-Horizon LLM Agents**、2026-11-05  
  [論文 — https://arxiv.org/html/2611.07631v1](https://arxiv.org/html/2611.07631v1)  
  [コード — https://github.com/AnandMayank/MEMOIR](https://github.com/AnandMayank/MEMOIR)
- **Learning by Doing: Autonomously Compiling and Curating Agent Skills from Verified Traces**、2026-11-01  
  [論文 — https://arxiv.org/html/2611.01386v1](https://arxiv.org/html/2611.01386v1)
- **Sleep-time Compute: Beyond Inference Scaling at Test-time**、2025-04-17  
  [論文 — https://arxiv.org/html/2504.13171v1](https://arxiv.org/html/2504.13171v1)  
  [研究コード — https://github.com/letta-ai/sleep-time-compute](https://github.com/letta-ai/sleep-time-compute)  
  [Letta — https://github.com/letta-ai/letta](https://github.com/letta-ai/letta)
- **A-MEM: Agentic Memory for LLM Agents**、2025-02-17  
  [論文 — https://arxiv.org/html/2502.12110v1](https://arxiv.org/html/2502.12110v1)  
  [実装 — https://github.com/agiresearch/A-mem](https://github.com/agiresearch/A-mem)
- **Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory**、2025-04-28  
  [論文 — https://arxiv.org/html/2504.19413v1](https://arxiv.org/html/2504.19413v1)  
  [コード — https://github.com/mem0ai/mem0](https://github.com/mem0ai/mem0)
- **Dream: Background memory consolidation for AI agents**、2026-08-04、09-28更新  
  [公式記事 — https://mem0.ai/blog/dream-background-memory-consolidation-for-ai-agents](https://mem0.ai/blog/dream-background-memory-consolidation-for-ai-agents)
- **Zep: A Temporal Knowledge Graph Architecture for Agent Memory**、2025-01-20  
  [論文 — https://arxiv.org/html/2501.13956v1](https://arxiv.org/html/2501.13956v1)  
  [Graphiti — https://github.com/getzep/graphiti](https://github.com/getzep/graphiti)
- **MemOS: A Memory OS for AI System**、2025-07-04、v4 12-03  
  [論文 — https://arxiv.org/html/2507.03724v4](https://arxiv.org/html/2507.03724v4)  
  [コード — https://github.com/MemTensor/MemOS](https://github.com/MemTensor/MemOS)
- **Memory OS of AI Agent**、2025-05-30  
  [論文 — https://arxiv.org/html/2506.06326v1](https://arxiv.org/html/2506.06326v1)  
  [コード — https://github.com/BAI-LAB/MemoryOS](https://github.com/BAI-LAB/MemoryOS)
- **LangMem SDK for agent long-term memory**、2025-02-18  
  [公式発表 — https://www.langchain.com/blog/langmem-sdk-launch](https://www.langchain.com/blog/langmem-sdk-launch)  
  [docs — https://langchain-ai.github.io/langmem/](https://langchain-ai.github.io/langmem/)  
  [コード — https://github.com/langchain-ai/langmem](https://github.com/langchain-ai/langmem)
- **Cognee**  
  [現行 improve docs — https://docs.cognee.ai/core-concepts/main-operations/improve](https://docs.cognee.ai/core-concepts/main-operations/improve)  
  [コード — https://github.com/topoteretes/cognee](https://github.com/topoteretes/cognee)
- **LightMem: Lightweight and Efficient Memory-Augmented Generation**、2025-10-21  
  [論文 — https://arxiv.org/html/2510.18866v1](https://arxiv.org/html/2510.18866v1)  
  [コード — https://github.com/zjunlp/LightMem](https://github.com/zjunlp/LightMem)
- **Memento: Fine-tuning LLM Agents without Fine-tuning LLMs**、2025-08-22  
  [論文 — https://arxiv.org/abs/2508.16153](https://arxiv.org/abs/2508.16153)  
  [コード — https://github.com/Memento-Teams/Memento](https://github.com/Memento-Teams/Memento)
- **Memento-Skills: Let Agents Design Agents**、2026-03-19  
  [論文 — https://arxiv.org/abs/2603.18743](https://arxiv.org/abs/2603.18743)  
  [コード — https://github.com/Memento-Teams/Memento-Skills](https://github.com/Memento-Teams/Memento-Skills)
- **Reflexion: Language Agents with Verbal Reinforcement Learning**、2023-03-20  
  [論文 — https://arxiv.org/abs/2303.11366](https://arxiv.org/abs/2303.11366)  
  [コード — https://github.com/noahshinn/reflexion](https://github.com/noahshinn/reflexion)
- **ExpeL: LLM Agents Are Experiential Learners**、2023-08-20  
  [論文 — https://arxiv.org/html/2308.10144v3](https://arxiv.org/html/2308.10144v3)  
  [コード — https://github.com/LeapLabTHU/ExpeL](https://github.com/LeapLabTHU/ExpeL)
- **Agent Workflow Memory**、2024-09-11  
  [論文 — https://arxiv.org/abs/2409.07429](https://arxiv.org/abs/2409.07429)  
  [コード — https://github.com/zorazrw/agent-workflow-memory](https://github.com/zorazrw/agent-workflow-memory)

### 6.2 Parametric・sleep architecture

- **Language Models Need Sleep: Learning to Self-Modify and Consolidate Memories**、2026-06-02、v2 07-10  
  [論文 — https://arxiv.org/html/2606.03979v2](https://arxiv.org/html/2606.03979v2)
- **Self-Consolidating Language Models: Continual Knowledge Incorporation from Context**、2026-05-08  
  [論文 — https://arxiv.org/html/2605.07076v2](https://arxiv.org/html/2605.07076v2)
- **Do Language Models Need Sleep? Offline Recurrence for Improved Online Inference**、2026-05-25  
  [論文 — https://arxiv.org/abs/2605.26099](https://arxiv.org/abs/2605.26099)
- **CoDe-LoRA: Mitigating the Orthogonality Dilemma in Continual Learning of LLMs via Knowledge Consolidation and Decoupling**、2026-10-06  
  [論文 — https://arxiv.org/abs/2610.08312](https://arxiv.org/abs/2610.08312)  
  [コード — https://github.com/Estrellajer/CoDe-LoRA](https://github.com/Estrellajer/CoDe-LoRA)
- **Cortex: Consolidating Context into Parametric Memory**、2026-10-14  
  [論文 — https://arxiv.org/abs/2610.20399](https://arxiv.org/abs/2610.20399)
- **Can a Language Model Learn Facts Continually in Its Weights?**、2026-07-13、v3 10-05  
  [論文 — https://arxiv.org/html/2607.11020v3](https://arxiv.org/html/2607.11020v3)
- **Learning to Forget: Sleep-Inspired Memory Consolidation for Resolving Proactive Interference in Large Language Models**、2026-03-15  
  [論文 — https://arxiv.org/abs/2603.14517](https://arxiv.org/abs/2603.14517)
- **SCM: Sleep-Consolidated Memory with Algorithmic Forgetting for Large Language Models**、2026-04-22  
  [論文 — https://arxiv.org/html/2604.20943v1](https://arxiv.org/html/2604.20943v1)
- **sleeping-llm**  
  [実装・著者報告 — https://github.com/vbario/sleeping-llm](https://github.com/vbario/sleeping-llm)

### 6.3 Benchmark・negative results・安全性

- **LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory**、2024-10-14  
  [論文 — https://arxiv.org/abs/2410.10813](https://arxiv.org/abs/2410.10813)  
  [コード — https://github.com/xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval)  
  [データ — https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
- **LongMemEval-V2: Evaluating Long-Term Agent Memory Toward Experienced Colleagues**、2026-05-12  
  [論文 — https://arxiv.org/abs/2605.12493](https://arxiv.org/abs/2605.12493)  
  [コード — https://github.com/xiaowu0162/LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2)  
  [データ — https://huggingface.co/datasets/xiaowu0162/longmemeval-v2](https://huggingface.co/datasets/xiaowu0162/longmemeval-v2)
- **Evaluating Very Long-Term Conversational Memory of LLM Agents**、2024-02-27  
  [論文 — https://arxiv.org/abs/2402.17753](https://arxiv.org/abs/2402.17753)  
  [公式 repo — https://github.com/snap-research/locomo](https://github.com/snap-research/locomo)
- **Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions**、2025-07-07  
  [論文 — https://arxiv.org/abs/2507.05257](https://arxiv.org/abs/2507.05257)  
  [コード — https://github.com/HUST-AI-HYZ/MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench)  
  [データ — https://huggingface.co/datasets/ai-hyz/MemoryAgentBench](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench)
- **MemBench: Towards More Comprehensive Evaluation on the Memory of LLM-based Agents**、2025-06-20  
  [論文 — https://arxiv.org/abs/2506.21605](https://arxiv.org/abs/2506.21605)  
  [コード — https://github.com/import-myself/Membench](https://github.com/import-myself/Membench)
- **Evo-Memory: Benchmarking LLM Agent Test-time Learning with Self-Evolving Memory**、2025-11-25  
  [論文 — https://arxiv.org/html/2511.20857v1](https://arxiv.org/html/2511.20857v1)
- **Continual Learning Bench: Evaluating Frontier AI Systems in Real-World Stateful Environments**、2026-06-04  
  [論文 — https://arxiv.org/abs/2606.05661](https://arxiv.org/abs/2606.05661)  
  [コード — https://github.com/pgasawa/continual-learning-bench](https://github.com/pgasawa/continual-learning-bench)  
  [DB データ — https://huggingface.co/datasets/continual-learning-bench/database-exploration](https://huggingface.co/datasets/continual-learning-bench/database-exploration)
- **STALE: Can LLM Agents Know When Their Memories Are No Longer Valid?**、2026-05-07  
  [論文 — https://arxiv.org/abs/2605.06527](https://arxiv.org/abs/2605.06527)  
  [コード — https://github.com/icedreamc/STALE](https://github.com/icedreamc/STALE)
- **MEME: Multi-entity & Evolving Memory Evaluation**、2026-05-12  
  [論文 — https://arxiv.org/abs/2605.12477](https://arxiv.org/abs/2605.12477)  
  [コード — https://github.com/SeokwonJung-Jay/MEME-public](https://github.com/SeokwonJung-Jay/MEME-public)
- **MemEvoBench: Benchmarking Memory MisEvolution in LLM Agents**、2026-04-17  
  [論文 — https://arxiv.org/abs/2604.15774](https://arxiv.org/abs/2604.15774)  
  [コード — https://github.com/xiewwee11/MemEvoBench](https://github.com/xiewwee11/MemEvoBench)
- **When Memory Becomes Authority**、2026-08-03  
  [論文 — https://arxiv.org/abs/2608.01679](https://arxiv.org/abs/2608.01679)
- **ERRAND: Budgeted Maintenance of Agent Memory**、2026-09-02  
  [論文 — https://arxiv.org/abs/2609.29545](https://arxiv.org/abs/2609.29545)
- **The Epistemics of Agent Memory**、2026-09-26  
  [論文 — https://arxiv.org/html/2609.33013v1](https://arxiv.org/html/2609.33013v1)
- **MemoryDojo: Benchmarking Poisoning Through LLM Agent Memory**、2026-11-06  
  [論文 — https://arxiv.org/html/2611.09721v1](https://arxiv.org/html/2611.09721v1)
- **Useful Memories Become Faulty When Continuously Updated by LLMs**、2026-05-13、v2 08-29  
  [論文 — https://arxiv.org/html/2605.12978v2](https://arxiv.org/html/2605.12978v2)  
  [コード — https://github.com/DylanZSZ/Memory-Collapse-Eval](https://github.com/DylanZSZ/Memory-Collapse-Eval)
- **The Sleeping Agent: What Gist-Based Context Compression Loses and Why**、2026-08-12  
  [論文 — https://arxiv.org/html/2608.11775v1](https://arxiv.org/html/2608.11775v1)
- **Hidden in Memory: Sleeper Memory Poisoning in LLM Agents**、2026-05-14  
  [論文 — https://arxiv.org/abs/2605.15338](https://arxiv.org/abs/2605.15338)
- **State Contamination in Memory-Augmented LLM Agents**、2026-05-16  
  [論文 — https://arxiv.org/abs/2605.16746](https://arxiv.org/abs/2605.16746)
- **Self-Propagating Misalignment in LLM Agents, and Why Auditing or Disabling Memory Is Not Enough**、2026-10-02  
  [論文 — https://arxiv.org/abs/2610.04083](https://arxiv.org/abs/2610.04083)
- **Memory Contagion: Cross-Temporal Propagation of Evaluator Bias via Agent Memory**、2026-06-22  
  [論文 — https://arxiv.org/abs/2606.23195](https://arxiv.org/abs/2606.23195)

### 6.4 Azure・ローカルモデル・実装基盤

- **GPT-6 Astra, Sol, and Luna: For production agents in Microsoft Foundry**、2026-09-22  
  [公式発表 — https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/?trk=article-ssr-frontend-pulse_little-text-block](https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/?trk=article-ssr-frontend-pulse_little-text-block)
- **Foundry Models sold by Azure**  
  [catalog — https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure?pivots=azure-direct-others](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure?pivots=azure-direct-others)
- **Region availability／quotas／Claude models**  
  [地域 — https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability)  
  [quota — https://learn.microsoft.com/en-us/azure/foundry/foundry-models/quotas-limits](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/quotas-limits)  
  [Claude — https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models)
- **Azure pricing**  
  [公式料金 — https://azure.microsoft.com/en-us/pricing/details/azure-openai/](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)
- **Qwen／Microsoft model cards**  
  [Qwen3-1.7B — https://huggingface.co/Qwen/Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B)  
  [Qwen3-4B — https://huggingface.co/Qwen/Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B)  
  [Phi-4-mini — https://huggingface.co/microsoft/Phi-4-mini-instruct](https://huggingface.co/microsoft/Phi-4-mini-instruct)  
  [Embedding — https://huggingface.co/Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)  
  [Reranker — https://huggingface.co/Qwen/Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)
- **実装基盤**  
  [LanceDB — https://github.com/lancedb/lancedb](https://github.com/lancedb/lancedb)  
  [sqlite-vec — https://github.com/asg017/sqlite-vec](https://github.com/asg017/sqlite-vec)  
  [Chroma — https://github.com/chroma-core/chroma](https://github.com/chroma-core/chroma)  
  [BM25S — https://github.com/xhluca/bm25s](https://github.com/xhluca/bm25s)  
  [LangGraph — https://github.com/langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)  
  [PydanticAI — https://github.com/pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai)  
  [Sentence Transformers — https://github.com/huggingface/sentence-transformers](https://github.com/huggingface/sentence-transformers)  
  [MLX-LM — https://github.com/ml-explore/mlx-lm](https://github.com/ml-explore/mlx-lm)  
  [MLX LoRA — https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md)  
  [Ollama — https://github.com/ollama/ollama](https://github.com/ollama/ollama)  
  [llama.cpp — https://github.com/ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)
- **Docker Model Runner Brings vLLM to macOS with Apple Silicon**、2026-02-26  
  [公式記事 — https://www.docker.com/blog/docker-model-runner-vllm-metal-macos/?trk=article-ssr-frontend-pulse_little-text-block](https://www.docker.com/blog/docker-model-runner-vllm-metal-macos/?trk=article-ssr-frontend-pulse_little-text-block)