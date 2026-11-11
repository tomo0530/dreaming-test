# Dreaming 追試: 設計書

- 作成日: 2026-11-11（R0 レビュー反映済み）
- 位置づけ: 追試で何を・なぜ・どの条件で検証するかの正本。実行パラメータの値は `configs/experiments/followup/*.yaml` を正本とし、本書ではキー名で参照する。
- 関連文書: [10_implementation-plan.md](10_implementation-plan.md)（実装計画）、[20_preregistration.md](20_preregistration.md)（追試の事前登録。本番の前に凍結）、[../plan/40_results.md](../plan/40_results.md)（前回の結果。本追試の仮説の出どころ）、[../plan/00_design.md](../plan/00_design.md)（前回の設計。本書に書かない事項はこれを引き継ぐ）
- 前回の文書（`docs/plan/`）は変更しない。

## 1. 目的と問い

前回の検証（以下「前回」）は、採点のない SchemaDrift-SQL で、記憶を持つ群が記憶なし（A0）を下回った（A4 − A0 −6.1pt、A1 − A0 −5.0pt）。主比較 A4 − A1 は −1.1pt [−4.2, +2.2] で、優位を示せなかった。ただし、オンラインの `memory_write` と `search_transcripts` は 1 回も呼ばれず、A4 と A2 の本来の処置は働いていなかった。正式な結論は「条件の構築まで」である。

本追試は、前回を**仮説の出どころ**として扱う独立の研究である。Dreaming が効く条件と、記憶が害になる仕組みを切り分ける。

- **主な問い**: 回答の採点フィードバックが transcript にあれば、Dreaming（A4）は改善するか。改善するなら、記憶なし（A0）と Rolling Summary（A1）を上回るか。
- **副次の問い**:
  - フィードバックによる改善は、A4 と A1 で違うか（交互作用）。
  - 「記憶があると値の確認が浅くなる」は、読み手側の介入で変わるか（機構実験）。
  - 統合役が現在の DB を読み取り専用で照会できれば、方針の記述や成績は変わるか（A4+probe）。
  - drift のないストリームで、統合役はありもしない変更を記憶に書くか。
  - CL-Bench で、A4 − A1 の推定はどこまで絞れるか。

**根拠（前回と文献）**
- Anthropic の CL-Bench 実験は、各問の後に正解を伝えてから記憶を書かせている。arXiv 2609.11060（2026-09）も採点ありの条件で、軌跡だけの curator（70%）が no-memory（39%）を上回った。
- 前回の CL-Bench（誤答時に正解を返す）では記憶群が A0 を上回り、採点なしの SchemaDrift-SQL では下回った。
- 記憶ツールを任意にするとほぼ使われないことは、前回の結果と arXiv 2607.20972 が示している。そのため、本追試では harness で強制する。

## 2. 目標・非目標・アンチゴール

| 区分 | 内容 |
|---|---|
| 目標 | 固定順序の主仮説 3 つを事前登録し、ストリーム単位の paired bootstrap 95% CI で報告する |
| 目標 | 処置（フィードバック、メモ作成、検索結果の注入、確認指示、統合役の照会）が実際に働いたことを、本番と同じ条件の pilot で機械的に確かめてから本番を行う |
| 目標 | 追加費用は目標 $100 以内（枠の合計 $98、見積は約 $65）。累計の絶対上限 $200（既存の台帳の hard cap）を守る |
| 非目標 | 前回の H1 の再判定。前回の 20 ストリームの再利用。LongMemEval などの外部ベンチ。重み更新（SLEEP）。人手監査。モデルの変更 |
| アンチゴール | A4 が勝つまでプロンプトやタスクを調整すること。gate に成績（A4 の優位、A1 の失敗、oracle との差）を使うこと。gate 不合格のまま開始条件を格下げして進めること。対照（A1）だけに不利な処置や、実行できない作業を入れること。処置が働かなかった群を元の処置名で解釈すること。前回の CL-Bench と合算する run の既定の挙動を変えること |

## 3. 決定事項

ユーザーとの協議（2026-11-11、Q1〜Q13）の結論と、その後の判断。

| # | 事項 | 決定 | 決め方 |
|---|---|---|---|
| F1 | 主目的 | 効く条件と害の仕組みの切り分け。前回と同じ条件での反復はしない | ユーザー |
| F2 | 主因子 | 採点フィードバック {なし, あり} × {A0, A1, A4} の 6 条件。フィードバックは回答の確定後に transcript だけに書く | ユーザー |
| F3 | 記憶操作の強制 | A1・A4 は毎問の回答後にメモ作成を強制する（回答ターンの枠外）。A2 は検索結果を harness が注入する。旧 A1・A2・A4 とは別の処置として報告する | ユーザー |
| F4 | 機構実験 | 確認指示 {なし, あり} × {A0, A1, A4}（20 ストリーム）。統合役の照会つき A4+probe（20 ストリーム）。どちらも探索的 | ユーザー |
| F5 | 予算 | 追加分の目標 $100、累計の絶対上限 $200（既に $11.94 を使用済み） | ユーザー |
| F6 | 外部ベンチ | LongMemEval-S は使わない。CL-Bench を 15 反復追加し、前回の 3 反復と合わせて 18 反復にする | ユーザー |
| F7 | 位置づけ | 新しい事前登録を持つ独立の追試。新しい seed を使い、対照も同時に走らせる。gate 不合格時の改訂は 1 回だけで、だめなら本番を行わない | ユーザー |
| F8 | フィードバック形式 | CL-Bench と同じ「正誤＋誤答時は期待値」の 1 種類 | ユーザー |
| F9 | 主仮説 | H1 → H2 → H3 の固定順序（§9）。交互作用は副次指標 | ユーザー |
| F10 | 主実験の規模 | 新しい 40 ストリーム。A0・A1・A4 × フィードバック 2 条件、A2（注入型）× 2 条件、A5 × 1 条件 | ユーザー |
| F11 | 偽メモ | 注入しない。代わりに drift のないストリーム 10 本で、A1・A4 が書く「変更の記述」と記憶の正しさを見る | ユーザー |
| F12 | モデル | 前回と同じ（全役割 `gpt-6-luna`、agent `low`、統合役 `medium`）。予算が残れば agent を `medium` にした感度分析を 10 ストリームで行う | ユーザー |
| F13 | 文書と報告 | `docs/followup/` に 00〜40 を作る。結果は既存の Notion ページに追記する | ユーザー |
| F14 | 条件の表し方 | 条件は「run の設定 × 群」で表す。群の名前（A0〜A5）は増やさず、処置は run 単位の設定キーで切り替える。フィードバックの有無などで run を分け、同じストリームで同時に実行する | メインセッション（群名を足すと評価コード全体に波及するため） |
| F15 | A0 の扱い | A0 にはフィードバックの経路がない。それでも両方の run で A0 を実行し、その差を A/A 検査（ノイズの大きさの確認）として報告する | メインセッション |
| F16 | 主条件の予算の保護 | 主仮説に使う run（`fu_fb0`、`fu_fb1`）は phase `fu_main`、探索的な run は phase `fu_aux` に分ける。探索的な run が枠を使い切っても、主条件の run は止まらない | メインセッション（R0 レビューの指摘） |
| F17 | A2 とフィードバック | A2 は過去の transcript を検索するので、`fu_fb1` ではフィードバック行も検索結果としてオンラインで読む。これは「採点つきログの検索」という別の記憶方式として扱う。「フィードバックを agent に見せない」規則は、A0・A1・A4・A5 に適用する | メインセッション（R0 レビューの指摘） |

## 4. 処置の定義

すべての処置は、run の設定キーで有効にする。既定値は前回の挙動と一致し、前回の run の設定 digest と再開を壊さない。共通の統合プロンプト `_consolidation_core.md` は変更しない（前回の CL-Bench と合算する run の挙動を保つため）。

| 設定キー | 値 | 処置 |
|---|---|---|
| `feedback` | `none`（既定）/ `graded` | `graded` のとき、採点の直後に transcript へ `EVENT feedback:` 行を書く（§4.1） |
| `session_notes` | `false`（既定）/ `true` | `true` のとき、記憶を持つ群（A1・A4）は毎問の回答後にメモを 1 回書く（§4.2） |
| `a2_retrieval` | `tool`（既定）/ `auto` | `auto` のとき、A2 は検索ツールを持たない。代わりに、問題文で検索した上位の断片が問題の前に注入される（§4.3） |
| `agent_prompt_addendum` | `none`（既定）/ `verify_memory` | `verify_memory` のとき、agent の system prompt の末尾（記憶ブロックの後）に確認指示を足す（§4.4） |
| `consolidator_sql` | `false`（既定）/ `true` | `true` のとき、A4 の analyzer は終わったセッションの DB を読み取り専用で照会できる（§4.5） |
| `drift` | `true`（既定）/ `false` | `false` のストリームは migration を持たない（§5.2） |

6 つのキーはすべて SchemaDrift 専用である。CL-Bench の設定で既定値以外を指定したら、読み込み時に拒否する。

### 4.1 採点フィードバック

- 形式は CL-Bench のアダプタ（`clbench.py` の `CLBenchEnv.feedback`）に合わせる。
  - 正答: `Question {n}: CORRECT!`
  - 誤答（`stale`・`sql_error`・`wrong_result`）: `Question {n}: INCORRECT.` の次の行に `Correct answer:`、続けて現行エポックの期待値。
  - 未提出: `Question {n}: No answer was submitted within the limits — treated as incorrect.` の次の行に `The correct answer was:`、続けて期待値。
  - `{n}` はストリーム内の通し番号（1 始まり）である。
- 期待値は `expected_by_epoch[現行エポック]` の行を `_cell` と同じ書式で整え、` | ` で区切って 1 行 1 レコードに並べる。最大 20 行・2,000 文字までとし、切った場合は `[truncated]` を付ける。期待値は列名を持たないので、列名は付けない。
- フィードバック行を読めるのは次の 2 つで、どちらも意図した経路である。
  - transcript を読める統合役（A1 の review、A4 の analyzer）。前回の共通規則では、`EVENT feedback:` を証拠として扱う。
  - A2 の索引（F17）。
- A0・A1・A4・A5 のオンラインの入力（system prompt、問題文、メモのターン）には、フィードバックも期待値も入らない。各問は新しい会話で始まり、transcript は agent の入力にならない。
- 前回の設計（transcript に採点を書かない）からの意図的な変更である。`graded` の run に限る。

### 4.2 回答後のメモ作成（A1・A4）

- 回答ループが終わった後（提出・ターン上限・クエリ上限のどの終わり方でも）、同じ会話に 1 ターンだけ続ける。
  - 直前の assistant 応答に、出力のない function call が残っている場合がある。提出の呼び出しには `Submitted.`、それ以外の呼び出しには `Not executed: the question is closed.` を出力として補う。Responses API は出力のない call を拒否するためである。
  - 続けて `note_turn.md`（固定の英文。書き込み先のパス `notes/s{SS}-{qid}.md` を埋め込む）を user として渡す。`tool_choice` で `memory_write` を 1 回だけ強制する。
  - tool のスキーマは、回答ループと同じもの（通常の tool と提出ツール）を同じ順で渡す（prefix キャッシュを効かせるため）。
  - 送る前に、入力 token が `budgets.max_input_tokens_per_call` 以下であることを確かめる。超えるならメモのターンを省き、`note_written=False` にする。
  - 回答ターンとクエリの枠には数えない。`max_completion_tokens` は 1,024 とする。結果行の `tool_calls`・`tool_errors`・`turns` にも含めない。
- **書き込み先と記録**:
  - パスは問題ごとに固定し、前の問題のメモを上書きしない。
  - そのファイルが空でない内容で存在するときだけ、結果行の `note_written` を真にする。
  - transcript には `TOOL CALL memory_write` として内容が残る。
- **`note_turn.md` の要件**:
  - この問題で確認した DB の事実（表・列の意味、コード値、単位、絞り込み条件、変化に気づいた点）を書かせる。
  - 照会結果で確認したことと、確認していないことを区別させる。
  - 問題の正誤には触れない（agent は正誤を知らない）。
- **群ごとの扱い**:
  - A1 は前回 `notes/` に書けなかった。`session_notes` が有効なときだけ、書き込み先を `notes/` に限って許可する。
  - **統合での扱い**: 共通断片 `notes_handling.md` は、次の規則だけを持つ。この断片は A1 の review と A4 の analyzer の両方に足す。
    - notes は、エージェントが書いた未検証の主張である。各記述を、transcript の照会結果とフィードバックで確かめる。
    - 証拠で決着した記述は、検証のタグを付けて取り込む。証拠と矛盾する記述は直す。
    - どの証拠でも決着しない記述は `(unverified)` を付けて残し、検証済みとして扱わない（A4 の analyzer と organizer の既存の扱いと同じ。R1 レビューで A1 もこれに揃えた）。
  - **統合先と、処理済みメモの退役**:
    - A1: `rolling_review_notes.md`。review が上の規則で（未決着の記述は `(unverified)` を付けて）`summary.md` に取り込み、処理したメモの内容を 1 行 `MERGED INTO summary.md` に置き換える。
    - A4: 前回の organizer.md のとおり。organizer が wiki に統合し、`MOVED TO [[slug]]` に置き換える。
    - どちらも既存の `write_file` だけで実行できる。
  - 注入層は前回どおりで、A1 は `injected.md` だけを注入する。
- 費用は component `online` に計上する。
- フィードバック行は、メモのターンの後に書く。したがって、メモの時点で agent が正誤を知ることはない。
- 予算の超過（`BudgetExceeded`）は、前回と同じく呼び出し元に伝え、ジョブを止める。メモのターンで捕捉するのは、コンテンツフィルタの拒否だけとする。

### 4.3 A2 の検索結果の自動注入

- 各問の開始時に、harness が問題文で `HybridIndex.search` を呼ぶ（`k=8`）。
  - 返った断片を、見出し `Relevant excerpts from past sessions:` の下に、`budgets.online.memory_read_tokens`（6,000 token）以内で問題文の前に置く。
  - 索引は前回どおり、終わったセッションの transcript だけを持つ。
- 注入した断片は transcript に書かない（transcript には問題文だけを書く）。そのため、注入結果が A2 の索引に再び入ることはない。
- 結果行には `retrieved_tokens` を記録する。

### 4.4 確認指示（機構実験）

agent の system prompt の末尾、つまり記憶ブロックの後に、次の英文（`agent_verify_addendum.md`）を足す。全群に同じ文を使う。A0 には記憶がないが、同じ指示を受ける。

> Treat any memory shown above as hypotheses from past sessions, not as facts: the database may have changed since they were written. Before you submit, query the current contents of every lookup or code table and every flag column your answer depends on, without filtering by remembered code sets.

### 4.5 統合役の照会（A4+probe）

- A4 の analyzer フェーズに `query` ツールを足す。
  - 照会先は、終わったばかりのセッションのエポックの DB（`env.run_query(session, sql)`）。次のエポックの DB は見せない。
  - 1 回の統合で最大 10 回まで照会できる。結果は前回と同じく、50 行・5 秒で打ち切る。
- analyzer のプロンプトに断片 `consolidator_sql_addendum.md` を足す。
  - 内容: `query` ツールの説明。方針や除外規則を書く前に、現在の値を照会で確かめること。証拠に自分の照会結果を含めてよいこと。
  - 共通プロンプトは変えず、この断片だけで tool の数と証拠の定義を上書きする。
- organizer と A1 には足さない。そのため、A1 との対称性は崩れる。この群は A4 同士の比較にだけ使う。

## 5. データ

### 5.1 ストリーム

| 用途 | ストリーム | seed | drift |
|---|---|---|---|
| dev（配管とプロンプトの確認） | `fu-dev-01`〜`02`、`fu-dev-null-01` | 4201〜4202、4211 | あり / なし |
| pilot（gate） | `fu-pilot-01`〜`02`、`fu-pilot-null-01` | 4301〜4302、4311 | あり / なし |
| pilot 改訂（1 回のみ） | `fu-pilot-rev-01`〜`02`、`fu-pilot-rev-null-01` | 4401〜4402、4411 | あり / なし |
| 本番 | `fu-01`〜`fu-40` | 4001〜4040 | あり |
| 本番（drift なし） | `fu-null-01`〜`fu-null-10` | 4101〜4110 | なし |

- 前回の seed（1001〜3020）とは重ならない。単体テストで使う seed は 4501 以上とし、これとも重ならないようにする。
- 生成器・採点器・問題テンプレートは前回と同じもの（12 セッション × 6 問）を使う。
- 本番ストリームは gate の前に生成と検証を済ませる（API 費用はかからない）。`stream.json` のハッシュは事前登録で固定する。dev と pilot で見た seed は本番に使わない。

### 5.2 drift のないストリーム

- 生成器に `drift` 引数を足す。`false` のときは次のとおりにする。
  - 乱数の消費順を変えない。drift の種類も抽出したうえで、変更時期を空にする。そのため、同じ seed なら drift ありのストリームとエポック 1 の名前・コードが一致する。
  - migration を作らない。
  - drift 依存の問題を作らない。
- データの追加（エポックごとの新しい行）は残る。スキーマと規則は全エポックで変わらない。
- 検証（`validate_stream`）は、drift のないストリームでは migration と drift 依存の検査を外す。他の検査（gold の結果と期待値の一致、空の期待値がないことなど）は同じにする。
- `Stream` に `drift: bool` を持たせる。
  - 既定値（真）のときは `stream.json` に書き出さない。そのため、既存ストリームのハッシュは変わらない。
  - run の設定の `drift` と一致しないストリームは、実行の前に拒否する。

## 6. 実行の構成

本番は次の run を**同時に**開始する（同じ台帳）。

| run（設定ファイル） | phase | ストリーム | 群 | 設定 |
|---|---|---|---|---|
| `fu_fb0` | `fu_main` | `fu-01`〜`40` | A0, A1, A2, A4, A5 | `feedback: none`、`session_notes: true`、`a2_retrieval: auto` |
| `fu_fb1` | `fu_main` | `fu-01`〜`40` | A0, A1, A2, A4 | `feedback: graded`、他は `fu_fb0` と同じ |
| `fu_verify` | `fu_aux` | `fu-01`〜`20` | A0, A1, A4 | `fu_fb0` ＋ `agent_prompt_addendum: verify_memory` |
| `fu_probe` | `fu_aux` | `fu-01`〜`20` | A4 | `fu_fb0` ＋ `consolidator_sql: true` |
| `fu_null` | `fu_aux` | `fu-null-01`〜`10` | A1, A4 | `fu_fb0` と同じ（`drift: false`） |
| `fu_clbench` | `fu_clbench` | `clbench-r3`〜`r17` | A0, A1, A4 | 前回の `clbench.yaml` と同じ（新しいスイッチは使わない。前回の 3 反復と合算するため） |
| `fu_sens`（任意。§8 の条件を満たしたときだけ） | `fu_aux` | `fu-01`〜`10` | A0, A1, A4 | `fu_fb0` ＋ `agent_reasoning_effort: medium` |

- 機構実験と A4+probe の比較相手は、`fu_fb0` の同じストリームの群である。どちらも同時に走るので、対照は同時に実行したものになる。
- A5 はフィードバックの影響を受けない（記憶は gold から機械生成する）。そのため `fu_fb0` だけで実行する。

## 7. Gate（本番の開始条件）

gate は**処置が働いたか**だけを確かめる。成績（正答率、群間の差、oracle との差）はどの条件にも使わない。

- **pilot の構成**: 本番と同じ設定の run を、pilot ストリームで走らせる。
  - `fu_pilot_fb0`、`fu_pilot_fb1`、`fu_pilot_verify`、`fu_pilot_probe`: ストリーム `fu-pilot-01`〜`02`。群と設定は、それぞれ本番の `fu_fb0`・`fu_fb1`・`fu_verify`・`fu_probe` と同じ。
  - `fu_pilot_null`: ストリーム `fu-pilot-null-01`、群 A1・A4。
  - phase はすべて `fu_pilot` である。

| # | 必須条件 | 判定に使う記録 |
|---|---|---|
| G0 | pilot の全ジョブが完走している | `status.json` |
| G1 | pilot と本番の全ストリーム（drift のないストリームを含む）が検証を通る | `validate_stream` |
| G2 | `fu_pilot_fb1` で、採点した全問について transcript に `EVENT feedback:` が 1 行ずつある。`fu_pilot_fb0` には 1 行もない | transcript |
| G3 | A1・A4 のそれぞれ（全 pilot run の合計）で、次の 2 つを満たす。(a) `note_written` が全問の 90% 以上。(b) `note_written` が 1 件以上あるセッションの統合の 90% 以上で、統合役が `notes/` 配下の退役していないファイルを `read_file` で 1 回以上読み、本文を 1 行以上取得している | 結果行、`ConsolidationRecord.notes_files_read` |
| G4 | A2 で、セッション 2 以降の問題の 95% 以上が `retrieved_tokens > 0` | 結果行 |
| G5 | すべての pilot run の各群で、`no_submission` 率が 10% 以下 | 結果行 |
| G6 | 本番の見込み費用が、phase ごとの枠以内である。見込みは、pilot の条件別実測単価 × 本番のジョブ数 × 1.3（キャッシュが効かない場合、再試行、未精算の予約を見込んだ係数）で出す。対象は `fu_main` と `fu_aux`（`fu_sens` を除く） | 台帳 |
| G7 | `fu_pilot_probe` の A4 で、analyzer の 50% 以上が照会を 1 回以上実行している（エラーや上限超過で実行されなかったものは数えない） | `ConsolidationRecord.sql_queries` |

- **不合格時**: phase `fu_pilot_rev` の枠で 1 回だけ直し、新しい seed（§5.1）で確かめる。直してよいのは、プロンプトの文面（`note_turn.md`、注入の見出し、統合役の断片）と配管だけで、閾値は変えない。それでも不合格なら本番を行わず、「処置の構築まで」としてレポートを閉じる。
- **dev の扱い**: dev では、全スイッチを有効にした 1 つの run で配管を確かめる。群別の正答率は見ない。見てよいのは機械的な指標（tool 呼び出しの成功率、G2〜G5・G7 の値、予算）だけとする。
- **事前登録の凍結**: gate の通過後、本番の最初の API 呼び出しより前に、事前登録の「凍結時に埋める欄」を埋めて commit する。

## 8. 予算

| phase | 枠 | 見積 |
|---|---|---|
| `fu_dev` | $3 | 約 $2 |
| `fu_pilot` | $6 | 約 $3.5 |
| `fu_pilot_rev`（不合格時のみ） | $6 | 約 $3.5 |
| `fu_main` | $52 | 約 $38（G6 の係数 1.3 を掛けて約 $49） |
| `fu_aux` | $25 | 必須 run 約 $15、任意の `fu_sens` 約 $5 |
| `fu_clbench` | $6 | 約 $4 |
| 合計 | $98（目標 $100 以内） | 約 $62〜66（任意の `fu_sens` を含めると約 $67〜71） |

- **見積の根拠**:
  - 前回の 1 ストリーム 1 群の実測は、A0 $0.038、A1 $0.128、A2 $0.055、A4 $0.110、A5 $0.024 だった。
  - メモのターンは 1 問 1 呼び出しで、prefix はキャッシュされる。統合の入力増も含め、A1・A4 に 1 ストリームあたり約 $0.035 を足した。
  - A2 の注入（1 問あたり最大 6,000 token）で、1 ストリームあたり約 $0.05 を足した。
  - CL-Bench は 1 反復 3 群で約 $0.25 である。
- **台帳**: 既存の台帳（`outputs/cost_ledger.sqlite`）をそのまま使う。全 phase の累計に対する hard cap $200（安全余裕 $2）は、前回から有効である。
- **`fu_sens` の実行条件**: `fu_aux` の必須 run の完了後、`fu_aux` の残りが $8 以上あるときだけ実行する。

## 9. 評価指標と統計

詳細と凍結値は事前登録（[20_preregistration.md](20_preregistration.md)）に置く。ここでは構成だけを示す。

- **主指標**: SchemaDrift-SQL の実行一致率（全 72 問）。採点は前回と同じ。
- **条件**: 条件は (run, 群) で表す。例: `fb1/A4` は `fu_fb1` の A4。
- **解析の単位**: ストリームを単位とする paired bootstrap（10,000 回、パーセンタイル法 95% CI、seed 20261111）で比べる。
- **主仮説**: 次の固定順序で判定する。前の仮説が「支持」のときだけ、次を確証的に判定する。
  - H1: `fb1/A4` − `fb0/A4` > 0
  - H2: `fb1/A4` − `fb1/A0` > 0
  - H3: `fb1/A4` − `fb1/A1` > 0
  - 40 ストリームすべてで 6 条件が完走した場合だけ確証的に判定し、1 本でも欠けたらすべて探索的に報告する。
- **副次指標**: 交互作用（`fb1/A4` − `fb0/A4`）−（`fb1/A1` − `fb0/A1`）、A/A 検査 `fb1/A0` − `fb0/A0`。
- **探索的指標**:
  - A2・A5 との差
  - drift 種別（特に soft delete）と、drift 依存かどうかによる層別
  - 機構実験の交互作用と、行動指標（1 問あたりのクエリ数、参照表の無条件照会率、スキーマ確認率）
  - A4+probe と A4 の差
  - drift のないストリームでの memory probe の正答率と、変更の記述の数
  - CL-Bench 18 反復の A4 − A1、A4 − A0
  - 1 正答あたりの費用
- 行動指標は探索的な記述であり、因果的な媒介の証明には使わない。
- 多重比較の補正は、固定順序の主仮説以外では行わず、「探索的」と明記する。

## 10. 実装方針

- **既存の流儀に合わせる**:
  - 群の違いは `Arm`、タスクの違いは `TaskEnv` に閉じ込める。
  - 処置の有無は `ExperimentConfig` の設定キーで切り替え、ランナーは群を知らない。
  - LLM 呼び出しはすべて `MeteredClient` を通す。
- **設定の互換**:
  - 新しい設定キーは、既定値のとき設定 digest から除外する（`false_note_timing` と同じ扱い）。
  - 旧形式の digest との照合は、新しいキーがすべて既定値のときだけ認める。
  - 既存 7 run の manifest は、すべて `matches_config` で一致し続けなければならない。
- **解析**:
  - 複数の run を「ラベル → run ディレクトリ」の対応で読み込む。transcript とスナップショットは既存の `RunLayout` で読む。
  - 条件ごとのストリーム別スコアを突き合わせ、既存の `paired_bootstrap` と `per_stream_score` を再利用する。
  - 依存は追加しない（numpy のみ）。
- **行動指標と記述の数**: transcript と記憶スナップショットからコードで数える（judge は使わない）。定義は事前登録で固定する。
- **安全**: 前回と同じ。`.env` とエンドポイントのホスト名を成果物・ログ・commit に出さない。

## 11. リスクと対策

| リスク | 対策 |
|---|---|
| メモ作成の強制で、notes が雑な記述で埋まる | `note_turn.md` で、照会結果に基づく事実と未確認事項を分けさせる。品質の問題が統合役の検証で吸収されるかを含めて、結果として報告する |
| フィードバックの期待値が長く、統合の入力を圧迫する | 20 行・2,000 文字で切る。統合の入力上限は前回と同じにする |
| A2 の注入で無関係な断片が入り、成績が下がる | 結果として報告する（探索的な群） |
| 確認指示が A0 の成績も変える | 交互作用（記憶の有無による改善量の差）で評価し、主効果は解釈しない |
| 未完走のストリームが処置に依存して偏る | 主条件を別 phase で保護する（F16）。除外の理由と件数を条件別に報告し、未完走ストリームの完了済みセッションによる感度分析を行う（事前登録 §5） |
| 同時に走らせることによる API 側の揺らぎ | 同時に開始し、A/A 検査（A0 の差）で大きさを確かめる |
| 同時に走らせる run が多く、レート制限に当たる | 同時に始める run（本番の 5 run と `fu_clbench`）の `concurrency` を合計 24 に抑える（`fu_fb0` 7、`fu_fb1` 7、`fu_verify` 2、`fu_probe` 3、`fu_null` 3、`fu_clbench` 2）。429 は既存の再試行で処理する |
| 前回と同じ失敗（gate 不合格のまま進む） | §7 の不合格時の分岐を守り、事前登録の凍結 commit を本番の前に置く |

## 12. 実装者に委ねる点

- `note_turn.md`、注入の見出し、`notes_handling.md`、`rolling_review_notes.md`、`consolidator_sql_addendum.md` の文面。§4 の要件を満たし、dev ストリームでだけ調整する。§4.4 の確認指示の文面は固定とする。
- 解析出力 JSON のキー名の細部と、図の体裁。
