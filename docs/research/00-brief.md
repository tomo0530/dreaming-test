# 共通ブリーフ: Dreaming（エージェントメモリのオフライン統合）技術検証

作成日: 2026-11-10
用途: 調査担当3者（Phase 1）とプラン担当3者（Phase 2）に、同じ内容で渡す前提資料。

## 1. 背景と目的

- 社内の「新規技術検証」（Notion DB、DSD / FY26H2）の1件として、**Dreaming** の有効性を検証したい。
- 成果物は、過去の検証レポート2件と同じ粒度にする。具体的には、仮説、比較群、データセット、評価指標、統計、結果、考察、「示せたこと／示せていないこと」の表、実務者への示唆を含む Notion レポートを想定する。
- 本リポジトリ `dreaming-test`（uv workspace / ABEJA DS プロジェクトテンプレート）で実験コードを書き、実行する前提である。

## 2. 検証対象

### 2.1 Anthropic「Components of agent memory」（2026）

- URL: https://www.anthropic.com/research/components-of-agent-memory
- メモリの階層（下から上の順）
  1. Transcripts（検索可能なイベントログ）
  2. Memory notes（セッション中に書く観察）
  3. Memory representations（wiki など、リンクされた構造）
  4. Injected context（毎セッションに注入する小さな層）
- **Dreaming** は、セッションの合間にオフラインで実行される。transcript を ground truth として再生し、notes を検証・訂正する。そのうえで経験をルール・wiki ページ・リンクに蒸留し、重複をまとめ、詳細は下位層に移してリンクだけを残す（注入層を小さく保つ）。複数セッションにまたがって繰り返し実行する。
- 報告値の例
  - **CL-Bench 1.0** database task（stateless → with memory）: Opus 5.5 は 0.655→0.838、Sonnet 5 は 0.306→0.610、Sonnet 4.6 は 0.330→0.364（プロンプト改善後 0.641）。モデルが記憶を読みに行くかどうかが効く。
  - 「失敗だけ記録せよ」と指示すると利得が下がった（Opus 5 で −0.234）。
  - Pokémon Red: 状態を大量に記録すると陳腐化した。戦略や検証済みルールを少量残すほうが強かった。
  - 誤ったメモを dreaming で訂正すると、詰まっていた地点を突破できた（Mt. Moon）。
  - Wiki（Stainless コーパス、約 420M tokens、25問）: dreaming を 2→128 セッションに増やすと、wiki 単独のスコアが 13%→48% に上がった。wiki＋検索は 61%、検索単独は 53%。1問あたり約 2 万 tokens 節約できた。
  - Harvey LAB: 複数文書の統合が要る問題では wiki 87%、検索 73%。単一文書で答えられる問題では差がなかった。wiki がソースの約 1.77 倍に肥大化した例もある。
  - 契約条項: 共有表に書くと 41/57、契約ごとの逐語ページにすると 57/57。
- 限界として挙げられている点: 計算コスト、誤記憶の固定化・伝播、環境変化によるドリフト、注入層の肥大化、wiki の肥大化、評価がほぼ自社ベンチ・単発 run であること。

### 2.2 Cookbook「Dreaming with a dynamic workflow」（Claude Managed Agents）

- URL: https://platform.claude.com/cookbook/managed-agents-cma-dreaming-with-a-dynamic-workflow
- 構成
  - session transcript を labeled plain text でエクスポートする。
  - lead（memory-distiller）が workflow script を書いて起動する。
  - Analyze 相: transcript ごとに transcript-analyzer を1つ割り当てる（約20並列）。手順は verify → enrich → organize。
  - Organize 相: memory-organizer を1回だけ実行する。重複の統合、リンクと frontmatter の修正、`_index.md` の補完を行う。
  - 出力は Markdown の memory store。`_index.md` を入口とし、各ファイルに frontmatter と `[[slug]]` リンクを持つ。`CONFLICT:` 行、`MOVED TO` スタブ、1ファイル 100 KB 上限を使う。
  - consumer セッションは store を read-only でマウントする。
- 非 Anthropic LLM で再実装するための指針も cookbook に書かれている。
  - memory store はディスク上のディレクトリにし、Git で監査する。
  - lead は通常のコードに置き換えてよい。
  - analyzer と organizer は、5つの sandboxed file tool（read/write/edit/glob/grep）を持つ tool-calling loop にする。
  - 並列度は asyncio.Semaphore(20) で制御し、書き込みはロックで直列化する。
- cookbook が提案する評価ハーネス: coverage、fact recall、staleness（supersede）、conflict、prompt injection 耐性、secret 非記録、index 整合性、cost。
- 実測値: 11 transcript（約 97K tokens）で $0.36・約 2.5 分。約 50 本の長い transcript（約 12 MB）で約 $8・約 10 分。

### 2.3 Google Research「The SLEEP framework」（2026-10）

- URL: https://research.google/blog/the-sleep-framework-a-new-way-for-llms-to-retain-knowledge-in-continual-learning/ 、論文 arXiv:2606.03979
- wake / sleep の2相で動く。
  - sleep 相 Stage 1（consolidation）: 低ランク expert を追加し、GKD で短期記憶を長期パラメータへ蒸留し、一時パラメータを破棄する。
  - sleep 相 Stage 2（dreaming）: ランダム expert で合成データを作り、勾配ベースで選別し、RL で改善する。
- **重みを更新する**方式であり、Anthropic の Dreaming（重み固定・外部メモリ）とは別物である。Azure の API モデルでは再現できない。ローカル小型モデルで縮小再現できるかは要検討とする。
- 報告値: Manchu / Kalamang の continual translation で忘却がほぼない、BABILong 10M tokens、AIME24 79.2% など。

## 3. 先行社内検証の要点（同じ DB 内の過去レポート）

### 3.1 自己進化AIエージェントフレームワークを試してみた（2026-05）

- 比較対象: OpenSpace / Memento-Skills / GenericAgent。Azure OpenAI（agent: gpt-5.4-mini、sub: nano、judge: gpt-5.4）を使用。
- 4相の評価
  - baseline（artifact なし）
  - learn（READ_WRITE）
  - warm_replay（READ_ONLY、同じタスク）
  - transfer_holdout（READ_ONLY、未学習タスク）
- 指標: reuse_gain、transfer_gain、promoted/candidate、tokens_per_success、safety events。
- データ: GDPVal / SkillsBench / GAIA Lv1 から 12 タスク、1 seed。
- 結果: generic_agent の reuse_gain が +0.111（実質 1 タスク分）、openspace は −0.222 だが transfer は +0.333。
- 教訓
  - n=12・1 seed では統計的な主張ができない。
  - 未検証のコードを skill 化すると「完了したという主張」として再生され、退行した。
  - 幻覚した数値が skill に固定化された。
  - promotion gate が必要。
  - artifact の中間根拠が正しいかは、最終回答の正誤とは別の軸で評価すべき。
  - Memento-Skills は「Dream Daemon」を持つ。

### 3.2 State-Aware RAG を試してみた（2026-07）

- 主仮説を1つ（H2）に絞り、残り4つを探索的仮説と事前に宣言した（多重性対策）。
- 「藁人形」を避けるため、強い対照（Rolling-Summary）を置いた。呼び出し回数と予算を揃え、差分は能動キュレーションの有無だけにした。
- 自作データセット StateDrift-JA を用意した。
  - 架空企業の社内文書 472 件。
  - 更新・廃止・矛盾・回答不能を作り込んだ。
  - 真理規則で gold を機械判定できるようにした。
  - 3つの AI が分担して執筆し、リンタで検証した。
- MuSiQue 100問と組み合わせて評価した。
  - 判定: Judge 3回多数決、paired bootstrap、McNemar、cluster bootstrap。
  - Pareto 図（精度 vs コスト）とエラー分類も作った。
- 結果: 能動キュレーションは汎用要約に勝てなかった。ボトルネックは記憶ではなく reader 側にあった。文脈圧が低いと「捨てる」機能の出番がない。
- 教訓
  - 手法が効くはずの条件（長い horizon、ノイズや矛盾の蓄積）を、実験内で実際に作れているかを事前に確かめる。
  - 「正しい文書が渡っているのに間違える率」を測る。

## 4. 制約

- **計算環境**: 可能な限り MacBook Pro M4 / 16GB（ユニファイドメモリ）のローカル環境で検証する。GPU サーバは前提にしない。ローカルで動かすのはオーケストレーション、ファイルベースのメモリストア、ベクトル DB、小型 embedding / reranker、必要なら小型ローカル LLM（MLX / llama.cpp / Ollama で 4bit 量子化の 3〜8B 程度）。
- **LLM**: Azure Microsoft Foundry でデプロイできるモデルを使う（例: gpt-6-luna）。役割ごとに（agent 本体、dreaming の analyzer / organizer、judge、embedding）どのモデルを使うか、コストを含めて検討する。Foundry カタログにある OpenAI 以外のモデル（例: Foundry 経由で使えるモデル群）も候補にしてよい。ただし、デプロイ可能かは一次情報で確認すること。
- **再現性・公平性**: 比較群の間では、モデル、温度、予算、呼び出し回数をできるだけ揃える。主仮説は1つに絞り、seed を複数にするか問題数を増やして、統計的に主張できる規模にする。
- **安全性**: 秘密情報・PII を扱わない。transcript を命令として扱わない（prompt injection 耐性も評価対象にする）。
- **ライセンス**: 使うのは MIT / Apache-2.0 / BSD の OSS とデータセットに限る。GPL / copyleft と、商用利用不可のデータは避けるか、明記する。
- **依存追加**: 実装段階でユーザーの承認を得る（本タスクはプラン作成まで）。

## 5. 未決事項（プラン側で提案してほしい点）

- Dreaming の効果を最もきれいに切り分けられる比較群。例:
  - no-memory
  - raw transcript 検索（RAG over transcripts）
  - オンラインで notes を書くだけ（dreaming なし）
  - notes＋dreaming
  - wiki 化 dreaming
  - 既存 OSS メモリ（mem0 / Letta / Zep 等）
- タスクとデータセット。例:
  - CL-Bench のような逐次学習タスク
  - LongMemEval / LoCoMo のような長期対話記憶
  - 自作の「環境がドリフトする」逐次タスク群
  - コードベース運用タスク
- 「dreaming が効くはずの条件」を実験内で確実に作る方法。セッション数、誤記憶の注入、ドリフト、複数文書の統合が要る質問などが考えられる。
- 評価指標と統計設計、コスト見積もり、工程（週単位）。
