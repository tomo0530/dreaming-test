# Dreaming（エージェントメモリのオフライン統合）関連研究・実装サーベイ（調査担当: Claude）

- 作成日: 2026-11-10
- 前提資料: `docs/research/00-brief.md`（Anthropic「Components of agent memory」、Managed Agents dreaming cookbook、Google SLEEP、社内検証レポート2件の要約）
- 方針: brief に要約済みの資料の繰り返しは避け、それ以外の一次情報（arXiv、公式ブログ・ドキュメント、GitHub、Hugging Face API、Azure Retail Prices API）を集めた。
- 表記ルール
  - 出典付きの記述は事実として扱う。
  - **【推論】** は筆者（Claude）の解釈や提案。
  - **【未確認】** は一次情報で確かめられなかった点。
  - 多くの 2026 年 arXiv 論文は abstract ページ（一部は HTML 本文）だけを読んだ。本文の細部（表の全数値、付録）は読んでいないものが多い。各項目に「確認範囲」を付けた。
  - ライセンスは GitHub API の `license.spdx_id` と Hugging Face API の `cardData.license` で機械的に確かめた（2026-11-10 時点）。

---

## 1. エグゼクティブサマリー

1. **「専用メモリ／統合は強いベースラインに負けやすい」という報告が 2026 年に相次いだ。** 比較群に「全履歴 ICL」と「逐語ログ検索」を必ず入れる必要がある。
   - CL-Bench（Berkeley/Snorkel、NeurIPS 2026）: naive ICL が Mem0・ACE などの専用メモリを上回った。
   - DreamBench-SWE: 逐語イベントメモリ 82/180 と、型付き＋生ログ参照の maintenance probe 83/180 に差がなかった。
   - Supersede: 自己管理メモリに切り替えると、LongMemEval knowledge-update の正答率が 92%→77% に下がった。
   - ContinualSkillBench: skill の保守をしなくても、明示的な skill library と同等だった。
2. **統合そのものの効果を切り分けた ablation は少ないが、ある。** Auto-Dreamer（2026-05）は writer-only（統合なし）、未学習の統合器、学習済み統合器の3条件を同じ環境で比べた。
   - ALFWorld: 48.0→59.2→60.2%
   - ScienceWorld: 29.6→31.4→41.1%
   - WebArena: 44.2→46.6→52.3%
   - あわせてメモリ量が 5〜9 倍小さくなった。prompt だけの統合器でも効く領域がある。ただし「過圧縮で局所的な具体情報を失う」失敗も報告されている。
3. **CL-Bench はコードが公開されている（Apache-2.0）。** Anthropic が使った database task（`database_exploration`）は手元で回せる。
   - 40 問で、Q20 の後に schema migration（ドリフト）が入る。
   - 1 問あたり最大 15 クエリ。reward は regret ベース。
   - SQLite なので Docker は不要。
   - ただし DB の元データ（Amazon-Reviews-2023）のライセンスは HF カードに書かれていない。【未確認】
   - Tencent の別ベンチ「CL-bench（context learning）」と名前が衝突する点に注意。
4. **staleness・conflict を直接測る 2026 年の評価資産が揃ってきた。**
   - STALE、MemConflict、AgingBench（MIT）、MemoryAgentBench の Conflict Resolution（MIT）、LongMemEval-V2（Apache-2.0、workflow／gotchas／premise awareness）、Supersede（Apache-2.0）。
   - 報告されている失敗の例:
     - 古い値に従う率が 0.92〜1.00。
     - 検証予算が限られると、74.7〜77.3% のエピソードで古い制約のまま判断した。
     - revoke 済みの記憶をデフォルトで遮断する実装は1つもなかった。
5. **統合ステップ自体の品質を測る指標が提案されている。**
   - TrustMem: 遷移ごとに omission / corruption / hallucination を測る。
   - MemSecBench: persistence → execute → selective repair のライフサイクルで測る。
   - Rollback repair: 回復率と再発率を測る。
   - これらを使うと、brief の教訓「中間根拠の正しさを最終正答とは別に評価する」を実装できる。【推論】
6. **自己書き込みメモリの誤り伝播は二峰性を示した**（Self-Cleaning、2026-09）。
   - claude-sonnet-4.5 は 20/20 seeds で誤りに捕捉された。
   - 平均値は実在する run の 8.2% しか代表しなかった。
   - **seed を再標本化の単位にし、平均ではなく分布を報告すべき**という統計上の教訓がある。
7. **Azure Foundry のモデル選択肢（一次情報で確認済み）**
   - GPT-6: gpt-6-astra / sol / luna。いずれも context 1,050,000（input 922K / output 128K）。
   - gpt-6-luna の価格（Global Standard、short context、1M tokens あたり）: input $0.10、cached $0.01、output $0.50。Flex メータは $0.05 / $0.25。
   - Claude も partner model として提供されている。claude-opus-5-5、claude-sonnet-5-5、claude-sonnet-5 は「Hosted on Azure: GA」。
   - **ただし Anthropic の Dreams API は Claude API の research preview のみ**。Foundry 上では cookbook 方式で再実装するしかない。
8. **SLEEP 系の重み更新は、M4 16GB では原論文の規模では再現できない。**【推論】
   - 理由: SLEEP はコード未公開。Auto-Dreamer の学習は 8×H100。
   - 代わりに、MLX（mlx-lm は qwen3_5 / gemma4 に対応、LoRA/QLoRA/DoRA。mlx-lm-lora は GRPO/DPO）で次の縮小プローブなら実行できる。
     - 2〜4B の 4bit モデルに LoRA SFT で「知識シーディング」して忘却を測る。
     - EVAF 型のゲート付き LoRA（GPT-2 / TinyLlama 規模）。
9. **ライセンス上の落とし穴**
   - LoCoMo: CC BY-NC 4.0。HaluMem: CC BY-NC-ND 4.0。BEAM: CC BY-SA 4.0。
   - MemBench と MemoryArena のリポジトリにはライセンスがない。
   - mlx-embeddings は GPL-3.0。OpenViking と basic-memory は AGPL-3.0。
   - Gemma 3 と EmbeddingGemma は Gemma license。一方、Gemma 4 E4B は Apache-2.0。

---

## 2. 関連研究・実装の一覧表

凡例
- 種別: P = paper、R = repo、D = docs/blog、B = benchmark。
- ライセンスは repo やデータのもの（論文 PDF のライセンスではない）。「なし」は GitHub 上に LICENSE がないことを指す。

### 2.1 Consolidation／メモリ方式

| 名称 | 年月 | 種別 | メモリ形式 | consolidation の有無と方式 | 主な結果（一次情報の範囲） | ライセンス | URL |
|---|---|---|---|---|---|---|---|
| Auto-Dreamer | 2026-05 | P | 型付き memory bank（テキスト） | あり。fast な per-session writer と slow なオフライン統合器を分離。統合器は bounded tool call で源 trajectory を確認し、置換集合を書く。GRPO で学習 | ScienceWorld で最良ベースライン比 +7pt。bank は 12 倍小さい。writer-only→未学習統合器→学習済み統合器は ALFWorld 48.0→59.2→60.2、ScienceWorld 29.6→31.4→41.1、WebArena 44.2→46.6→52.3 | コード未公開（論文 CC BY 4.0） | https://arxiv.org/abs/2605.20616 |
| MindMemOS | 2026-08 | P | entity–property–time 構造 | あり（“dreaming”）。冗長レコードの統合と矛盾解消。人の暗黙の訂正フィードバックも使う | LoCoMo 94.03%、PersonaMem 70.63%。dreaming の ablation は abstract になし | コード未確認 | https://arxiv.org/abs/2608.12428 |
| REALM | 2026-09 | P | 異種 cognitive graph | あり。検索フィードバックに基づく reconsolidation | LoCoMo 75.97%（+7.17pt）、LongMemEval 65.11%（+1.31pt）。reconsolidation の ablation で一貫して改善 | コード未確認 | https://arxiv.org/abs/2609.16053 |
| TrustMem | 2026-06 | P | 外部テキストメモリ | 更新の検証。Memory Transition Verifier が coverage / preservation / faithfulness を採点し、選好 RL で学習 | 最強ベースライン比で omission −40.1%、corruption −79.1%、hallucination −50.0%。HaluMem 抽出 +12.14 F1 | コード未確認 | https://arxiv.org/abs/2606.25161 |
| LightMem | 2025-10（ICLR 2026） | P+R | 3段（sensory/STM/LTM） | あり。LTM を sleep-time にオフライン更新し、推論から切り離す | LongMemEval / LoCoMo で QA が最大 +7.7% / +29.3%、tokens 最大 1/38・1/20.9、API calls 最大 1/30・1/55.5 | MIT | https://arxiv.org/abs/2510.18866 / https://github.com/zjunlp/LightMem |
| Sleep-time Compute | 2025-04 | P+R | context の事前推論 | あり。クエリ到着前に推論しておく | 同精度で test-time compute を約 1/5、精度は最大 +13% / +18%、関連クエリへの償却で 1 クエリあたりのコストが 1/2.5。効果はクエリの予測可能性と相関 | MIT | https://arxiv.org/abs/2504.13171 / https://github.com/letta-ai/sleep-time-compute |
| Letta Context Repositories / MemFS | 2026-02 | D+R | git 管理の Markdown ファイル群（`system/` は常駐） | あり。background の “memory reflection” を git worktree で実行して自動 merge。“defragmentation” で 15〜25 ファイルに再編。docs 名は “Memory & dreaming” | 定量評価なし（設計資料） | Apache-2.0（letta） | https://www.letta.com/blog/context-repositories / https://docs.letta.com/letta-code/memory |
| OpenAI ChatGPT “Dreaming” (V3) | 2026-06 | D | 合成メモリ要約 | あり。過去チャットから background で合成し、時間経過で更新（「行く予定」→「行った」） | 公式ページに数値なし。Free 向け提供のため compute を約 1/5 にした | 商用 | https://openai.com/index/chatgpt-memory-dreaming/ |
| Anthropic Dreams API | 2026 | D | Managed Agents memory store | あり。入力 store と 1〜100 sessions から新しい store を出力する（入力は不変）。research preview。Claude API のみ | 所要は数分〜数時間。課金は token 従量 | 商用 | https://platform.claude.com/docs/en/managed-agents/dreams |
| Memento-Skills v0.3.0 Dream Daemon | 2026 | R | skill library＋memory | あり。`daemon/dream/consolidator.py` がセッション間に経験を長期記憶と skill 候補へ統合 | 定量評価は未確認 | Apache-2.0 | https://github.com/Memento-Teams/Memento-Skills |
| Mem0 / Mem0g | 2025-04 | P+R | ベクトル（＋グラフ） | 当初は抽出＋統合（更新・削除あり） | LoCoMo の LLM-judge で OpenAI 比 +26%（相対）。p95 latency −91%。tokens −90% 超 | Apache-2.0 | https://arxiv.org/abs/2504.19413 / https://github.com/mem0ai/mem0 |
| Mem0 新アルゴリズム（v3） | 2026-04 | R | 同上 | 方針転換。**ADD-only（UPDATE/DELETE なし）** にし、時間を考慮した検索で読み出し時に解決する | ベンダー値（managed 版）: LoCoMo 71.4→92.5、LongMemEval 67.8→94.4 | Apache-2.0（評価系 mem0ai/memory-benchmarks も Apache-2.0） | https://github.com/mem0ai/mem0 |
| Zep / Graphiti | 2025-01 | P+R | 時間付き知識グラフ | 増分。時間的に関係を保持 | DMR 94.8 vs 93.4。LongMemEval で最大 +18.5%、latency −90% | Apache-2.0 | https://arxiv.org/abs/2501.13956 / https://github.com/getzep/graphiti |
| A-MEM | 2025-02 | P+R | Zettelkasten 型ノート＋リンク | 書き込み時に既存メモの属性を更新（memory evolution） | 6 モデルで SOTA 比改善（abstract） | MIT | https://arxiv.org/abs/2502.12110 / https://github.com/WujiangXu/A-mem |
| MemOS | 2025-07 | P+R | 多種メモリの OS 化 | 非同期 MemScheduler、自己進化 | 数値は未確認 | Apache-2.0 | https://arxiv.org/abs/2507.03724 / https://github.com/MemTensor/MemOS |
| MemoryOS | 2025-06 | P+R | 階層メモリ | 階層間の更新 | 数値は未確認（EMNLP 2025 Oral） | Apache-2.0 | https://arxiv.org/abs/2506.06326 / https://github.com/BAI-LAB/MemoryOS |
| LangMem | 2025〜 | R | LangGraph store | あり。“Background memory manager” が抽出・統合・更新を行う | なし | MIT | https://github.com/langchain-ai/langmem |
| Cognee | 2025〜 | R | グラフ＋ベクトル | `improve` 操作で enrich・feedback 適用・セッション知識をグラフへ橋渡し | なし | Apache-2.0 | https://github.com/topoteretes/cognee |
| ACE (Agentic Context Engineering) | 2025-10（ICLR 2026） | P | 進化する playbook（context） | generation / reflection / curation による増分更新（context collapse を回避） | agent +10.6%、finance +8.6%。**ただし CL-Bench では ACE·GPT-5.4 の normalized reward が 4.6% と低く、コストは最高の $62.8** | 未確認 | https://arxiv.org/abs/2510.04618 |
| Evo-Memory / ReMem | 2025-11 | P | 経験メモリ | ReMem（action-think-memory refine） | 単純な ExpRAG が強い（Claude 3.7 単ターン平均 0.59。Mem0 0.55、LangMem 0.49）。ReMem の多ターン成功率は 0.24→0.78（Claude 3.7） | コード「公開予定」 | https://arxiv.org/abs/2511.20857 |
| Reflexion / ExpeL / AWM / ReasoningBank | 2023-03 / 2023-08 / 2024-09 / 2025-09 | P+R | 反省文・insight・workflow・reasoning memory | エピソード後の蒸留 | 基礎文献（数値の再確認はしていない） | MIT / Apache-2.0 / Apache-2.0 / 未確認 | https://github.com/noahshinn/reflexion / https://github.com/LeapLabTHU/ExpeL / https://github.com/zorazrw/agent-workflow-memory / https://arxiv.org/abs/2509.25140 |
| SSGM | 2026-03 | P | 枠組み | 統合の前に整合性検証・時間減衰・アクセス制御 | 実験なし（概念と形式分析） | — | https://arxiv.org/abs/2603.11768 |
| Rollback repair | 2026-08 | P+R | memory→action 依存グラフ | 事後修復。依存を追跡し、裏付けのない記憶を無効化し、選択的に再実行 | 回復率 85.3%（最強ベースライン 77.3%）、LLM 呼び出し −41.8%、再発率 26.6%（LLM-judge repair は 12.1%） | 未確認 | https://arxiv.org/abs/2608.10502 / https://github.com/xiaoqi-7/memory-rollback |
| Anthropic knowledge-wiki quickstart | 2026 | R | Markdown wiki | 文書を wiki へ蒸留する notebook | — | MIT | https://github.com/anthropics/claude-quickstarts/tree/main/managed-agents/knowledge-wiki |

### 2.2 パラメトリック統合（SLEEP に近いもの）

| 名称 | 年月 | 種別 | 方式 | 主な結果 | コード／ライセンス | URL |
|---|---|---|---|---|---|---|
| SLEEP（Language Models Need Sleep） | 2026-06（blog 2026-10-27） | P+D | Knowledge Seeding（小モデル→大モデルへの一般化蒸留）＋Dreaming（RL による合成カリキュラム） | AIME24 79.2%、AIME25 69.0%、HMMT25 46.1%、SQuAD 48.9%、BABILong 10M、Manchu/Kalamang でほぼ忘却なし | コード未公開。モデル規模も blog に記載なし | https://arxiv.org/abs/2606.03979 / https://research.google/blog/the-sleep-framework-a-new-way-for-llms-to-retain-knowledge-in-continual-learning/ |
| Do LMs Need Sleep? Offline Recurrence | 2026-05 | P | context を SSM ブロックの fast weights へ N 回の offline 再帰で固定し、KV を破棄 | sleep 長 N を増やすと改善（深い推論ほど） | コード未確認 | https://arxiv.org/abs/2605.26099 |
| EVAF（Memory Depth, Not Memory Access） | 2026-06 | P | surprise/valence でゲートした LoRA 統合 | 短い事実は検索が強い（0.956〜0.973）。目標の持続は EVAF が強い（0.812〜0.904）。書き込みは 200 イベントあたり 2〜3 回。stale の無効化は未解決 | GPT-2 / TinyLlama / Mistral-7B。コード未確認 | https://arxiv.org/abs/2606.26806 |
| Supersede | 2026-06 | P+R | stale より現在値を報酬とする RL 環境（verifiers/prime-rl） | Qwen2.5-3B の GRPO で held-out supersession が 9.0→16.7%（単一 run） | Apache-2.0 | https://arxiv.org/abs/2606.27472 / https://github.com/Vrin-cloud/supersede |
| SEAL | 2025-06 | P | 自己編集データの生成→SFT、RL で編集方針を学習 | 知識取り込みと few-shot | プロジェクトページ | https://arxiv.org/abs/2506.10943 |
| PEAM | 2026-05 | P | カテゴリ別に分離した LoRA へ経験を内在化 | 数値は未確認 | 未確認 | https://arxiv.org/abs/2605.27762 |

### 2.3 ベンチマーク

| 名称 | 年月 | 測るもの | 規模 | 公開・ライセンス | ローカル＋API 適性【推論含む】 | URL |
|---|---|---|---|---|---|---|
| **CL-Bench（Continual Learning Bench）** | 2026-06（NeurIPS 2026） | 逐次経験からの学習（gain = stateful − stateless） | 6 ドメイン。DB task は 40 問（Q20 で migration）。Codebase は 19、Poker は 120 hands など | Apache-2.0。DB は HF からダウンロード。元データ Amazon-Reviews-2023 のライセンスは未記載 | ◎（DB task は SQLite のみ、litellm 経由で Azure 可、Python ≥3.13） | https://github.com/pgasawa/continual-learning-bench / https://arxiv.org/abs/2606.05661 |
| LongMemEval | 2024-10（ICLR 2025） | 長期対話記憶 5 能力（knowledge-update を含む） | 500 問。S は約 115K tokens / 約 40 sessions、M は約 500 sessions | MIT（コード・HF データ） | ◎ | https://github.com/xiaowu0162/LongMemEval |
| **LongMemEval-V2** | 2026-05（ICML 2026） | web-agent trajectory からの経験（static / dynamic state、workflow、gotchas、premise awareness） | 451 問、最大 500 trajectories / 115M tokens、small と medium の tier | Apache-2.0（コード・データ） | ○（small tier。マルチモーダルで大容量、judge の既定は gpt-5.2） | https://github.com/xiaowu0162/LongMemEval-V2 |
| MemoryAgentBench | 2025-07（ICLR 2026） | AR / TTL / LRU / **Conflict Resolution**（FactConsolidation） | HF 146 行（AR 22、TTL 6、LRU 110、CR 8）。1 行に長文＋複数問 | MIT（再構成元データのライセンスは個別に確認が必要） | ○ | https://github.com/HUST-AI-HYZ/MemoryAgentBench |
| LoCoMo | 2024（ACL） | 超長期対話 QA と要約 | 10 会話 | **CC BY-NC 4.0** | △（非商用） | https://github.com/snap-research/locomo |
| BEAM | 2025-10（ICLR 2026） | 128K〜10M tokens の対話記憶 | 100 会話、2,000 問 | **CC BY-SA 4.0** | △（share-alike） | https://arxiv.org/abs/2510.27246 / https://huggingface.co/datasets/Mohammadta/BEAM |
| HaluMem | 2025〜 | メモリ操作レベルの幻覚 | — | **CC BY-NC-ND 4.0** | ×（改変不可・非商用） | https://github.com/MemTensor/HaluMem |
| MemBench | 2025 | エージェント記憶 | — | **LICENSE なし** | × | https://github.com/import-myself/Membench |
| Evo-Memory | 2025-11 | タスクストリームでの test-time learning | 9〜10 データセット（AlfWorld、BabyAI、ScienceWorld、PDDL、AIME など） | コード公開予定 | △ | https://arxiv.org/abs/2511.20857 |
| MemoryArena | 2026-02（ICML 2026） | 相互依存マルチセッション（平均 57 steps） | 4 ドメイン | **LICENSE なし** | △ | https://arxiv.org/abs/2602.16313 / https://github.com/ZexueHe/MemoryArena |
| STALE | 2026-05 | 暗黙の矛盾（state resolution / premise resistance / policy adaptation） | 400 シナリオ / 1,200 問、最大 150K context | データの所在は未確認 | ？ | https://arxiv.org/abs/2605.06527 |
| MemConflict | 2026-05 | dynamic / static / conditional な矛盾、検索と順位付けの白箱評価 | 未記載 | 公式 repo に LICENSE なし | △ | https://arxiv.org/abs/2605.20926 / https://github.com/TaoZhen1110/MemConflict |
| AgingBench | 2026-05（NeurIPS 2026） | compression / interference / revision / maintenance の 4 種の aging | 6 シナリオ、10 メモリ系、6 モデル、400 runs、50〜200 sessions | MIT | ○（中身の確認は必要） | https://arxiv.org/abs/2605.26302 / https://github.com/VITA-Group/AgingBench |
| DreamBench-SWE | 2026-08 | マルチセッション SWE の memory hygiene | 1 条件あたり 180 タスク（v2.1） | Apache-2.0。ただし hidden oracle は非公開 | △ | https://arxiv.org/abs/2608.20664 / https://github.com/iroiro147/dreambench-swe |
| AgentCL | 2026-06 | dependent / conventional / independent ストリーム | — | コード未確認 | ？ | https://arxiv.org/abs/2606.02461 |
| ContinualSkillBench | 2026-08 | in-context skill 進化 | 5 ドメイン×100 タスク | コード未確認 | ？ | https://arxiv.org/abs/2608.03874 |
| MemSecBench | 2026-07 | 汚染の persistence → consequence → repair | 310 ケース、48 文脈、24 構成 | コード未確認 | ？ | https://arxiv.org/abs/2607.27080 |
| Harvey LAB | 2026 | 法務の複数文書タスク（Anthropic の wiki 評価で使用） | 2,010 タスク | MIT | ○（multi-doc の部分集合を自前で定義する必要あり） | https://github.com/harveyai/harvey-labs |
| （名前衝突）CL-bench（Tencent） | 2026-02 | context learning | 500 contexts / 1,899 tasks | — | 本件とは別物 | https://arxiv.org/abs/2602.03587 |

---

## 3. 各トピックの詳細

### 3.1 オフライン／sleep-time／background 統合を持つエージェントメモリ

#### (a) 2026 年の「dreaming」系研究

- **Auto-Dreamer**（arXiv 2605.20616、2026-05-20）
  - 確認範囲: abstract と HTML 本文の前半。
  - 構成: complementary learning systems 理論に基づき、fast な per-session writer と slow なオフライン統合器を分ける。
  - 統合器の動作: typed memory bank の一部を証拠として読み、bounded tool call で元 trajectory を確かめ、元エントリを置き換える compact な集合を書く。
  - 学習: GRPO。報酬はエージェントの end-to-end 性能。
  - 使用モデル: agent は Qwen3.5-9B（WebArena では Gemini-3-flash）、統合器は Qwen3-14B。
  - 統合の効果（Table 2、成功率 % と memory tokens）

    | 環境 | writer-only（統合なし） | 未学習の統合器 | 学習済み統合器 |
    |---|---|---|---|
    | ALFWorld | 48.0（50,735 tokens） | 59.2（6,900） | 60.2（10,954） |
    | ScienceWorld | 29.6（61,679） | 31.4（5,700） | 41.1（6,947） |
    | WebArena | 44.2（6,421） | 46.6（1,067） | 52.3（927） |

  - ベースライン: No memory、Reflexion、ExpeL、AWM、Memp、ReasoningBank、Mem0、LightMem、Mem-α、UMEM の 10 種。
  - 限界（論文の記載）
    - 過圧縮で局所的な具体情報を失う（ALFWorld の一部タスク群で、学習済みが未学習に負けた）。
    - 3 つのテキスト環境でしか評価していない。
    - top-K=3 という検索予算に依存する。
    - seed は 42 の 1 本だけ。CI は task bootstrap のみ。
    - 学習は 8×H100。
  - **【推論】** 未学習（prompt だけ）の統合器でも、ALFWorld で writer-only 比 +11.2pt、メモリは約 1/7 になった。API モデルで行う Dreaming 再実装でも、統合の効果が出る余地はある。ただし ScienceWorld では +1.8pt しかなく、ドメイン依存が大きい。
- **MindMemOS**（2608.12428、2026-08）
  - dreaming は「冗長レコードの統合と矛盾解消」を担う。
  - 検索スニペットでは、「1 回目の LLM 呼び出しで issue（conflict、duplication、complementary fragments、low-value、曖昧な関係）を検出し、2 回目で conservative な変更計画にする」という 2 段構成が読み取れた。
  - **【未確認】** abstract ページではこの細部と dreaming の ablation を確かめられなかった。
  - LoCoMo 94.03%、PersonaMem 70.63%。
- **REALM**（2609.16053、2026-09）
  - 検索結果（retrieval feedback）を契機に再統合する（reconsolidation）。
  - ablation では reconsolidation が一貫して効いた（abstract）。
  - **【推論】** 「夜間の一括処理」だけでなく「使われた記憶を使われた時に直す」という設計軸もある。
- **TrustMem**（2606.25161、2026-06）
  - memory の write / revise / delete が起こす失敗を 3 つに分類した。
    - omission（重要情報の脱落）
    - corruption（既存エントリの破損）
    - hallucination（根拠のない追加）
  - 各遷移を Memory Transition Verifier で採点する。
  - **統合品質の指標として、そのまま使える定義**である。
- **SCM**（2604.20943）
  - NREM / REM / forgetting を持つが、評価は小さな自作テスト（10 ターン会話など）だけで、ablation もない。
  - 根拠としては弱い。

#### (b) 製品・OSS の動向（一次ドキュメント）

- **OpenAI “Dreaming”**（2026-06-04）
  - 過去チャットから background でメモリを合成し、時間経過で書き換える（予定を過去形にする）。
  - 公式ページの記述
    - 「dreaming は単体のメモリシステムとしては十分でなかった」。
    - 評価は「時間に敏感な eval で substantial lift」。
  - 数値は載っていない。二次情報の 82.8% / 41.5% は一次ページで確認できなかった。
- **Anthropic Dreams API**（research preview）
  - 入力: 既存の memory store と 1〜100 sessions。
  - 出力: 新しい memory store（入力は変更しない）。
  - `instructions` で方針を指示できる。ただし行単位の指示は効かない。
  - 所要時間は数分〜数時間。課金は通常の token 単価で、セッション数と長さにほぼ線形。
  - Platform 欄は「Claude API」だけ。ZDR の対象外。
- **Letta**（Context Repositories、2026-02）
  - git 管理の Markdown（MemFS）を使う。`system/` 配下は常駐（注入層）で、それ以外は frontmatter 付きで必要時に読む（progressive disclosure）。
  - background の memory reflection は git worktree 上で実行して自動 merge する。
  - defragmentation で 15〜25 ファイルに再編する。
  - サーバ側の sleep-time 設定（`enable_sleeptime`）と、クライアント側の dream / reflection subagent は別物（docs）。
  - **【推論】** cookbook と同じく「ディスク上のディレクトリ＋git 監査」の設計で、実装の参考になる。
- **Mem0 の方針転換**（2026-04、README）
  - 「Single-pass ADD-only extraction — no UPDATE/DELETE. Memories accumulate; nothing is overwritten.」
  - 時間を考慮した多信号検索（semantic＋BM25＋entity）に切り替え、LoCoMo 92.5、LongMemEval 94.4 を報告。
  - 数値は managed 版のもので、ベンダー報告である。
  - **【推論】** 「書き込み時に書き換えず、読み出し時に解決する」という、Dreaming と対立する設計仮説である。比較群に入れる価値がある。
- **Memento-Skills v0.3.0** は `daemon/dream/`（consolidator.py、loop.py）を持つ。社内レポートが言及した Dream Daemon の実体である。
- **LangMem** は background memory manager、**Cognee** は `improve` 操作を持つ。どちらも「バックグラウンドでの抽出・統合」を既製機能として提供する。

#### (c) 基礎文献（2025 年以前で、比較群の候補）

- Sleep-time Compute（2504.13171）は「事前推論による償却」を定量化した。効果はクエリの予測可能性と相関する。
- LightMem（2510.18866）は sleep-time の LTM 更新で、tokens と API calls を大幅に減らした。
- ACE（2510.04618）は incremental delta 更新で「context collapse」を避ける。
- Evo-Memory（2511.20857）は単純な ExpRAG が多くの複雑なメモリより強いと報告した。
- Reflexion / ExpeL / AWM / ReasoningBank は、いずれもエピソード後の蒸留である。

### 3.2 パラメトリック統合・継続学習と M4 16GB での縮小再現性

- **SLEEP**（2606.03979）
  - Google blog（2026-10-27）は ICL・Cartridges・SFT と比べ、Manchu / Kalamang の多段評価でほぼ忘却がなかったとする。
  - コードへのリンクがなく、base model の規模も書かれていない。
  - **【推論】** 原論文の規模での再現は不可能。
- **Do LMs Need Sleep? Offline Recurrence**（2605.26099）は SSM の fast weights を使うアーキテクチャ改変である。API モデルには適用できない。
- **EVAF**（2606.26806）
  - GPT-2 / TinyLlama 規模で、ゲート付き LoRA 統合を「検索は残したまま working context を外す」loop-drift protocol で評価した。
  - **【推論】** M4 16GB でも再現しやすい規模である。
  - 「浅い事実は検索、目標の持続は parametric」という分業の結果は、Dreaming（外部メモリ）と重み更新の役割分担を議論する材料になる。
- **Supersede**（2606.27472、Apache-2.0）
  - Qwen2.5-3B の GRPO で supersession の正答率が 9.0→16.7% になった（単一 run）。基盤は verifiers / prime-rl（GPU 前提）。
- **MLX 側の事実**
  - mlx-lm（MIT、v0.32.2、2026-10-16）
    - LoRA / DoRA / full fine-tune に対応。量子化モデルを指定すると QLoRA になる。
    - `models/` に `qwen3_5.py`、`gemma4.py`、`gpt_oss.py` がある。
    - メモリ節約策（batch size、grad checkpoint、層数の削減）が LORA.md に書かれている。
  - mlx-lm-lora（Apache-2.0）は SFT / DPO / ORPO / GRPO / GSPO などに対応する。
  - 16GB での実測例は二次情報（DagsHub ブログ: 4bit Qwen3-4B の LoRA 学習で約 5〜6GB）しかない。【未確認】
- **【推論】M4 16GB で実行できる縮小プローブ**
  1. dreaming の出力（検証済みルールや wiki）を Q&A 化し、Qwen3.5-2B/4B（4bit）に LoRA SFT で焼き込む（Knowledge Seeding の縮小版）。
  2. 外部メモリ版と、(a) 正答率、(b) 無関係タスクでの忘却、(c) ドリフト後に更新できるか（stale の上書き）を比べる。
  - GRPO や RL ベースの Dreaming（SLEEP Stage 2）は、16GB では rollout のスループットの面で非現実的とみる。
  - 主仮説には含めず、探索的な付録とするのが妥当。

### 3.3 ベンチマークと評価方法

#### CL-Bench（Continual Learning Bench）

- 何か: Asawa, Zaharia, Gonzalez ら（UC Berkeley / Snorkel AI / UW-Madison）による、逐次経験から学習できるかを測るベンチ。arXiv 2606.05661、NeurIPS 2026。
- 公開状況: リポジトリ https://github.com/pgasawa/continual-learning-bench は Apache-2.0。task 環境、system（ICL、ICL Notepad、Mem0、ACE、claude、codex）、harness、schedule、viewer を含む。
- Anthropic の記事が参照する「CL-Bench 1.0」の告知は 2026-05-04（https://continual-learning-bench.com/news/cl-bench-1-0/）。
- **database_exploration**（README による）
  - 内容: Amazon の 3 商品グループを、意図的に schema を変えて SQLite 化したもの（価格はドルとセント、時刻は epoch ms / s / ISO が混在、カラム名は難読化）。
  - `schema_drift` variant: 40 問。21 問目以降は migration 後の DB（stale columns、部分 migration、rename、soft delete、時刻フィールドの分割）。
  - reward: 正解なら 1 − 探索クエリ数/15、不正解なら 0。score は累積 regret から算出する。
  - 補助指標: Accuracy、Avg Queries、Query Reduction（前半−後半）。
  - **【推論】** 検証にとって重要な性質が揃っている。
    - 「学習可能な潜在構造」がある。
    - 「途中でドリフトする」。
    - 正誤を機械判定できる。
    - ローカルの SQLite で完結する。
- gain の定義: 同じ instance での stateful reward − stateless reward。normalized gain は残りの headroom で割る。
- 論文の主結果
  - ICL（Sonnet 4.6）が normalized reward 22.3% / gain 25.4% で 1 位。
  - Mem0（GPT-5.4）は 15.1% / 20.2%、ACE（GPT-5.4）は 4.6% / 8.6%。
  - 「naive ICL outperforms systems dedicated to memory management」。
- 費用（Table 1、mean rollout cost per run）: Gemini 3 Flash ICL $7.6 〜 ACE $62.8。
  - 【未確認】これが全 6 ドメイン合計の値か、どの単位かは読み切れていない。
- 依存関係: litellm 1.81.6 と openai 2.16.0 を pin。Python ≥3.13。DB task 以外は Docker が要るものがある。
- 注意点
  - Anthropic の数値（mean reward、例: Opus 5.5 0.655→0.838）と、CL-Bench 論文やリーダーボードの数値（normalized reward、avg reward）は指標や版が違う可能性があり、直接は比べられない。【未確認】
  - Anthropic がどの schedule（drift ありの default か）で回したかは記事に書かれていない。【未確認】
  - Tencent Hunyuan の「CL-bench: A Benchmark for Context Learning」（arXiv 2602.03587）は別物である。

#### その他のベンチマーク

- **LongMemEval**（MIT）
  - 500 問。質問タイプに `knowledge-update` と `_abs`（回答不能）がある。
  - S は約 115K tokens（約 40 sessions）、M は約 500 sessions。
  - 公式の採点は gpt-4o judge。
  - Supersede はこの knowledge-update 部分集合を使っている。
- **LongMemEval-V2**（Apache-2.0、2026-05）
  - 5 能力のうち dynamic state tracking、workflow knowledge、environment gotchas、premise awareness は、Dreaming が蒸留する「ルール・手順・落とし穴」を直接問う。
  - 規模が大きく（最大 115M tokens）マルチモーダルなので、small tier に限るのが現実的。【推論】
- **MemoryAgentBench**（MIT、ICLR 2026）: Conflict Resolution（FactConsolidation）が更新や矛盾の処理を測る。
- **STALE / MemConflict / AgingBench**
  - いずれも staleness や矛盾を正面から測る 2026 年のベンチ。
  - STALE は最良モデルでも 55.2%。
  - MemConflict は「最終回答の正しさと、検索・順位付けの質が乖離する」と報告した。
  - AgingBench は「整った応答の裏で、メモリが値を供給しなくなっている」こと、派生状態が更新の蓄積とともに劣化することを報告した。
- **DreamBench-SWE**: 事前登録した v2.1 で、記憶なし 21/180、逐語イベントメモリ 82/180、型付き＋生ログ参照 probe 83/180、hosted Mem0 97/180。原研究の主比較（DF-hybrid 95 vs B5 89）は有意差なし（Holm p=1）。
- **AgentCL**: タスク間の依存が既知のストリーム（dependent）でないとメモリ設計の差が出にくい。conventional / independent なストリームでは、メモリが性能を下げることもある。
- **評価コストの目安**（一次情報で分かる範囲）
  - CL-Bench は 1 run あたり $7.6〜$62.8（frontier モデル、表の単位は上記の注意点を参照）。
  - LongMemEval-S は 1 問あたり約 115K tokens の履歴がある（全文 ICL の場合）。

### 3.4 統合の有無による定量効果と、既知の失敗モード

#### 正の効果（統合あり vs なし）

- Auto-Dreamer の Table 2（前述）: 統合ありで +1.8〜+11.2pt（未学習）、+8.1〜+12.2pt（学習済み）。メモリ量は大幅に減る。
- LightMem: sleep-time 更新で精度を維持・改善しつつ、tokens と API calls を 1〜2 桁減らした。
- REALM: reconsolidation の ablation で改善した。
- Evo-Memory ReMem: 多ターンの成功率が大きく上がった（Claude 3.7: 0.24→0.78）。
- Sleep-time compute: 同精度で test-time compute が約 1/5。

#### 効果なし・負の結果（設計上もっとも重要）

- **CL-Bench**: 専用メモリ（Mem0、ACE）と ICL Notepad が素の ICL に負けた。
- **Supersede**: bounded な自己管理メモリでは 92%→77%（gpt-5.4、McNemar p<0.005）。会話を 24 倍に伸ばすと 68%→28% に下がり、メモリを比例して増やしても戻らなかった（28%→28%、n=25）。
- **DreamBench-SWE**: 逐語ログと「型付き＋生ログ」maintenance がほぼ同等だった。
- **ContinualSkillBench**: skill の保守なしでも、明示的な skill library と同等だった。
- **Vector RAG vs LLM-compiled wiki**（事前登録、2605.18490）
  - wiki は論文横断の統合では大きく上回った。
  - しかし 1 クエリあたり約 21 倍の LLM tokens を使い、損益分岐点はなかった。
  - citation support の優位は、スコアラのバグ修正後に成立しなくなった（v3 訂正）。
  - judge 間の順位一致は ρ=0.04〜0.81 と大きくばらついた。
  - Anthropic の Harvey LAB の結果（wiki 87% vs 検索 73%）に近い傾向で、「統合が要る問題でだけ効く」。

#### 失敗モード（staleness・誤り伝播・汚染）

- **古い記憶を過信する**（Memory Trust Gap、2609.01852）
  - Benefit suite で古い値を答える率は、すべての規模で 0.92〜1.00。
  - 新しい日付を付けた古いメモに、大きいモデルほど騙される。
  - 軽減策も能力に依存する。小さいモデルでは「矛盾を事前に解決しておく」ことだけが効いた。
- **検証予算がある中での stale constraint**（2608.25553）
  - 16 モデルで、supersede の後も 74.7〜77.3% のエピソードが古い制約に沿って判断した。
  - 予算 2 枠のうち 1 枠を critical path に割り当てると +61〜81pt 改善した。
  - target-blind な 1 文のルール（「行動の上限を述べる記憶を優先せよ」）で +89.3pt。
  - 鮮度の手がかり（freshness cue）を加えても効かなかった。
  - データとコードは Zenodo にある。
- **revoke が効かない**（2609.08258）
  - 5 つのメモリ系、9 シナリオ、9 モデルを調べ、デフォルトで revocation を強制する系はなかった。
  - revoke 済みの事実が置換後の事実より上位に来て、unsafe な行動を招いた。
  - 対策として backend の前段に置く guard を提案（コード: https://github.com/VulcanLab/Memory-Rebirth-Attack 、Apache-2.0）。
- **自己書き込みストアでの誤りの捕捉**（Self-Cleaning、2609.25052）
  - 初期誤り率 0.9 から始めると、結果は二峰性になった。
  - pooled 平均は 8.2% の run しか代表しなかった。
  - claude-sonnet-4.5 は 20/20 seeds で捕捉された。
  - consistency gate を入れると全モデルが 0.993 になった。
  - seed が正しい再標本化の単位である（design effect 3.75）。
- **自己訂正と役割ラベル**（2606.05976）
  - 同じ誤った主張でも、自分の `<thought>` から外部の役割（`<memory>` や user）に置き換えると、明示的に訂正する率が 23〜93pt 上がった。
  - **【推論】** analyzer に既存 notes を「自分の考え」ではなく「外部メモリ」として提示し、transcript と突き合わせて検証させると、訂正率が上がる可能性がある。比べる価値のあるプロンプト設計の変数である。
- **事後修復**（Rollback repair、2608.10502）: 依存グラフに基づく選択的な再実行で、回復率 85.3%（LLM-judge repair 系より高い）。ただし再発率は 26.6% とトレードオフがある。
- **メモリ汚染**
  - MemSecBench（2607.27080）: 悪性メモリの持続 84.2%、Write→Execute 全鎖の成功 50.3%、selective repair 56.1%。メモリスタック間で最大 41.3pt の差があった。
  - 体系的研究（2606.04329）: 書き込みと検索が積極的なエージェントほど攻撃されやすく、既存の prompt-injection 防御では十分にカバーできない。
  - eTAMP（2604.02623）: LLM による要約や統合は、保存内容を変形しうると指摘している（検索スニペットのみ確認）。
- **圧縮の累積**
  - Rate–Distortion 論文（2607.08032）: 繰り返し行う compaction は「ほとんど測られていない」と指摘。
  - AgingBench: compression aging と interference aging。
  - SSGM: 反復要約による semantic drift。
  - Auto-Dreamer: 過圧縮。
  - これらは Anthropic が挙げた限界（誤記憶の固定化・伝播、肥大化）と対応する。

### 3.5 モデル選択（Azure Microsoft Foundry と ローカル）

#### Foundry（一次情報: Microsoft Learn「Foundry Models sold by Azure」、updated_at 2026-10-29）

- **GPT-6 系列**

  | model | version | Training data |
  |---|---|---|
  | gpt-6-astra | 2026-09-03 | April 2026 |
  | gpt-6-sol | 2026-09-22 | April 2026 |
  | gpt-6-luna | 2026-09-22 | May 2026 |

  - context はいずれも 1,050,000（Input 922,000 / Output 128,000）、max output は 128,000。
  - gpt-6-luna は Responses / Chat Completions / structured outputs / function calling / reasoning effort に対応し、Decisions API にも対応する。
  - 一部の quota tier では、GPT-6 系の deploy に quota 申請が必要（Tier 5/6 は既定で quota あり）。
  - 価格は short context と long context で分かれる。【未確認】閾値（何 tokens から long か）は pricing ページ側にあり、確認できていない。
- **その他の OpenAI モデル**: gpt-6.1-sol（2026-09-29）、gpt-6.1-terra（2026-10-26）、gpt-5.6-sol / terra / luna、gpt-5.4 / mini / nano、gpt-oss-120b / 20b（Preview）。
- **価格**（Azure blog と Azure Retail Prices API（eastus）で一致を確認。Global Standard、USD / 1M tokens）

  | モデル | 区分 | Input | Cached Input | Output | Flex（Input / Output） |
  |---|---|---|---|---|---|
  | gpt-6-astra | short | 10.00 | 1.00 | 50.00 | 5.00 / 25.00 |
  | gpt-6-astra | long | 20.00 | 2.00 | 75.00 | 10.00 / 37.50 |
  | gpt-6-sol | short | 2.00 | 0.20 | 10.00 | 1.00 / 5.00 |
  | gpt-6-sol | long | 4.00 | 0.40 | 15.00 | 2.00 / 7.50 |
  | gpt-6-luna | short | 0.10 | 0.01 | 0.50 | 0.05 / 0.25 |
  | gpt-6-luna | long | 0.20 | 0.02 | 0.75 | 0.10 / 0.375 |
  | gpt-5.4-mini（参考、Global） | — | 0.75 | 0.075 | 4.50 | — |
  | text-embedding-3-small | — | 0.02 | — | — | — |
  | text-embedding-3-large | — | 0.13 | — | — | — |

  - Data Zone（US）は約 +10%、EU は約 +20%（blog の表）。
  - Flex は Retail API にメータがあるが、Learn ページで Flex processing 対応と明記されているのは gpt-5.6-sol など一部だけである。gpt-6 系で Flex を使えるかは【未確認】。
  - 埋め込みはどちらも max input 8,192 tokens。出力次元は small 1,536、large 3,072。
- **Claude（partner model）**
  - Learn「Claude models in Microsoft Foundry」: claude-opus-5-5、claude-sonnet-5-5、claude-sonnet-5、claude-opus-5、claude-opus-4-8 は「Hosted on Azure: GA / Hosted on Anthropic infrastructure: GA」で、1M / 128K。
  - claude-sonnet-4-6、claude-haiku-5-5 などもリストにある（status は個別に未確認）。
  - 利用条件: 有償の Azure サブスクリプションが必要。CSP や credit のみのサブスクリプションは不可。
  - Opus 5.5 は adaptive thinking が常に有効で、thinking を無効にする要求は 400 で拒否される。
  - Fable 系は top_p ≥ 0.99 が必須で、temperature を指定できない。
  - **【推論】** 比較群間で温度を揃える方針と衝突しうる。役割ごとに「温度を指定できるモデルか」を確認する必要がある。
  - Foundry 上の Claude の価格は Retail Prices API に出てこない（Marketplace 課金とみられる）。【未確認】
  - **Dreams API（Managed Agents）は Foundry では使えない**（docs の Cloud platforms 欄は Claude API のみ）。
- **その他の非 OpenAI モデル**（Azure 直販）
  - DeepSeek-V4-Pro / V4.1-Flash（input 1M）、Kimi-K3.1-Pro（1M）、grok-4.7（500K）、MAI-Thinking-1（Preview、256K）、Mistral-Large-3（Preview）、Llama-3.3-70B。
  - Cohere-rerank-v4.0-pro / fast（GA）と v5（Preview）。embed-v-4-0 と Cohere-embed-v5-Fast。
  - Microsoft-Decision-1: 選択肢ごとに較正済みの確率を返す。
  - **【推論】** Microsoft-Decision-1 は judge の補助（多数決の代わりに確率を得る）に使える可能性がある。

#### ローカル（M4 16GB）候補

ライセンスは HF API で確認した。

| 役割 | モデル | パラメータ | ライセンス | 備考 |
|---|---|---|---|---|
| analyzer / organizer の縮小版 | Qwen3.5-4B | 4.66B | Apache-2.0 | mlx-lm は qwen3_5 に対応 |
| 同上 | Qwen3.5-2B | 2.27B | Apache-2.0 | |
| 同上（4bit で約 5〜6GB 想定【推論】） | Qwen3.5-9B | 9.65B | Apache-2.0 | |
| 同上 | Gemma 4 E4B-it | — | Apache-2.0 | mlx-lm は gemma4 に対応 |
| 同上 | Phi-4-mini-instruct | 3.84B | MIT | |
| 同上 | granite-4.0-micro | 3.4B | Apache-2.0 | |
| 同上（16GB では窮屈【推論】） | gpt-oss-20b | 20.9B | Apache-2.0 | |
| 日本語 | sarashina2.2-3b-instruct | — | MIT | |
| embedding | Qwen3-Embedding-0.6B | 0.6B | Apache-2.0 | |
| embedding | bge-m3 | — | MIT | |
| embedding | multilingual-e5-small | — | MIT | |
| embedding（日本語） | ruri-v3-130m / 310m | — | Apache-2.0 | |
| reranker | bge-reranker-v2-m3 | — | Apache-2.0 | |
| reranker | Qwen3-Reranker-0.6B | 0.6B | Apache-2.0 | |

- **避けるもの**
  - Gemma 3（gemma-3-4b-it）と EmbeddingGemma-300m: Gemma license。
  - mlx-embeddings: GPL-3.0。
- Qwen3.8 の大型版は license が “other” だった（ローカル用途ではそもそも対象外）。

### 3.6 実装のビルディングブロック（MIT / Apache / BSD、Python uv、macOS）

- **ファイルベースのメモリストア**
  - ディレクトリ＋Markdown＋YAML frontmatter＋git。
  - 参考実装:
    - Anthropic knowledge-wiki quickstart（MIT）
    - Letta MemFS（Apache-2.0）
    - EverMemOS（Apache-2.0、“Markdown-native”）
    - Memento-Skills（Apache-2.0）
  - basic-memory と OpenViking は AGPL-3.0 なので使わない。
- **ベクトル DB**
  - LanceDB（Apache-2.0、組み込み型）
  - sqlite-vec（GitHub メタデータ上は Apache-2.0）
  - Chroma（Apache-2.0）
  - DuckDB（MIT）
- **BM25 / 全文検索**: bm25s（MIT）、rank_bm25（Apache-2.0）、tantivy-py（MIT）。
- **埋め込みの実行**: sentence-transformers（Apache-2.0、PyTorch MPS）、fastembed（Apache-2.0、ONNX）、Ollama（MIT）。
- **LLM クライアントと agent loop**
  - openai-python（Apache-2.0、Azure OpenAI に対応）
  - openai-agents-python（MIT）
  - pydantic-ai（MIT）
  - smolagents（Apache-2.0）
  - microsoft/agent-framework（MIT）
  - anthropic-sdk-python（Apache-2.0）
  - claude-agent-sdk-python（MIT）
  - instructor（MIT）
  - litellm（`enterprise/` 以外は MIT。CL-Bench が依存）
- **ローカル学習・推論**: mlx-lm（MIT）、mlx-lm-lora（Apache-2.0）、llama.cpp（MIT）、Ollama（MIT）。
- **評価ハーネス**: CL-Bench（Apache-2.0）、LongMemEval / -V2（MIT / Apache-2.0）、MemoryAgentBench（MIT）、AgingBench（MIT）、letta-evals（Apache-2.0）、mem0ai/memory-benchmarks（Apache-2.0）、Harvey LAB（MIT）。
- **【推論】** cookbook の「5 つの sandboxed file tool＋asyncio.Semaphore(20)＋書き込みロック」を素の openai-python（Responses API）で書けば、依存の追加は最小で済む。

---

## 4. 検証設計への示唆

ここでの提案はすべて **【推論】** である。根拠は上記の一次情報による。

### 4.1 主仮説の候補と比較群

- **主仮説（1 つに絞る）の例**
  - 「同じモデル・同じ token 予算で、notes＋Dreaming（transcript を ground truth にした検証と再編）は、notes のみ（オンライン書き込みだけ）より、ドリフトを含む逐次タスクの gain（stateful − stateless）を有意に大きくする」。
  - CL-Bench の gain 定義をそのまま使える。
- **比較群**（下の 3 つは「藁人形」回避のために必須）
  1. stateless（no-memory）
  2. **全履歴 ICL**（CL-Bench で最強だったもの。予算超過の場合は rolling summary にする）
  3. **逐語 transcript 検索**（BM25＋埋め込みのハイブリッド。DreamBench-SWE の verbatim や Mem0 v3 の ADD-only に相当）
  4. notes のみ（Dreaming なし）
  5. notes＋Dreaming（cookbook 方式の analyzer と organizer）
  6. 探索的な群
     - wiki 化 Dreaming
     - 既存 OSS（Mem0 OSS、Apache-2.0）
     - Dreaming の ablation（transcript 検証なし、つまり「整理のみ」）
- **呼び出し予算の揃え方**: Dreaming 群は「オフラインの tokens」を使う。そこで次の 2 つを両方報告する。
  - 総 token（online＋offline）を揃えた比較
  - online の token だけを揃えた比較
  - 根拠: Sleep-time compute と LightMem は「オフラインへの付け替え」を利点としている。一方 wiki vs RAG では、1 クエリあたりの token が逆に 21 倍になった。

### 4.2 タスク・データ

| 優先 | 候補 | 理由 | 注意 |
|---|---|---|---|
| 主 | **CL-Bench `database_exploration`**（`schema_drift`、40 問） | 潜在構造とドリフト（Q20 で migration）を内蔵し、機械採点でき、ローカルの SQLite で済む。Anthropic の報告とも同じ系統 | 1 系列 40 問なので、統計的な検出力には **seed や問題順の複数化が必須**。元データのライセンスを確認する |
| 主 or 副 | **自作のドリフト逐次タスク**（StateDrift-JA の資産を流用） | 誤記憶の注入・矛盾・回答不能を制御できる。日本語 | 「Dreaming が効くはずの条件」を事前にパイロットで確かめる（社内の教訓） |
| 副 | LongMemEval（MIT）の knowledge-update＋abstention 部分集合 / Supersede 環境 | stale の上書きを外部ベンチで確認できる | 会話記憶なので、エージェントタスクとは性質が違う |
| 副 | MemoryAgentBench（MIT）の Conflict Resolution | 矛盾の処理 | 行数が少ない（CR は 8 行） |
| 探索 | LongMemEval-V2 small（Apache-2.0） | workflow / gotchas / premise awareness | 大容量・マルチモーダルでコストが高い |
| 探索 | Harvey LAB（MIT）の multi-doc 部分集合 | wiki 化の効果（統合が要る問題）を確かめる | 部分集合は自前で定義する必要がある |
| 避ける | LoCoMo、HaluMem、BEAM、MemBench、MemoryArena | NC / ND / SA / LICENSE なし | 使う場合は「社内研究目的」と明記する |

### 4.3 指標

- **主要指標**: CL-Bench の gain と normalized gain、regret（探索クエリ数）、accuracy。drift の前後（Q1–20 と Q21–40）で分けて報告する。
- **staleness**
  - 古い値を答える率（Memory Trust Gap 型）
  - premise resistance（STALE 型）
  - revoke 済み記憶の検索混入率（Revoked 型）
- **統合品質**（最終正答とは別の軸）。dreaming の 1 回ごとに、transcript を ground truth として判定する。
  - omission / corruption / hallucination 率（TrustMem 型）
  - 注入した誤記憶の訂正率
  - 不要な書き換えによる良性記憶の破壊率（Rollback 論文の「benign preserved」）
- **肥大化**: 注入層の tokens、store の総 tokens、ファイル数。Dreaming の回数に対する推移。
- **コスト**: online と offline の tokens、USD（Foundry の単価から算出）、wall time。
- **安全**: transcript 経由で注入した指示が store に残る率と、後のセッションで実行される率（MemSecBench の W–E–F 型）。秘密情報が記録される率（cookbook のハーネス項目）。

### 4.4 「効くはずの条件」を実験内で作る操作

- **ドリフト**: CL-Bench の migration を使う。自作タスクでは「規則変更イベント」を時刻 t で入れる。
- **誤記憶の注入**: notes に誤ったルールを k 件混ぜ、Dreaming 後の訂正率と downstream の回復率を測る（Anthropic の Mt. Moon 事例の制御版）。
- **新しい日付を付けた stale メモ**（recency deception）と、**予算制約下での検証**（検証できるレコード数を制限する）。これらは 2026 年の論文で大きな失敗率が報告された条件である。
- **セッション数の掃引**（例: 2→8→32 sessions）。wiki の効果はセッション数に依存した（Anthropic は 2→128 で 13%→48%）。
- **統合が要る質問の割合を操作する**: 単一文書で答えられる問題と、複数文書が要る問題を分ける（Harvey と wiki vs RAG の知見）。

### 4.5 統計

- **seed（または問題順の系列）を再標本化の単位**にする（Self-Cleaning の design effect 3.75）。分布と二峰性を図で示し、平均だけを報告しない。
- paired bootstrap / McNemar（社内の前例）に加え、系列単位の cluster bootstrap を使う。主仮説は事前に登録し、その他は探索扱いにする。Holm 補正を使う（DreamBench-SWE の作法）。
- 判定は機械判定（SQL の正誤、真理規則）を主にする。LLM judge は補助にとどめ、judge 間の一致度も報告する（wiki vs RAG では judge によって ρ が 0.04〜0.81 と大きく変わった）。

### 4.6 M4 16GB＋Foundry での実行可能性と役割分担の案

- **ローカルで実行するもの**
  - オーケストレーション（asyncio）
  - ファイルストア＋git
  - LanceDB または sqlite-vec
  - bm25s
  - 埋め込み（ruri-v3-310m か Qwen3-Embedding-0.6B）
  - reranker（bge-reranker-v2-m3）
  - CL-Bench の SQLite
- **Foundry**
  - agent 本体: gpt-6-luna（安価）を主、gpt-6-sol を感度分析に使う。
  - analyzer / organizer: agent と同じモデル（公平性のため）。「強い統合器」は別条件として gpt-6-sol で試す。
  - judge: 別系列のモデル。例: Foundry 上の claude-sonnet-5-5（GA）。ただし temperature などのパラメータ制約を事前に確認する。
  - Claude を使えば Anthropic の数値（Opus 5.5 / Sonnet 5.5）の追試もできるが、Dreams API は使えないため cookbook 方式で再実装する。
- **コストの粗い見積もり**（純粋な推論。単価は gpt-6-luna の short context、cached input の効果は考慮しない）
  - CL-Bench DB の 1 run（40 問×最大 16 呼び出し×1 呼び出しあたり約 8〜30K input tokens と仮定）は 5〜20M input tokens 程度になり、luna で $0.5〜$2 程度。
  - 6 条件×10 seeds で数十ドル規模になる。
  - ICL 群は履歴が伸びるので上振れする。Dreaming の offline 分は cookbook の実測（11 transcripts で $0.36、Anthropic モデル）程度のオーダーと想定する。
  - **パイロット 1 run で実測して補正すること**。
- **パラメトリック（SLEEP 型）**: Qwen3.5-2B/4B の 4bit で MLX LoRA SFT による「Dreaming 出力の焼き込み」を探索的な付録として行う。主仮説にはしない。

---

## 5. 不確実性・未確認事項

- **CL-Bench**
  - Anthropic の「CL-Bench 1.0 database task」と公開リポジトリの `database_exploration` の default schedule が同一かは確かめていない（drift の有無、指標が mean reward か score か）。
  - DB の元データ McAuley-Lab/Amazon-Reviews-2023 と、派生データ `continual-learning-bench/database-exploration` の HF カードにライセンスが書かれていない。
  - 論文 Table 1 のコスト（$/run）の単位（全ドメイン合計か）を読み切れていない。
  - リーダーボードの avg reward（例: 0.196）と論文の normalized reward（22.3%）は値が違い、版か指標の差とみられるが確認していない。
- **2026 年の arXiv 論文の大半**: abstract（一部は HTML 前半）しか読んでいない。表の全数値、実験設定の細部、コードの有無は本文で確かめる必要がある。
  - 特に MindMemOS の dreaming の 2 段 LLM 呼び出しは検索スニペットにしかなく、abstract ページでは確認できなかった。
  - 同様に MindMemOS と SCM の ablation、MemConflict の評価対象システム名、MemSecBench の構成名も未確認。
- **SLEEP**: base model の規模、計算量、コード公開の予定は不明。
- **Foundry**
  - GPT-6 の short / long context の閾値。
  - gpt-6 系の Flex processing の正式な対応状況（Retail API にはメータがある）。
  - 日本リージョンでの提供状況（region availability ページは未確認）。
  - Foundry 上の Claude の価格、Claude Opus 5.5 / Sonnet 5.5 で temperature を指定できるか。
  - claude-haiku-5-5 の lifecycle。
- **OpenAI Dreaming の数値**（82.8% vs 41.5%）は二次情報にしかなく、公式ページには載っていない。
- **MLX の 16GB での実測**（メモリ量、tokens/s）は二次情報しかない。パイロットで計測が必要。
- **Evo-Memory、AgentCL、ContinualSkillBench、STALE、MemSecBench** のコードやデータの公開状況は確認できなかった。
- **データのライセンス**: MemoryAgentBench（MIT）は既存ベンチを再構成したもので、構成元ごとのライセンスは未確認。LongMemEval-V2 のデータ（Apache-2.0）に含まれる web 由来コンテンツの権利関係も未確認。
- **ベンダー報告値**: Mem0 v3 の LoCoMo 92.5 と LongMemEval 94.4 は managed 版のベンダー報告で、独立検証はされていない。
- **Anthropic 記事の数値**は単発 run・自社ベンチである（brief に記載のとおり）。本サーベイでも独立追試は見つからなかった。

---

## 6. 参考文献

### Dreaming・統合・メモリ方式

- Anthropic, The components of agent memory: https://www.anthropic.com/research/components-of-agent-memory
- Anthropic, Managed Agents Dreams docs: https://platform.claude.com/docs/en/managed-agents/dreams
- Anthropic, Dreaming with a dynamic workflow (cookbook): https://platform.claude.com/cookbook/managed-agents-cma-dreaming-with-a-dynamic-workflow
- Anthropic, claude-quickstarts knowledge-wiki: https://github.com/anthropics/claude-quickstarts/tree/main/managed-agents/knowledge-wiki
- Claude Code memory docs: https://code.claude.com/docs/en/memory
- OpenAI, Dreaming: Better memory for a more helpful ChatGPT (2026-06-04): https://openai.com/index/chatgpt-memory-dreaming/
- Letta, Context Repositories: https://www.letta.com/blog/context-repositories
- Letta docs, Memory & dreaming: https://docs.letta.com/letta-code/memory
- Letta repo: https://github.com/letta-ai/letta
- Auto-Dreamer (arXiv 2605.20616): https://arxiv.org/abs/2605.20616
- MindMemOS (arXiv 2608.12428): https://arxiv.org/abs/2608.12428
- REALM, Retrieval-Driven Memory Reconsolidation (arXiv 2609.16053): https://arxiv.org/abs/2609.16053
- TrustMem (arXiv 2606.25161): https://arxiv.org/abs/2606.25161
- LightMem (arXiv 2510.18866): https://arxiv.org/abs/2510.18866 / https://github.com/zjunlp/LightMem
- Sleep-time Compute (arXiv 2504.13171): https://arxiv.org/abs/2504.13171 / https://github.com/letta-ai/sleep-time-compute
- Mem0 (arXiv 2504.19413): https://arxiv.org/abs/2504.19413 / https://github.com/mem0ai/mem0 / https://github.com/mem0ai/memory-benchmarks
- Zep (arXiv 2501.13956): https://arxiv.org/abs/2501.13956 / https://github.com/getzep/graphiti
- A-MEM (arXiv 2502.12110): https://arxiv.org/abs/2502.12110 / https://github.com/WujiangXu/A-mem
- MemOS (arXiv 2507.03724): https://arxiv.org/abs/2507.03724 / https://github.com/MemTensor/MemOS
- MemoryOS (arXiv 2506.06326): https://arxiv.org/abs/2506.06326 / https://github.com/BAI-LAB/MemoryOS
- LangMem: https://github.com/langchain-ai/langmem
- Cognee: https://github.com/topoteretes/cognee
- Memento-Skills: https://github.com/Memento-Teams/Memento-Skills
- EverMemOS: https://github.com/EverMind-AI/EverMemOS
- ACE (arXiv 2510.04618): https://arxiv.org/abs/2510.04618
- Evo-Memory (arXiv 2511.20857): https://arxiv.org/abs/2511.20857
- Reflexion: https://github.com/noahshinn/reflexion
- ExpeL (arXiv 2308.10144): https://arxiv.org/abs/2308.10144 / https://github.com/LeapLabTHU/ExpeL
- Agent Workflow Memory (arXiv 2409.07429): https://arxiv.org/abs/2409.07429 / https://github.com/zorazrw/agent-workflow-memory
- ReasoningBank (arXiv 2509.25140): https://arxiv.org/abs/2509.25140
- SSGM (arXiv 2603.11768): https://arxiv.org/abs/2603.11768
- SCM (arXiv 2604.20943): https://arxiv.org/abs/2604.20943
- Memory for Autonomous LLM Agents (survey, arXiv 2603.07670): https://arxiv.org/abs/2603.07670

### パラメトリック・継続学習

- SLEEP (arXiv 2606.03979): https://arxiv.org/abs/2606.03979
- Google Research blog, The SLEEP framework (2026-10-27): https://research.google/blog/the-sleep-framework-a-new-way-for-llms-to-retain-knowledge-in-continual-learning/
- Do Language Models Need Sleep? Offline Recurrence (arXiv 2605.26099): https://arxiv.org/abs/2605.26099
- Memory Depth, Not Memory Access / EVAF (arXiv 2606.26806): https://arxiv.org/abs/2606.26806
- Supersede (arXiv 2606.27472): https://arxiv.org/abs/2606.27472 / https://github.com/Vrin-cloud/supersede
- SEAL (arXiv 2506.10943): https://arxiv.org/abs/2506.10943
- PEAM (arXiv 2605.27762): https://arxiv.org/abs/2605.27762
- mlx-lm: https://github.com/ml-explore/mlx-lm
- mlx-lm-lora: https://github.com/Goekdeniz-Guelmez/mlx-lm-lora

### ベンチマーク

- CL-Bench paper (arXiv 2606.05661): https://arxiv.org/abs/2606.05661
- CL-Bench repo: https://github.com/pgasawa/continual-learning-bench
- CL-Bench site: https://www.continual-learning-bench.com
- CL-Bench 1.0 news: https://continual-learning-bench.com/news/cl-bench-1-0/
- Snorkel CL-Bench leaderboard: https://snorkel.ai/leaderboard/continual-learning-bench/
- Amazon-Reviews-2023 (CL-Bench DB の元データ): https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023
- (名前衝突) CL-bench: A Benchmark for Context Learning (arXiv 2602.03587): https://arxiv.org/abs/2602.03587
- LongMemEval: https://github.com/xiaowu0162/LongMemEval
- LongMemEval-V2: https://github.com/xiaowu0162/LongMemEval-V2 / https://xiaowu0162.github.io/longmemeval-v2/
- MemoryAgentBench (arXiv 2507.05257): https://arxiv.org/abs/2507.05257 / https://github.com/HUST-AI-HYZ/MemoryAgentBench
- LoCoMo: https://github.com/snap-research/locomo
- BEAM (arXiv 2510.27246): https://arxiv.org/abs/2510.27246 / https://huggingface.co/datasets/Mohammadta/BEAM
- HaluMem: https://github.com/MemTensor/HaluMem
- MemBench: https://github.com/import-myself/Membench
- MemoryArena (arXiv 2602.16313): https://arxiv.org/abs/2602.16313 / https://github.com/ZexueHe/MemoryArena
- STALE (arXiv 2605.06527): https://arxiv.org/abs/2605.06527
- MemConflict (arXiv 2605.20926): https://arxiv.org/abs/2605.20926 / https://github.com/TaoZhen1110/MemConflict
- AgingBench (arXiv 2605.26302): https://arxiv.org/abs/2605.26302 / https://github.com/VITA-Group/AgingBench
- DreamBench-SWE (arXiv 2608.20664): https://arxiv.org/abs/2608.20664 / https://github.com/iroiro147/dreambench-swe
- AgentCL (arXiv 2606.02461): https://arxiv.org/abs/2606.02461
- ContinualSkillBench (arXiv 2608.03874): https://arxiv.org/abs/2608.03874
- Harvey LAB: https://github.com/harveyai/harvey-labs
- Vector RAG vs LLM-Compiled Wiki (arXiv 2605.18490): https://arxiv.org/abs/2605.18490

### 失敗モード・安全

- The Memory Trust Gap (arXiv 2609.01852): https://arxiv.org/abs/2609.01852
- When Stale Constraints Go Unchecked (arXiv 2608.25553): https://arxiv.org/abs/2608.25553 （data: https://doi.org/10.5281/zenodo.22147784）
- Revoked but Still Authoritative (arXiv 2609.08258): https://arxiv.org/abs/2609.08258 / https://github.com/VulcanLab/Memory-Rebirth-Attack
- Self-Cleaning and Captured Anyway (arXiv 2609.25052): https://arxiv.org/abs/2609.25052
- The Self-Correction Illusion (arXiv 2606.05976): https://arxiv.org/abs/2606.05976
- From Faulty Memories to Corrected Actions (arXiv 2608.10502): https://arxiv.org/abs/2608.10502 / https://github.com/xiaoqi-7/memory-rollback
- MemSecBench (arXiv 2607.27080): https://arxiv.org/abs/2607.27080
- From Untrusted Input to Trusted Memory (arXiv 2606.04329): https://arxiv.org/abs/2606.04329
- Poison Once, Exploit Forever / eTAMP (arXiv 2604.02623): https://arxiv.org/abs/2604.02623
- What to Keep, What to Forget (arXiv 2607.08032): https://arxiv.org/abs/2607.08032

### モデル・価格（Azure Foundry）

- Microsoft Learn, Foundry Models sold by Azure: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure
- Microsoft Learn, Foundry Models from partners: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-from-partners
- Microsoft Learn, Claude models in Microsoft Foundry: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models
- Microsoft Learn, Region availability: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability
- Azure blog, GPT-6 Astra, Sol, and Luna in Microsoft Foundry: https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/
- Azure OpenAI pricing: https://azure.microsoft.com/pricing/details/azure-openai/
- Azure Retail Prices API（価格の機械確認に使用）: https://prices.azure.com/api/retail/prices

### ローカルモデル（Hugging Face）

- https://huggingface.co/Qwen/Qwen3.5-4B
- https://huggingface.co/Qwen/Qwen3.5-2B
- https://huggingface.co/Qwen/Qwen3.5-9B
- https://huggingface.co/google/gemma-4-E4B-it
- https://huggingface.co/microsoft/Phi-4-mini-instruct
- https://huggingface.co/ibm-granite/granite-4.0-micro
- https://huggingface.co/openai/gpt-oss-20b
- https://huggingface.co/sbintuitions/sarashina2.2-3b-instruct-v0.1
- https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- https://huggingface.co/BAAI/bge-m3
- https://huggingface.co/intfloat/multilingual-e5-small
- https://huggingface.co/cl-nagoya/ruri-v3-310m
- https://huggingface.co/BAAI/bge-reranker-v2-m3
- https://huggingface.co/Qwen/Qwen3-Reranker-0.6B

### 実装ライブラリ

- LanceDB: https://github.com/lancedb/lancedb
- sqlite-vec: https://github.com/asg017/sqlite-vec
- Chroma: https://github.com/chroma-core/chroma
- DuckDB: https://github.com/duckdb/duckdb
- bm25s: https://github.com/xhluca/bm25s
- rank_bm25: https://github.com/dorianbrown/rank_bm25
- tantivy-py: https://github.com/quickwit-oss/tantivy-py
- sentence-transformers: https://github.com/UKPLab/sentence-transformers
- fastembed: https://github.com/qdrant/fastembed
- openai-python: https://github.com/openai/openai-python
- openai-agents-python: https://github.com/openai/openai-agents-python
- pydantic-ai: https://github.com/pydantic/pydantic-ai
- smolagents: https://github.com/huggingface/smolagents
- Microsoft Agent Framework: https://github.com/microsoft/agent-framework
- anthropic-sdk-python: https://github.com/anthropics/anthropic-sdk-python
- claude-agent-sdk-python: https://github.com/anthropics/claude-agent-sdk-python
- instructor: https://github.com/jxnl/instructor
- litellm: https://github.com/BerriAI/litellm
- Ollama: https://github.com/ollama/ollama
- llama.cpp: https://github.com/ggml-org/llama.cpp
- letta-evals: https://github.com/letta-ai/letta-evals
- 避けるもの（参考）
  - mlx-embeddings（GPL-3.0）: https://github.com/Blaizzy/mlx-embeddings
  - OpenViking（AGPL-3.0）: https://github.com/volcengine/OpenViking
  - basic-memory（AGPL-3.0）: https://github.com/basicmachines-co/basic-memory
