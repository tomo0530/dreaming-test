# Dreaming 技術検証計画

**基準日：2026-11-10／期間：4週間を基本、最大5週間／担当：データサイエンティスト1名**

本計画は指定された brief と3件の survey を読み、採用判断に関わるモデル、価格、ライセンス、実験結果を一次資料で照合したもの。以下の数値は、明記した既報値を除き、**実験設計値・見積値であり、実測結果ではない**。

## 1. 概要 / 一行結論

**長期・ドリフト・誤記憶を含む環境で、原ログによる検証・訂正を伴う Dreaming が、同じ計算予算を使う強い通常要約より最終回答正答率を5ポイント以上改善するかを確かめる。**

主対象は、**重みを更新しない外部メモリのオフライン統合**である。Google SLEEP 型の学習、LoRA、GRPO、実行可能な skill の自動生成は対象外とする。

| 項目 | 採用方針 |
|---|---|
| 主実験 | 自作の日本語・逐次業務環境160個、各64セッション、3反復 |
| 主比較 | Source-verified Dreaming 対 budget-matched Rolling Summary |
| 強い参照群 | Raw transcript hybrid RAG、全文 ICL |
| 外部ベンチ | LongMemEval-S の事前抽出150問 |
| 主評価 | 機械採点した最終回答の正答率 |
| 独立評価 | 記憶内容の正確性、検索、reader、費用、安全性 |
| ローカル実行 | データ生成、検索、embedding、store、統計、監査 |
| API | Foundry の Luna を生成系、Sonnet 5.5 を補助 Judge |
| 費用 | 中央見積約560 USD、予備費込み約730 USD。停止上限900 USD |
| 成果 | 再実行可能なハーネス、固定済みデータ・設定・結果、Notion向け検証レポート |

**「主仮説を支持した」と「実務導入すべき」は分ける。** 通常要約に勝っても、RAGや全文 ICL が同等以上で安価なら、Dreaming の導入は推奨しない。

---

## 2. 検証の問いと仮説

### 2.1 主仮説 H1：計算予算を揃えた追加価値

> 64セッションまでの履歴、仕様変更、複数文書統合を含む事前定義の評価分布において、Source-verified Dreaming は、同一モデル・maintenance呼出回数・token上限を持つ Rolling Summary より最終回答正答率を改善する。

評価対象は、後述する**誤メモ注入なし80環境、10%注入あり80環境の等重み混合**。自然環境での誤記憶発生率を推定する実験ではない。

環境 \(w\)、反復 \(r\) の正答率を \(A_{w,r}\) とし、

\[
d_w=\frac{1}{3}\sum_{r=1}^{3}
\left(A^{Dream}_{w,r}-A^{Summary}_{w,r}\right)
\]

\[
\Delta=\frac{1}{160}\sum_{w=1}^{160}d_w
\]

を主効果とする。

**成功条件は両方を満たすこと。**

1. \(\hat{\Delta}\geq 0.05\)：正答率差が **+5 percentage points以上**。
2. 環境単位の paired cluster bootstrap による、差の両側95%信頼区間下限が **0を超える**。

主解析は32・64セッション終了後の各8問、計16問／環境。早期checkpointや公開ベンチとの成績を混ぜない。

### 2.2 探索的仮説

| ID | 問い | 事前に置く数値目標 |
|---|---|---|
| E1：修復 | 誤記憶を訂正できるか | 注入誤メモの80%以上を1 dreaming cycle以内に訂正または明示的に無効化。正しい記憶の破壊率2%以下 |
| E2：ドリフト | 古い仕様への依存を減らすか | Summary比で stale-answer率を相対30%以上削減。過去時点を問う問題の正答率低下は2ポイント以内 |
| E3：統合 | 複数文書が必要な問題で特に効くか | multi-document問題の改善幅がsingle-document問題より5ポイント以上大きい |
| E4：reader | 記憶ではなくreaderが限界か | 正しい最小根拠を与えてもLunaの誤答率が10%以上、かつ同一根拠でSolが10ポイント以上改善すればreader bottleneckと判定 |
| E5：効率 | 少ない読取り量で品質を保てるか | Dreamingのreader evidence予算4KがSummaryの8Kに対して正答率差−2ポイント以内、かつ実消費入力tokenを25%以上削減 |
| E6：安全性 | 毒性情報を永続化・実行しないか | 独立した安全性テスト300件で、禁止操作の成功0件、秘密canaryの派生メモリ保存0件 |

E1の「無効化」と「正しい値への修復」は別々に報告する。削除するだけで修復に成功したとは解釈しない。

E5の非劣性は信頼区間で確認できた場合のみ主張する。小規模感度分析で区間が広ければ「未判定」とする。探索的結果から主仮説を差し替えない。

### 2.3 実務導入への追加条件

H1に加え、次を満たす場合に限定的な導入候補とする。

- 記憶の根拠付き主張率98%以上、正しい記憶の破壊率2%以下。
- 誤メモ注入なし条件で、明確な性能退行がない。
- 重大な安全性違反0件。
- 実際に想定するquery数で、RAG・全文 ICLとの精度／総費用の比較に優位性がある。

---

## 3. 比較群

### 3.1 主実験の6群

| 群 | 実現方法 | 位置づけ・藁人形でない理由 |
|---|---|---|
| B0：No-memory | 現在の質問と共通タスク仕様のみ。過去履歴なし | 記憶が必要な問題になっているかを確認する下限参照。主対照にはしない |
| B1：Raw-RAG | 原ログをimmutable保存。BM25＋dense検索、日時・出典、前後chunkを返す | 要約損失がなく、古い情報と訂正の両方を取得できる強い対照 |
| B2：Online Notes | セッションごとに生成したnotesを追記。notesと原ログを同じ検索器で検索 | offline処理なしの実用構成。原ログへのfallbackを禁止しない |
| B3：Rolling Summary | セッション単位の要約＋定期的なtopic別rolling summary。原ログ・notes・前回summaryを利用 | 日付、否定、例外、出典、更新を保持するよう指示し、誤りの訂正も許す強い主対照 |
| D：Verified Dreaming | analyzerで主張と原ログを照合し、organizerで訂正・supersede・統合・リンク整理 | 主検証対象 |
| B4：Full-history ICL | 評価時点までの全原ログを時系列で入力 | 現行モデルの大きなcontextと低価格を踏まえた実用上の強い参照。性能上限とは断定しない |

全群を160環境×3反復で評価する。B4は文脈予算を揃えない別軸の比較であり、**精度・費用・遅延を含めた実用比較**として扱う。

### 3.2 主比較で揃えるもの

B3とDでは以下を同一にする。

- agent、notes writer、analyzer、organizerのモデルversionとreasoning設定。
- 同一の原ログ、初期notes、誤記憶注入位置。
- 8セッションごとのmaintenance。
- 1セッションにつきanalyzer **2 LLM calls**。
- 1 cycleにつきorganizer **2 LLM calls**。
- 64セッション全体で **144 maintenance calls／群／反復**。
- active memory上限16K tokens、常時注入index上限1K。
- 同一の検索器、chunking、retrieval候補数、tool権限。
- 同一の構造・アクセス制御・canary除去gate。
- readerは2 calls、取得したmemory／原ログの総量上限8K tokens。

2回目の呼出しは、B3では要約の点検・修正、Dでは証拠照合結果と更新案の点検に使う。**ダミー呼出しで数だけ合わせない。**

B3にも原ログ、引用、訂正能力を与える。差分は、**体系的な主張単位の再検証・状態遷移管理を明示的に行うか**であり、「正しい情報を渡す群と渡さない群」ではない。

### 3.3 予算一致の限界を明示する

同一のtoken上限でも、実際の出力長・reasoning量は一致しない。

- 「同一上限・同一呼出回数」と「実消費token一致」を別に記録する。
- B3とDの実消費費用差が10%を超えた場合、**等実費比較だったとは表記しない**。
- devでB3の有用な再読・点検量を調整し、Dだけ多く計算する状態を避ける。
- 本試験後は設定を変えず、実費差と精度／費用曲線を併記する。
- B0・B1・B2には不要なmaintenanceを課さない。こちらは自然な運用費用で比較する。

### 3.4 機序を調べる小規模ablation

事前抽出した40環境、1反復で追加する。

- **D-flat**：検証・訂正は同じ、wiki linksを使わないflat表現。
- **D-no-reverify**：整理は同じ、既存主張の原ログ再照合を行わない。

主比較は「検証を伴う統合方針」のパッケージ効果。訂正とリンク構造の独立寄与は、この探索的ablationの範囲でのみ論じる。

---

## 4. Dreaming 実装方針

### 4.1 Cookbookの再実装範囲

採用するのは、**transcript → analyzer → organizer → read-only consumer**という構成とMarkdown storeの表現である。Managed AgentsやDreams APIをFoundry上で呼び出す設計にはしない。

lead agentが実行コードを生成する部分は、固定したPythonオーケストレーターに置き換える。cookbookの忠実な性能再現ではなく、**Azureモデルを用いる機能的再実装**とする。[Claude Cookbook](https://platform.claude.com/cookbook/managed-agents-cma-dreaming-with-a-dynamic-workflow)

### 4.2 Transcript形式

正本はJSONL、LLM入力はそこから決定的に変換したlabeled plain text。

必須フィールド：

```text
world_id, session_id, event_id
observed_at, effective_at
role, source_type, authority
tool_name, tool_call_id, tool_status
content, content_sha256
```

表示例：

```text
SESSION world-003/session-024
EVENT e024-017 | observed_at=... | source_type=tool_observation
TOOL RESULT query_config | status=success:
  ...

EVENT e024-018 | source_type=assistant_hypothesis
AGENT:
  ...
```

重要な区別：

- transcriptは「何を発言・観測したか」の証拠であり、全発言が真実ではない。
- assistantの推測・完了宣言は、成功したtool結果と区別する。
- 発言日時と仕様の発効日時を分ける。
- transcript内の見出し・命令文は構造化roleに昇格させない。
- gold ledger、評価質問、正解、採点結果はwriterから見えない場所に分離する。

### 4.3 AnalyzerとOrganizer

**Analyzer**

1. 対象transcript全文、関連notes、関連memoryを読む。
2. 既存主張を `supported / contradicted / unresolved` に分類。
3. 値だけでなく、日時、否定、例外、適用条件、authorityを確認。
4. 新規追加・訂正・supersede・保留をpatchとして提案。
5. 出典event IDと具体的な引用範囲を記録。

対象transcriptに証拠がないだけで、既存主張を削除しない。別セッションの根拠がある可能性を残す。

**Organizer**

1. analyzerのpatchを時系列・安定したID順に取り込む。
2. 重複統合と矛盾の明示、関連ページのリンク更新。
3. 失効した主張から派生したページを再点検対象にする。
4. `_index.md`を更新し、詳細を下位ページへ移す。
5. gate通過後に新しいrevisionを公開する。

### 4.4 Memory store

```text
project_module/
  pyproject.toml
  src/dreaming_test/
    cli.py
    schemas.py
    datasets/
    retrieval/
    memory/
    runners/
    evaluation/
  tests/

configs/
  primary.yaml
  ablations.yaml
  prices.yaml

data/
  manifests/
  synthetic/
  external/

artifacts/<run_id>/<world_id>/<replicate>/<arm>/
  transcripts/                 # immutable
  notes/
  candidates/
  memory/
    _index.md
    entities/
    rules/
    procedures/
    archive/
  audit/
    patches.jsonl
    gate_results.jsonl
    usage.jsonl
    revision_manifest.json

evaluation_private/
  gold_ledger.jsonl
  probes.jsonl

reports/
  tables/
  figures/
```

ページには以下を持たせる。

```text
slug, description, claim_id
status, valid_from, valid_to
source_event_ids, evidence_spans
supersedes, dependencies
last_source_checked, revision
```

- `[[slug]]`リンク、`CONFLICT:`、移動先を示す`MOVED TO` stubを使用。
- 1ファイル100KB以下。ただし通常ページは2K tokens程度を目標。
- active 16K tokensは、**全topicページの合計上限**。ファイルごとの上限ではない。
- 原ログは保存し続ける。active圧縮と全保存量の削減を混同しない。
- `source_checked`は真実の保証ではない。内容の正しさは後段の独立評価で測る。

### 4.5 スケジュールと並列性

```yaml
sessions_per_world: 64
dream_every_sessions: 8
analyzer_calls_per_session: 2
organizer_calls_per_cycle: 2

api_concurrency: 8
organizer_concurrency_per_store: 1
active_memory_tokens: 16000
injected_index_tokens: 1000
reader_evidence_tokens: 8000

retrieval:
  lexical_top_k: 40
  dense_top_k: 40
  rrf_k: 60

reader_calls: 2
transport_retries: 2
```

実験は壁時計の「夜間」ではなく、セッション数で起動する。各cycle完了後に次のセッションへ進む。

analyzerは共通のsnapshotを読み、個別candidate領域へ書く。共有Markdownへの並列直接書込みは避け、organizerだけが新revisionを組み立てる。途中失敗時は旧revisionを維持する。

### 4.6 Promotion / safety gate

全群共通の機械gate：

- schema、引用eventの存在、引用範囲、リンク整合性。
- path traversal、symlink越境、許可以外のファイル操作を拒否。
- synthetic secret-canary、明示的な外部送信指示を派生storeへ保存しない。
- read-only transcriptの変更禁止。
- active容量、ファイル容量、依存先の存在を検査。
- 不正なpatchは隔離し、旧memoryを保持。

D固有の処理は、**原ログと主張の意味的な照合**。goldを参照するvalidatorはpromotionに使わない。

生成された手順は文章としてのみ保存する。今回、生成コードの実行、shell権限の付与、skillの自動インストールは行わない。

安全性評価では、gateで遮断した件数だけでなく、gate前candidateへの混入率も測る。

### 4.7 Git監査

実装時には、実験artifact専用のローカルGit repositoryでcycle単位の変更を記録する設計とする。

- source hash、親revision、prompt hash、model version、設定、usageを関連づける。
- rollbackは過去snapshotへの参照切替。履歴を書き換えない。
- main repositoryへの大量ログcommitや自動pushはしない。
- repositoryのcommit規則に従い、実行開始時に監査用commitの扱いを確定する。本相談ではcommitしない。

### 4.8 採用OSS

| 部品 | 採用候補 | ライセンス |
|---|---|---|
| orchestration | 標準`asyncio`＋薄い独自runner | 既存Python基盤 |
| LLM client | `openai-python`、`anthropic-sdk-python` | Apache-2.0 |
| schema / frontmatter | Pydantic、PyYAML | MIT |
| lexical search | `bm25s` | MIT |
| vector search / statistics | NumPy、SciPy | BSD-3-Clause |
| local embedding runtime | Sentence Transformers、PyTorch | Apache-2.0／BSD-style |
| embedding weights | `multilingual-e5-small` | MIT |
| 可視化 | Plotly | MIT |

この規模では独立したvector DBサーバを置かず、**環境ごとのNumPy行列＋metadata JSONL**で検索する。LanceDBなどの導入は必須にしない。

追加ライブラリ・重み・データはMIT／Apache／BSDを採用基準とし、lock時に推移依存も確認する。指定済みのPython・Git・Docker等の実行基盤まで「3種のライセンスだけで構成される」とは主張しない。

---

## 5. タスク・データセット

### 5.1 主データ：Dreaming-Drift-JA

**架空の業務環境を独立生成する。** 既存StateDrift-JAの472文書が利用できることは前提にせず、設計上の教訓だけを継承する。

| 分割 | 規模 | 用途 |
|---|---:|---|
| dev / pilot | 24環境 | prompt、検索、採点、価格・速度の校正 |
| test | 160環境 | 主仮説の固定評価 |
| 安全性suite | 300個の独立した短い履歴 | injection、canary、越境操作 |
| closed-loop補助 | testから事前抽出20環境 | 実際のagent行動が次の履歴へ影響する動作確認 |

各test環境：

- 64セッション。
- 1セッション約2K tokens、合計約128K tokens。
- 8つの業務テーマを各20環境：設定管理、データ仕様、サポート運用、発注、権限、リリース、品質管理、日程・依存管理。
- entity、数値、規則の組合せ、依存graph、変更時刻周辺の配置を独立生成。
- 名前だけを変えた同一問題の大量複製にはしない。
- devとtestで文書の言い回し、規則の合成パターン、entity集合を分離する。

自作データは第三者文章をコピーせず、generatorとtruth ledgerから作る。配布する場合はApache-2.0を提案し、権利者による付与を確認する。

### 5.2 Dreamingが働く条件の操作

| 条件 | 作り方 | 操作が成立したかの確認 |
|---|---|---|
| 長いhorizon | 64セッション、原ログ128Kに対しreader evidence上限8K | 後半履歴長がreader予算の8倍以上となる環境が90%以上 |
| 文脈圧 | 類似entity、古い仕様、無関係だが自然な業務ログを混在 | oracle最小根拠は4K以内、全文は8Kへ収まらない |
| ドリフト | セッション24・48で仕様変更。明示訂正、部分変更、例外追加を混在 | 変更前後でgoldが実際に変わることを機械検査 |
| 誤メモ | 80環境でnotesの対象主張10%を改変。残り80環境は無注入 | 誤値、単位、条件脱落、成功の誤認を均等に含める |
| multi-document | 規則、例外、対象entity情報を3〜5文書へ分散 | 各根拠を1つずつ除くと答えが決まらなくなることを検査 |
| authority conflict | 新しい低権限の発言と、古いが有効な正式仕様を競合させる | 単純な最新timestamp選択では解けない |
| 低頻度重要情報 | 初期に一度だけ登場する例外を後半で問う | 頻度ベースのpruningだけでは保持しにくいことを確認 |

誤メモはB2・B3・Dへ同じ位置・内容で注入する。Raw-RAGと全文 ICLの原ログは改変しない。したがって、人工的なnotes汚染に対して原ログ保存方式が強いことも正当な結果である。

**自然にwriterが生む誤りと、人工注入した誤りを分けて集計する。**

### 5.3 評価質問

32・64セッション終了後に各8問：

- 4問：複数文書・複数セッション統合。
- 2問：単一文書／単純更新。
- 1問：過去時点の値・適用条件。
- 1問：回答不能または未解消矛盾。

計16問×160環境＝**2,560固有評価問**。3反復で7,680回答／群。

答えは、値・集合・選択した操作・回答不能などの構造化形式とし、機械採点できるようにする。引用の正しさは別指標とする。

評価質問はmemory生成完了後に初めて提示し、回答・採点・goldを後続memoryへ書き戻さない。warm replayは動作確認に限り、主成績に含めない。

### 5.4 固定ログとclosed-loopの区別

主実験は**固定ログreplay**である。方式ごとに違う経験を得たことによる交絡を避け、memory policyの効果を切り分ける。

補助の20環境では、agentがtypedな模擬toolを使い、その実行結果を次セッションの履歴へ入れる。これは実運用との接続確認であり、小規模なため主仮説の統計には加えない。

この区別により、「記憶処理が改善した」と「自律agentの長期作業全体が改善した」を混同しない。

### 5.5 外部ベンチ：LongMemEval-S

- 公式cleanedデータから、履歴重複を確認した上でdev20問、test150問を事前抽出。
- question typeで層化し、subset IDと抽出seedを固定。
- testは2反復、B1・B3・D・B4の4群。
- 公式質問をmaintenanceへ渡さない。
- goldの`answer_session_ids`はoracle診断専用。
- 公式評価基準を参考にするが、Judge変更とsubset利用を明記し、公式leaderboardの再現値とは呼ばない。
- 同一・近重複履歴があれば同じclusterとして扱う。

コードのMITと、公式cleanedデータcardのMIT表記を確認した。採用時にrevision、LICENSE／card、ファイルhashを保存する。[コードLICENSE](https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/LICENSE)、[公式データ](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)

### 5.6 今回採用しないもの

| 候補 | 判断 |
|---|---|
| CL-Bench DB | 方式の参考。元データの許諾が未確定なので主工程に入れない |
| LoCoMo | 公式LICENSEがCC BY-NC 4.0のため不採用 |
| MemoryAgentBench | 再構成元データの権利と少数sequence依存があるため今回は保留 |
| LongMemEval-V2 | agent経験への適合性は高いが、データ容量・追加評価工数から後続案件 |
| MuSiQue等 | 今回のデータライセンス条件を確認せずに流用しない |
| StateDrift-JA原本 | 所在・再利用条件を未確認。必須依存にしない |

LoCoMoを「社内研究だから可」として制約を緩めない。[公式LICENSE](https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt)

---

## 6. 評価指標と統計

### 6.1 指標

| 層 | 指標 | 分母・意味 |
|---|---|---|
| 主指標 | 最終回答正答率 | 事前登録した全評価問。値、集合、時点、回答不能を採点 |
| 最終回答 | stale-answer率、abstention precision/recall、条件別正答率 | 古い値を答えた割合など |
| 記憶品質 | supported claim precision | active memory内の全抽出主張に対する、根拠に支持された主張 |
| 記憶品質 | required fact recall | 評価に必要なgold factのうち、正しい値・条件・時点で残る割合 |
| 遷移品質 | 誤→正、正→誤、脱落、unsupported追加 | cycle前後の主張を対応づける |
| ドリフト | 更新反映遅延、派生記憶の失効漏れ | 変更イベントから正しいrevisionまでのsession数 |
| 検索 | supporting-evidence recall、complete-set recall | 必要な根拠の一部／全部をreaderへ渡せたか |
| reader | oracle evidence時の誤答率 | 正しい最小根拠を与えても解けない割合 |
| 容量 | active／archive／index／全保存tokens、重複率 | 圧縮と全体肥大化を分離 |
| 運用 | p50/p95 query latency、maintenance時間、retry率 | API待ち・検索・生成を分解 |
| 費用 | ingest／write／maintenance／query／judge別USD | 実usageに基づく |
| 安全性 | 保存→検索→行動の各成功率 | 条件付き率とend-to-end率を別表示 |

**最終回答が正しくても根拠memoryが誤っているケース、memoryが正しくても回答が誤るケースを、2×2表で必ず示す。**

### 6.2 Reader-side bottleneckの診断

事前抽出した40環境で、同一質問に対して以下を比較する。

1. **Natural retrieval**：通常の検索結果。
2. **Oracle-source**：goldが指定する最小原文根拠を渡す。
3. **Oracle-memory selection**：生成済みmemoryから、必要主張を含むページを理想的に選択。
4. **Stronger reader**：同じ根拠をSolへ渡す。

解釈：

- 1→3で改善：memoryの検索・配置が主な問題。
- 3→2で改善：memory内の誤り・情報欠落が疑われる。
- 2でも不正解：readerの推論・指示理解の問題。
- 2→4で改善：reader能力依存の証拠。

これらは介入診断であり、単純な差分を厳密な因果寄与率とは呼ばない。

### 6.3 必要サンプル数

**独立単位は環境160個であり、回答数7,680ではない。**

概算として、paired二値回答の不一致率 \(q=0.25\)、環境内の16問における差分の相関 \(\rho=0.15\) を仮定する。

\[
DE=1+(16-1)\times0.15=3.25
\]

\[
n_{\mathrm{eff}}\approx\frac{160\times16}{3.25}=788
\]

両側5%、検出力80%の最小検出差の概算：

\[
MDE\approx\sqrt{\frac{(1.96+0.84)^2\times0.25}{788}}
\approx0.050
\]

別表現では、環境差分 \(d_w\) の標準偏差が約0.225以下なら、160環境で5ポイント差の検出力が約80%となる。

これは**仮定に基づく計画値**。3反復による有効標本増を先取りしていない。pilotで環境差分の分散を確認し、simulationで検出力を再計算する。

- 本試験前に検出力80%を満たさないと分かった場合はNo-Go。
- 本試験を見た後で問題数を増減したり、成功閾値を緩めたりしない。
- 「有意差検出80%」と「推定差も5ポイント以上となる確率」は違う。真の差がちょうど5ポイントなら、後者の条件通過率はおおむね50%となるため、両方を分けて説明する。

### 6.4 検定と事前登録

- 主推定：環境ごとのpaired差を平均。
- 95% CI：環境単位のpaired cluster bootstrap、10,000 resamples。
- 各環境の全質問・全3反復をまとめて再標本化。
- 注入なし／ありの比率は層化bootstrapで維持。
- 補助：環境単位のpaired permutation。
- 生の7,680回答を独立とみなすMcNemar検定は使用しない。
- 探索的比較にp値を付ける場合は、事前定義の比較family内でHolm補正。
- 平均だけでなく、環境差分の分布、反復別結果、下位10%環境、二峰性を示す。

事前登録には、dataset hash、全split、除外規則、主比較、主指標、検定、seed／run ID、prompt、モデルversion、停止条件を含める。

APIのseed指定が使えない場合は独立run IDと実行順を記録し、「seedで完全再現可能」とは表記しない。

### 6.5 Judgeと人手監査

- 自作主データの最終回答は機械採点。LLM Judgeを主指標の決定者にしない。
- LongMemEvalの自由文採点はSonnet 5.5を3回呼び、ラベル多数決と不一致率を記録。
- memory意味評価は構造化主張とgold ledgerの照合を基本とし、自由文部分をJudgeで補助。
- 人手監査：memory主張300件、外部ベンチ回答100件をarm名を隠して確認。
- 誤→正／正→誤は過抽出して原因分析し、全体率は抽出確率で重み付けする。
- 自動採点と人手の一致率95%未満なら、採点結果を確定しない。

Judgeの3票は独立した3人の評価ではない。同一モデルに共通する偏りは残る。

### 6.6 失敗・欠測

- output schema違反、意味的な失敗、予算超過は失敗として分母に残す。
- schema修復は既定の2 calls内で行い、特定群だけ追加推論しない。
- 429・通信エラーは共通規則で最大2回retryし、usageを費用に含める。
- 大規模なサービス障害はarm横断の実行block単位で扱う。
- 欠測が1%を超えた場合は、その理由と感度分析を示すまで結論を保留する。

---

## 7. モデル構成

### 7.1 役割別

| 役割 | 第一候補 | 理由 | Foundry／利用確度 |
|---|---|---|---|
| agent / reader | `gpt-6-luna`、2026-09-22版 | 大量反復を実行できる費用。全armで固定 | Microsoft公式catalog掲載確認。対象subscriptionでのdeploymentは未確認 |
| online notes writer | 同じLuna | writer能力を交絡させない | 同上 |
| analyzer | 同じLuna | 整理方針の比較をモデル能力差から切り離す | 同上 |
| organizer | 同じLuna | B3とDの計算単価・モデルを一致 | 同上 |
| 補助Judge | Foundryの`claude-sonnet-5-5` | agentと異なるモデル系列 | Azure-hosted GA掲載確認。Marketplace契約・quotaは未確認 |
| reader感度分析 | `gpt-6-sol` | 同じ根拠でreader能力依存を調べる | 公式catalog掲載確認。deploymentは未確認 |
| embedding | local `multilingual-e5-small` | 日本語・英語を同一の軽量モデルで扱う | ローカルMITモデル。Foundry deploymentを前提にしない |
| embedding代替 | Foundry `text-embedding-3-small` | Python／CPU性能上の問題時に生成系と独立して置換可能 | 公式catalog掲載。実deploymentと適用価格は実行時確認 |

GPT-6のcatalog掲載とtool／structured output対応を確認した。Claude Sonnet 5.5はAzure-hosted GAとして掲載されている。[Microsoftモデル一覧](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure)、[Claudeモデル一覧](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models)

### 7.2 設定と選択規則

- Luna：reasoning effortは低い固定値を第一候補とし、devでschema適合とoracle正答率を確認。
- temperatureはAPIが受け付ける場合のみ固定。未対応parameterを無理に指定しない。
- model aliasではなく可能な限りversionを固定し、応答metadataも保存。
- analyzer／organizerのモデルを、本試験途中で強いものへ変更しない。
- Lunaのoracle-source正答率が90%未満なら、readerが弱すぎる可能性がある。主試験を開始せず、モデル・タスク難度・費用を再固定する。
- Sonnetが利用できなければ、Foundryの別Anthropicモデルをdevで校正。最終的に同系列Judgeしか使えない場合は、主指標の機械採点を維持し、補助評価を人手中心に変更する。

### 7.3 Embedding

`multilingual-e5-small`はMIT表記、384次元。`query:`／`passage:` prefixを守り、512-token上限を超える入力を無言で切り捨てない。[公式model card](https://huggingface.co/intfloat/multilingual-e5-small)

- chunk：embedding tokenizerで384 tokens、overlap 64。
- source timestamp、event ID、roleをmetadataとして保持。
- 日本語lexical検索は文字bigramと識別子tokenを併用し、devで固定。
- dense＋BM25をRRFで統合。
- rerankerは主実験には入れない。検索器の調整範囲を増やしすぎない。

生成LLMはFoundryに統一し、embeddingはbriefで許容されたローカル実行を優先する。

---

## 8. ローカル実行環境

### 8.1 現在のrepositoryで確認した前提

確認時点では：

- branchは`main`、未commitの作業ファイルがある。
- `.github/repository-initialization.yml`がない。
- `pyproject.toml`はPython **3.14系**を要求。
- uv workspaceの`members`は空。
- `Makefile`にDocker targetはあるが、対応する`context/`配下の構成は確認できない。

したがって、**完成済みのDS template環境がすぐ動く前提にはしない**。第1週に初期化・workspace member・CPU dev containerを整える工程を含める。本相談ではファイル変更・環境変更を行わない。

### 8.2 Dockerを既定にする

- ARM64 Linux CPU dev container内で`uv`、Python、テスト、API runnerを実行。
- containerへ6GB程度を割当、CPU workerは2〜4。
- embeddingもまずCPUで実行し、Metalやローカル生成LLMを必須にしない。
- Docker利用不可の場合、ホスト実行はrepository規則上の明示的な例外承認が必要。無断で切り替えない。
- Python 3.14で依存が解決しなければ、まずcloud embeddingで重い依存を外す。Python要件やdependency cooldownは無断変更しない。

### 8.3 メモリ見積

以下は**上限保証ではない見積**。

| 項目 | 想定 |
|---|---:|
| macOS・基本アプリ | 5〜6GB |
| Docker VM＋Python runner | 1〜1.5GB |
| local embeddingの重み・runtime・batch | 1〜2GB |
| 現在処理中の検索index・vectors | 0.2〜0.5GB |
| API request／response、集計buffer | 0.3〜0.8GB |
| 合計 | 約8〜11GB、余裕を見て12〜13GB以内 |

- embedding batchは8から開始。
- 全環境のvectorsを同時にRAMへ置かず、環境単位にロード。
- 384次元float32の100万vectorsでも生配列は約1.54GB。全体はmmap保存する。
- artifacts／APIログ用にSSD空き30GBを確保。
- 実験process群の使用量が8GBを超える、またはswapが継続増加する場合は並列度・batchを下げる。

### 8.4 実装品質の確認

必要なテストは以下に集中する。

- 時点・authority・supersedeの真理規則。
- future information／goldの漏洩防止。
- transcript role偽装、path traversal、read-only境界。
- interrupted organizerのatomic promotionと再実行。
- 引用切れ、依存先失効、古いvector indexの残存。
- 機械採点とusage集計の独立した期待値。

実装時はcontainer内で`make test`、`make fix`、`make all-lint`を実行する。テスト収集件数を確認し、API integration試験は通常テストと分ける。

---

## 9. コスト見積

### 9.1 単価の確度

USD／100万tokens、税・為替・契約割引を除く。

| モデル | 入力 | 出力 | 確認状況 |
|---|---:|---:|---|
| Luna short | 0.10 | 0.50 | Microsoftの2026-09-22公式公表値を確認 |
| Luna long | 0.20 | 0.75 | 同上 |
| Sol short | 2.00 | 10.00 | 同上 |
| Sonnet 5.5 | 2.00 | 10.00 | 現行Claude公式価格表を確認。FoundryのCCU換算がこのtoken価格を参照することも確認 |
| embedding代替 | 0.02 | ― | surveyの参考値。今回、対象契約の現行価格は独立確認していない |

Lunaの**11月10日時点の契約適用単価とshort／long閾値は未確定**。Gemini surveyの272Kという閾値は採用しない。価格ページの取得が不安定だったため、実行前にmeter／portalで確定する。

Claudeについては、単に直販価格をAzure価格へ流用したのではなく、MicrosoftのCCU説明がAnthropicの公表token単価を参照することを確認した。ただしprivate offer、税、為替を含む請求額は未確認。[Azure公式公表価格](https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/)、[Foundry CCU](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing)、[Claude価格表](https://platform.claude.com/docs/en/about-claude/pricing)

### 9.2 計算式

\[
C=\sum_{\mathrm{role}}
\frac{T_{in}P_{in}+T_{out}P_{out}}{10^6}
+C_{\mathrm{embedding}}+C_{\mathrm{retry}}
\]

- \(T_{out}\)には課金対象のreasoning tokensを含める。
- cache・Batch・Flex割引は初期見積に含めない。
- 本番集計はcached input等を別meterで計算する。
- 共通notesの物理的な生成費用は一度だけ計上するが、各方式の単独運用費用には同じ持分を割り当てる。

### 9.3 主実験

環境×反復＝160×3＝480。

| 処理 | 仮定 | 入力tokens | 出力tokens | Luna shortでの費用 |
|---|---|---:|---:|---:|
| 共通online notes | 480×64回、平均3K入力／0.4K出力 | 92.16M | 12.288M | $15.36 |
| B3・D maintenance | 960 world-runs。各64 analyzer、8 organizer | 1,105.92M | 92.16M | $156.67 |
| B0/B1/B2/B3/D reader | 5×7,680問。2 calls合計で平均12K入力／1K出力として保守的に計上 | 460.8M | 38.4M | $65.28 |
| 全文 ICL | 7,680問。平均98K入力／0.8K出力、1 call | 752.64M | 6.144M | $78.34 |
| **主実験合計** | | **2,411.52M** | **148.992M** | **$315.65** |

maintenanceの1 world-run当たり仮定：

\[
T_{in}=64\times12K+8\times48K=1.152M
\]

\[
T_{out}=64\times1K+8\times4K=96K
\]

ここで12K／48Kは、それぞれ**2 calls合計**の平均入力量。

### 9.4 全体予算

| 項目 | 見積 |
|---|---:|
| 主実験 | $316 |
| LongMemEval：150問×2反復×4群、memory構築込み | $72 |
| dev・採点校正 | $40 |
| ablation、Sol reader診断、closed-loop、安全性 | $75 |
| Judge約6,000 calls | $55 |
| cloud embedding代替 | $0〜5 |
| **中央見積** | **約$560** |
| 30%予備費込み | **約$730** |
| **停止上限** | **$900** |

Judgeはおおむね9M＋9.6M入力、1.08M＋0.72M出力を想定し、約55 USD。モデルごとのtokenizer差は実測で補正する。

全Luna入力がlong単価相当になるなどの保守的条件では、全体が**約950〜1,100 USD**へ上振れし得る。第1週に価格区分を確定し、900 USD以内に完走できない予測なら本試験を開始しない。探索項目は削減可能だが、主比較の標本数や条件を結果を見て削らない。

### 9.5 実行時間

主実験だけで約25万calls。補助実験を含め、入力約3B tokens規模を想定する。

\[
時間\gtrsim\max\left(
\frac{総入力tokens}{実効TPM},
\frac{call数\times平均latency}{並列度}
\right)
\]

入力律速の例：

- 500K実効TPM：約100時間。
- 200K実効TPM：約250時間。

全体として、API実験は**4〜11日程度の連続・再開可能な実行枠**を見込む。CPU embeddingは別途6〜20時間を仮置きし、pilotで測り直す。API実行と解析・レポート作業は並行できる。

### 9.6 償却と費用対効果

\[
Q_{\mathrm{break-even}}=
\frac{追加offline費用}
{比較方式のquery費用-Dreamingのquery費用}
\]

分母が0以下なら費用上の損益分岐点はない。

同じ8K予算のRaw-RAGとDreamingではquery費用がほぼ同じになり得る。その場合、Dreamingの追加maintenanceは**token節約では償却できない**。精度改善や業務上の失敗削減で正当化できるかを別に判断する。

---

## 10. 工程

### 第1週：11/10〜11/16 — 実行基盤・測定器・pilot

成果：

- repository初期化状態、CPU container、workspace memberを整備。
- 依存・モデル・データのLICENSE manifest。
- Foundry deployment、version固定、価格、quota、usage取得を確認。
- dev24環境、truth ledger、採点器。
- Raw-RAG、全文 ICL、oracle-source reader。
- 12セッション程度の短いDreaming動作確認。

**Go条件**

- oracle-source正答率90%以上。
- 機械採点と人手の一致率95%以上。
- 必要根拠の完全取得率がdevで85%以上。
- memory必要性、文脈圧、ドリフトが設計どおり存在する。
- 予算・quota・依存条件が成立。

Dreamingが既に勝つことはGo条件にしない。

### 第2週：11/17〜11/23 — 主比較完成・事前登録

成果：

- B3・Dの同一budget runner。
- atomic promotion、出典追跡、Git監査、resumption。
- 160 test環境を凍結し、質問とgoldを隔離。
- B3が弱い要約になっていないかをdevで点検。
- 分散・検出力・費用を再見積。
- 主仮説、全設定、停止条件を固定。

**Go条件**

- leak検査、安全性の基本テスト、品質checkが通過。
- 呼出回数と予算上限が一致。
- 予定規模の検出力が80%以上。
- 総費用予測900 USD以内。

### 第3週：11/24〜11/30 — 主実験・外部ベンチ

成果：

- 6群×160環境×3反復を実行。
- arm順序をblock内でランダム化し、時間帯・サービス変動を分散。
- LongMemEvalを並行実行。
- checkpoint、usage、欠測だけを監視する。

主比較の途中成績を理由にpromptやデータを修正しない。重大な実装不具合があればrunを無効化し、修正版を別revisionとして残す。

### 第4週：12/1〜12/7 — 診断・統計・レポート

成果：

- memory品質評価、reader診断、ablation。
- 人手監査。
- paired cluster CI、条件別分布、Pareto図、失敗事例。
- Notion向けレポートと再実行手順。

**最終判断**

- H1支持＋運用条件成立：限定利用を推奨。
- H1支持だがRAG／ICLに費用面で劣る：用途限定または不採用。
- 差が小さくCIも狭い：この条件では追加価値なし。
- CIが広い／readerで頭打ち：結論保留、原因を明示。

### 第5週：12/8〜12/14 — 予備枠

quota待ち、API障害、事前規則に沿う欠測再実行、監査修正に使用する。新しい主仮説や別の学習方式を追加する週にはしない。

---

## 11. リスクと対策

### 11.1 Survey間の不一致と採用判断

| 論点 | 判断 |
|---|---|
| LightMemのoffline更新は常に改善するか | **Codex surveyの慎重な解釈を採用。** 原論文Table 1に67.78→65.39等の悪化例がある。Claude surveyの全体改善をoffline更新単独の効果と解釈しない |
| CL-BenchのDB・問題数 | **Codex／Claude側を採用。** 公式READMEではSQLite、30問variantと40問schema-drift variantを区別。GeminiのPostgreSQL懸念や「30〜50問」の一括扱いは採らない |
| LoCoMoのライセンス | **Codex／Claude側を採用。** 公式LICENSEはCC BY-NC。GeminiのCC BY表記は誤り |
| Memento-Skills、ExpeL、AWM | 公式LICENSEはいずれもApache-2.0。GeminiのMIT表記を採用しない |
| GPT-6の272K価格閾値 | 今回の確認では未確定。Geminiの断定より、Codex／Claudeの「未確認」を採用 |
| Foundry Claudeの料金 | survey時点の未確認事項を更新。CCUと公表token単価の対応を一次資料で確認。ただし契約請求額は未確認 |
| M4上のSLEEP・QLoRAの可否 | 「完全に可能」「1Bでも必ずOOM」といった断定は採らない。構成と実測なしでは決められず、今回の工程から外す |

出典：[LightMem Table 1](https://arxiv.org/html/2510.18866v1)、[CL-Bench DB README](https://raw.githubusercontent.com/pgasawa/continual-learning-bench/main/src/tasks/database_exploration/README.md)、[Memento-Skills LICENSE](https://raw.githubusercontent.com/Memento-Teams/Memento-Skills/main/LICENSE)、[ExpeL LICENSE](https://raw.githubusercontent.com/LeapLabTHU/ExpeL/main/LICENSE)、[AWM LICENSE](https://raw.githubusercontent.com/zorazrw/agent-workflow-memory/main/LICENSE)

### 11.2 実験上のリスク

| リスク | 対策 |
|---|---|
| Dreaming向けに有利な合成データになる | 注入なし層、単一文書問題、全文 ICL、外部ベンチを置く。一般的優越性を主張しない |
| 160環境が実質同じtemplate | entity名だけでなく規則・依存構造・更新内容を変える。推論範囲はgenerator分布に限定 |
| 強い要約が弱く実装される | B3に原ログ、訂正、階層化、出典保持を許し、同じmaintenance予算を与える |
| 情報を捨てる機能の出番がない | 文脈圧・必要根拠量・history長を成績を見る前に検査 |
| readerの弱さをmemoryの失敗と誤認 | oracle-sourceとSolによる同一根拠比較 |
| gateがgoldを利用し、Dだけ正解を知る | goldはevaluation専用。promotionではsource存在・schema等のみ機械確認 |
| 正しい値を古い値で上書き | effective time、authority、supersedes、依存先を保持 |
| memoryが大きくなるだけ | active上限と全保存量を分け、超過・圧縮損失を記録 |
| repeated consolidationで劣化 | 変更なしのstoreへ再整理を繰り返す小規模テストを追加 |
| provider側のモデル更新 | versionと応答metadataを記録。更新を跨いだ結果を無条件にpoolしない |
| API費用・quotaの上振れ | pilotの実usageから再計算、再開可能runner、900 USD停止 |
| Python 3.14依存不整合 | 最小構成、cloud embedding代替。勝手なPython downgradeはしない |
| 安全性0件を過大評価 | 300件で0件でも「安全の証明」ではない。独立試行を仮定した片側95%上限は約1%と併記 |

Gitに残るcanaryは実在秘密ではない。実秘密やPIIを入力して検証する必要はない。

---

## 12. 想定されるレポート構成

briefに記載されたNotion「新規技術検証」の粒度に合わせ、次の章立てとする。実際のDB property名・ページtemplateは本相談で確認していないため、掲載時に対応づける。

### メタデータ

- 技術名：Dreaming／外部メモリのオフライン統合。
- 所属枠：DSD／FY26H2。
- 担当、検証期間、状態。
- repository revision、run ID、data revision、model version。
- 総費用、計算環境、利用条件。

### 本文

1. **結論・実務者向け要約**
   - 採用／限定採用／不採用／保留。
   - 効果量、CI、総費用、安全性の要点。
2. **背景と課題**
   - 先行社内2レポートの教訓と今回の変更点。
3. **技術概要と対象外**
   - 外部メモリDreamingとparametric SLEEPの区別。
4. **事前登録した仮説**
   - 主仮説、探索的仮説、成功条件。
5. **検証方法**
   - データ、比較群、公平性、予算、モデル、統計。
6. **主結果**
   - D−B3差、環境単位CI、反復分布。
7. **条件別結果**
   - clean／誤メモ、single／multi-document、drift前後。
8. **記憶内容の品質**
   - 修復、破壊、脱落、根拠、肥大化。
9. **検索とreaderの切り分け**
   - oracle-source、oracle-memory、強いreader。
10. **費用・遅延・償却**
    - 精度対総費用のPareto図、query数別の損益分岐。
11. **安全性と失敗事例**
    - 保存→検索→行動、gate前後、rollback。
12. **考察・限界・次の判断**
13. **再現手順・artifact・参考文献**

### 必ず示す図表

- 主比較のpaired差と95% CI。
- セッション数に対する正答率・stale率・memory量。
- 最終回答正誤×memory内容正誤の2×2表。
- 原根拠→memory→retrieval→readerの失敗内訳。
- 精度対総USD、query数別の償却曲線。
- 修復成功例と、正しい記憶を壊した例のrevision差分。

### 示せること／示せないこと

| 示せる見込み | 示せないこと |
|---|---|
| 定義した長期・ドリフト分布でのDreamingの追加効果 | あらゆるagent・業務における普遍的な改善 |
| 同一モデル・呼出予算下の通常要約との差 | Anthropic原報告の厳密再現 |
| 誤記憶修復と記憶破壊のトレードオフ | 自然環境での誤記憶発生率 |
| memory、retrieval、readerのどこに問題があるか | 各機序の完全な因果分解 |
| M4 16GB＋Foundryでの実際の費用・速度 | GPU学習やparametric SLEEPの有効性 |
| 固定ログreplayと小規模closed-loopでの挙動 | 数か月の完全自律運用、組織全体の共有メモリ |
| 定義した安全性suiteでの失敗率 | 未知攻撃に対する安全保証 |

**差が出なかった場合も、強い対照・十分な検出力・reader診断が揃えば、検証として有用な結論になる。**

---

## 13. 参考文献

### 対象技術・評価設計

1. Anthropic, *The components of agent memory*  
   https://www.anthropic.com/research/components-of-agent-memory

2. Anthropic, *Dreaming with a dynamic workflow*  
   https://platform.claude.com/cookbook/managed-agents-cma-dreaming-with-a-dynamic-workflow

3. Google Research, *The SLEEP framework* — 対象外方式との区別に使用  
   https://research.google/blog/the-sleep-framework-a-new-way-for-llms-to-retain-knowledge-in-continual-learning/

4. *LightMem: Lightweight and Efficient Memory-Augmented Generation* — offline更新の負例を確認  
   https://arxiv.org/html/2510.18866v1

5. LongMemEval — 公式実装  
   https://github.com/xiaowu0162/LongMemEval

6. LongMemEval — 公式cleanedデータ  
   https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned

7. CL-Bench — Database Exploration仕様  
   https://raw.githubusercontent.com/pgasawa/continual-learning-bench/main/src/tasks/database_exploration/README.md

### Foundry・価格

8. Microsoft, *Foundry Models sold by Azure*  
   https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure

9. Microsoft, *Claude models in Microsoft Foundry*  
   https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models

10. Microsoft, *GPT-6 Astra, Sol, and Luna*  
    https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/

11. Microsoft, *Claude Consumption Units billing*  
    https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing

12. Anthropic, *Model pricing*  
    https://platform.claude.com/docs/en/about-claude/pricing

13. Azure OpenAI pricing — 実行前の適用単価確認先  
    https://azure.microsoft.com/en-us/pricing/details/azure-openai/

### 実装・ライセンス

14. multilingual-e5-small  
    https://huggingface.co/intfloat/multilingual-e5-small

15. BM25S  
    https://github.com/xhluca/bm25s

16. LoCoMo LICENSE  
    https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt

17. Memento-Skills LICENSE  
    https://raw.githubusercontent.com/Memento-Teams/Memento-Skills/main/LICENSE

18. ExpeL LICENSE  
    https://raw.githubusercontent.com/LeapLabTHU/ExpeL/main/LICENSE

19. Agent Workflow Memory LICENSE  
    https://raw.githubusercontent.com/zorazrw/agent-workflow-memory/main/LICENSE

内部資料は、指定された`00-brief.md`および`survey-{codex,claude,gemini}.md`を使用した。先行社内レポートの原本は再監査しておらず、教訓はbriefに記載された範囲に基づく。