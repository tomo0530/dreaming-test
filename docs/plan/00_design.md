# Dreaming 有効性検証: 設計書

- 作成日: 2026-11-10
- 位置づけ: 何を・なぜ・どの条件で検証するかの正本。実行パラメータの値は `configs/*.yaml` を正本とし、本書ではキー名で参照する。
- 関連文書: [10_implementation-plan.md](10_implementation-plan.md)（実装計画）、[20_preregistration.md](20_preregistration.md)（本実験前に凍結する事前登録）、[../research/00-brief.md](../research/00-brief.md)（背景）、[../research/plan-comparison.md](../research/plan-comparison.md)（3 プランの比較）
- 分割規則: 本書が 500 行を超えたら `design/` 配下へ章単位で分割する。

## 1. 目的と問い

Anthropic「Components of agent memory」の **Dreaming**（セッション間にオフラインで transcript を ground truth として再生し、メモを検証・訂正・統合する処理）が、**同じモデル・同じ予算の強い対照**に対して、ドリフトする逐次タスクで実際に効くかを簡易検証する。成果物は社内 Notion「新規技術検証」DB へのレポート（過去 2 件と同じ粒度）。

- 主な問い: transcript で裏付けを取る Dreaming は、transcript を読める Rolling Summary より、ドリフト後も正しく答えられるか。
- 副次の問い: 裏付け工程そのものの寄与（A4 vs A3）、生ログ検索で足りるか（A4 vs A2）、正しい記憶を渡しても読み手が誤る率（A5）、注入した誤メモの訂正と残存。

## 2. 目標・非目標・アンチゴール

| 区分 | 内容 |
|---|---|
| 目標 | 主仮説 1 つを事前登録し、ストリーム単位の paired bootstrap 95% CI で正直に報告する。総 API 費用を通常 $157〜167（gate 改訂を含めても最大 $189）・絶対上限 $200 に収める |
| 目標 | cookbook 準拠の Dreaming（analyzer → organizer、5 つの file tool による tool-calling loop）を OpenAI 互換モデルで Python 再実装する |
| 非目標 | 有意差の保証（検出力不足を許容し明記する）。記事の数値の再現。SLEEP 型（重み更新）。LongMemEval などの会話記憶。人手監査。Mem0 等の外部 OSS メモリとの比較 |
| アンチゴール | A4 が勝つまでタスクやプロンプトを調整すること（結果の選別）。対照（A1）を弱く作ること。セッションを短縮して文脈圧を消すこと。gold や将来の問題を記憶側に漏らすこと |

## 3. 決定事項（ユーザー決定と協議の帰結）

| # | 事項 | 決定 | 決め方 |
|---|---|---|---|
| D1 | 主対照 | A1 Rolling Summary（transcript を読める、予算一致） | ユーザー |
| D2 | 予算 | ユーザー指定の $100〜200 の範囲で、絶対上限 $200。呼び出し前の費用予約方式（配分は §8.2） | ユーザー |
| D3 | 比較群 | A0〜A5 の 6 群（§4） | ユーザー |
| D4 | データ | 主: 自作 SchemaDrift-SQL。副: CL-Bench database task（社内研究目的に限る） | ユーザー |
| D5 | 誤メモ注入 | 1 ストリーム 2 件、他の問題と独立した leaf rule に限定。全問で主指標、注入影響問題と非影響問題で層別報告 | Gemini 裁定 |
| D6 | 規模 | 12 セッションを固定し、1 セッション 6 問（単価が高い場合のみ 5 問）。ストリーム数は pilot 実測単価で決定（§8.2） | Gemini 裁定 |
| D7 | Pilot gate | 必須 7 条件・助言 4 条件（§9）。A4 の成績は必須条件に使わない | Gemini 裁定＋レビュー反映 |
| D8 | モデル | 全役割 `gpt-6-luna`。`reasoning_effort` は agent=low、consolidator（A1/A3/A4 共通）=medium。judge なし | 協議一致＋Gemini 裁定 |
| D9 | 埋め込み | `text-embedding-3-large`（デプロイ済み）。A2 は SQLite FTS5 BM25 と RRF で併用 | ユーザー |
| D10 | 感度分析 | `gpt-6.1-sol` による感度分析は行わない（cookbook 型 loop では $15 枠で 1 ストリームしか回らない） | ユーザー |
| D11 | 実行環境 | ホストで `uv run`（Docker なし）。Python 3.14 | ユーザー |
| D12 | 依存 | runtime: `openai`, `pydantic`, `pyyaml`, `numpy`, `tenacity`, `tiktoken`, `matplotlib`。dev: 既存＋`types-PyYAML` | ユーザー＋Gemini 裁定 |
| D13 | ライセンス | copyleft でない寛容ライセンス（MIT / Apache-2.0 / BSD / PSF / Zlib / CC0 / HPND 等）を許可。GPL / LGPL / MPL 等の copyleft は禁止。ただし `certifi`・`pathspec`（MPL-2.0、ファイル単位の弱い copyleft）は未改変の推移的依存としてのみ許容する | ユーザー（MPL の例外はメインセッションの判断） |
| D14 | 構成 | 単一ルートプロジェクト（uv workspace なし）。コードは `project_module/src/project_module/` | Gemini 裁定 |
| D15 | 言語 | 実験（問題文・プロンプト・メモリ）は英語、レポートは日本語 | ユーザー |
| D16 | 進め方 | 節目ごとの feature branch → `main` への PR を直列に積む | ユーザー |

## 4. 比較群

全群で agent モデル、`reasoning_effort`、1 問あたりのオンライン予算（`budgets.online`）を揃える。記憶を持つ群は、回答時に読める記憶量（注入層＋追加読込）も揃える。

| 群 | 記憶の作り方（オフライン） | 回答時に使える記憶（オンライン） | 誤メモ注入 |
|---|---|---|---|
| A0 Stateless | なし | なし | なし |
| A1 Rolling Summary（主対照） | 2 フェーズ。review（transcript で `summary.md` を検証・訂正・追記）→ rewrite（`summary.md` を圧縮し `injected.md` を書き直す） | `injected.md` を注入、`memory_read` / `memory_search`（読み取りのみ） | `summary.md` に追記 |
| A2 Raw-RAG | なし（transcript を索引に追加するだけ） | `search_transcripts`（埋め込み＋BM25、RRF） | なし |
| A3 Organize-only Dreaming | organizer のみ。agent が書いた notes を整理・統合するが transcript は読めない | `injected.md` を注入、`memory_read` / `memory_search`、`memory_write` | `notes/` に追記 |
| A4 Verified Dreaming（処置群） | analyzer（transcript で verify → enrich → organize）→ organizer | A3 と同じ | `notes/` に追記 |
| A5 Oracle（診断） | gold fact ledger から現時点で有効な規則を機械生成（`fact_id` 順。注入層に入りきらない分は `facts.md`） | `injected.md` を注入、`memory_read` / `memory_search` | なし |

- A1 と A4 のオフライン処理は、同じ 2 フェーズ構成（transcript を読める 10 ターン＋読めない 12 ターン、別々の会話）・同じ累計入力上限（`budgets.consolidation.max_input_tokens_per_run`）・同じ出力上限・同じ `reasoning_effort` で動かし、実消費を併記する（R1 レビューで、1 本の会話に 22 ターンを詰めると A1 だけが先に入力上限に当たる点が指摘されたため、実装前の段階で A4 と同じ形にそろえた）。両者のプロンプトには共通の規則断片（transcript と tool 結果の照合、正確な識別子・単位・失敗した SQL の記録など）を入れ、A1 の要約プロンプトだけが手薄にならないようにする。
- 主比較 A4 − A1 は「オンラインで notes を書き、オフラインで transcript により裏付けて統合する記憶システム」と「オフラインで transcript から 1 本の要約を書き直す記憶システム」の比較であり、オンラインで notes を書ける（`memory_write`、`notes/` 配下のみ）のは A3 / A4 だけである。したがって Δ にはセッション内のメモ書きの効果も含まれる。この主張範囲を事前登録に書く。
- A3 と A4 の差は transcript へのアクセスを含む検証工程の有無。この比較は「検証プロンプトだけの効果」ではなく「原ログへのアクセスを含む検証工程の効果」と記述する。
- 注入層は全群でコードにより `budgets.injected_tokens` に切り詰める。追加の読み出し（記憶ツール・A2 の検索）も 1 問あたり累計 `budgets.online.memory_read_tokens` までとする。
- セッション k の処理順は「オンライン → 誤メモ注入（k = 3, 7）→ オフライン処理 → probe（k = 4, 8, 12）」。記憶群のオフライン処理はセッション 1〜11 の後に 1 回ずつ（1 ストリーム 11 回）実行し、最終セッション 12 の後は実行しない。A2 の索引追加は全セッションの後に行う。

## 5. 主データ: SchemaDrift-SQL

### 5.1 1 ストリームの構成

| 要素 | 仕様 |
|---|---|
| DB | 架空 EC 事業の SQLite。6 テーブル程度（例: customers, products, orders, order_items, order_status, shipments）。seed 決定的に生成し、各テーブル数百〜数千行 |
| 潜在構造（学習対象） | 列名の難読化、単位の混在（cents / dollars）、時刻形式の混在、主キーと外部キーの命名不一致、ステータスコードの意味（lookup テーブル経由）。seed ごとに変える |
| エポック | 3 エポック。epoch 1 = セッション 1〜4、epoch 2 = 5〜8、epoch 3 = 9〜12。migration はセッション 4・8 の後 |
| migration | 各 migration に **DDL で見える変更**（列名変更、テーブル分割など）と **DDL で見えない意味的変更**（同名列の単位変更、ステータスコードの再割当、旧テーブルが更新停止して stale になる、論理削除フラグの導入）を必ず両方含める |
| 問題 | 1 セッション 6 問、計 72 問。テンプレート（集計・結合・期間・ランキング・存在確認・複数規則の統合）から生成。各問題は gold SQL と、生成元レコードから独立計算した期待値の両方を持つ |
| ドリフト依存問題 | migration 直後のエポック（セッション 5〜8、9〜12）の問題の過半は、変更された要素に依存する（旧規則で書くと誤答になる） |
| 統合問題 | 各セッション 1 問以上、2 つ以上の潜在規則（単位＋ステータス意味など）を同時に使う問題 |
| gold fact ledger | 潜在規則を原子的事実として JSON 化（`fact_id`, `kind`, `subject`, `value`, `valid_from_session`, `valid_until_session`）。A5、memory probe、誤メモ注入の基礎。agent・consolidator からは隔離する |
| 誤メモ注入 | 2 件。セッション 3 と 7 の終了直後（その回のオフライン処理の前）に、他の問題と独立した **leaf rule**（単一テーブルのステータスコードやフィルタ条件）の値を偽った 1 件（150 tokens 以下、平叙文のみ。残存判定用の固有の一文 `marker` と、偽の主張を検出する文字列の組 `claim_patterns` を持つ）を、群ごとの記憶（§4）に agent の文体で追記する。注入対象の fact は次の migration で変わらないものに限り、Q_inj はドリフト依存問題にしない。注入対象の leaf rule を参照する問題（Q_inj）はセッション 4〜6、8〜10 に配置し、注入直前のセッションにも当該 leaf rule を照会する問題を 1 問置く（transcript に反証が残りやすくする） |
| memory probe | セッション 4・8・12 の処理の最後（セッション 4・8 はオフライン処理の後）に、有効な fact から 8 問の短答問題を決定的に抽出し（注入対象 fact が有効なら必ず含める）、DB にアクセスさせず記憶だけで答えさせる（A1〜A5）。probe は transcript を書かず、記憶への書き込みもさせない。正規化一致で機械採点 |

### 5.2 採点

- agent は `submit_sql` で最終 SQL を提出する。採点器はそれを現在エポックの DB で実行し、gold の結果集合と比較する（多重集合として比較。問題が順序を要求する場合のみ順序も比較。数値は相対誤差 1e-6、文字列は前後空白除去と大文字小文字の正規化、NULL は NULL とだけ一致）。
- **stale 判定**: 各問題は、当該エポックと過去エポックそれぞれの規則で同じ問いに答えた期待値（生成元レコードから独立計算）を持つ。判定は (1) 現行の期待値と一致 → `correct`、(2) エポック 2 以降の問題で、提出 SQL を直前エポックの DB で実行した結果が直前エポックの期待値と一致し、両者の期待値が異なり、直前の期待値が空でない → `stale`、または現行 DB で `no such column` / `no such table` になり直前エポックの DB では実行できる → `stale`、(3) その他の実行エラー → `sql_error`、(4) それ以外 → `wrong_result`、の順に行う。エポック 1 の問題は `stale` にならない。採点は切り詰めずに全結果で比較する（行数上限 100,000、タイムアウト 10 秒）。
- 誤答の機械分類: `stale` / `sql_error`（SQL のタイムアウトを含む）/ `wrong_result` / `no_submission`（ターン上限・クエリ上限・入力上限）。台帳の予算到達はストリーム単位の除外として扱う（事前登録 §3）。
- 生成器の健全性: `validate_stream` で、全エポックの gold SQL の結果と独立計算の期待値の一致、ドリフト依存問題で旧規則と新規則の結果が異なること、空の期待値がないこと、migration に DDL で見える変更と見えない変更の両方があること、注入の配置条件を検査する。テスト・ストリーム生成（不合格なら書き出さない）・gate (1) で同じ関数を使う。
- agent が実行できる SQL は読み取りのみ（`ATTACH` と書き込み系は拒否）、探索時は 50 行・5 秒で打ち切る。1 問あたりのクエリは `budgets.online.max_queries` 回まで（超過分は実行せずエラー文字列を返す）。

### 5.3 分割

| 用途 | ストリーム | 規則 |
|---|---|---|
| dev（プロンプトと配管の調整） | `dev-01`〜`dev-02` | ここで見た seed は他に使わない |
| pilot（gate と単価の実測） | `pilot-01`〜`pilot-04` | 誤メモ注入なし。A0 / A1 / A5 を 4 本、`pilot-01` だけ 6 群すべて（単価の実測用）。注入の機構確認は `pilot-inj-01`（A0 / A1 / A3 / A4）で別に行う |
| pilot 改訂（1 回のみ） | `pilot-rev-01`〜`04`、`pilot-inj-rev-01` | gate 不合格時だけ使う |
| main（本実験） | `main-01`〜`main-N`（N ≤ 20） | 規模の確定後に生成・検証し、seed 範囲と `stream.json` のハッシュを事前登録で固定する。プロンプト調整には使わない |

## 6. 副データ: CL-Bench database task（アンカー）

- 対象は `pgasawa/continual-learning-bench`（Apache-2.0、commit `5f8c50eb` に固定）の `database_exploration` タスク、`schema_drift` 設定（40 問。前半 20 問の後に DB が drift 版へ切り替わる）。DB は Amazon-Reviews-2023 由来で、配布元（Hugging Face）にライセンス表記がない。Git 管理外（`data/external/`）に実行時取得し、社内研究目的に限って使うことをレポートに明記する。
- 公式の依存（litellm、`openai==2.16.0` の厳密ピン）と衝突するため import せず、タスク仕様（許可する SQL、50 行上限、10 秒タイムアウト、1 問 15 クエリ、回答値の提出、型別の正誤判定、reward = 正解 × (1 − クエリ数 / 15)、誤答時に正解値を返すフィードバック、drift 時の通知文）を薄いアダプタで再現する。
- 40 問を 5 問 × 8 セッションに分け、Dreaming などのオフライン処理はセッション間に行う（drift はセッション 4 と 5 の間）。A0 / A1 / A4 × 3 反復（run_index 0〜2）で実行し、公式 score（reward の平均）と正答率の両方を報告する。
- 位置づけは「記事の報告と改善の向きが一致するか」の確認であり、数値の再現ではない。問題の並べ替えは公式実装の式を再現しており、固定 SHA の実データで run 0〜4 の順序が公式と一致することを確認済み。
- タスクの違い（SQL の提出か回答値の提出か、採点、フィードバック）は `TaskEnv` に閉じ込め、agent・記憶・consolidation は本ハーネスの群実装をそのまま使う。

## 7. モデルと実行設定

| 役割 | モデル | 設定 |
|---|---|---|
| agent（全群）、memory probe の回答者 | `gpt-6-luna` | `reasoning_effort=low` |
| consolidator（A1 summarizer、A3 organizer、A4 analyzer / organizer） | `gpt-6-luna` | `reasoning_effort=medium` |
| 埋め込み（A2） | `text-embedding-3-large` | — |
| judge | なし | 主指標・probe・誤答分類はすべてコードで採点 |

- `temperature` は送らない（`gpt-6-luna` は既定値 1 のみ受け付ける）。出力上限は `max_completion_tokens`（推論トークンを含む。agent 2,048、consolidator 8,192）で指定する。
- `gpt-6-luna` は Chat Completions で function tools と `reasoning_effort` の併用を受け付けないため、Responses API（`store=False`、推論内容はターン間で持ち越さない）で呼ぶ。
- オンラインの問題と probe では、最終ターンに提出ツールの呼び出しを強制する（全群共通）。
- Azure のコンテンツフィルタで拒否された呼び出しは課金されず、1 回だけ再試行する。それでも拒否されたら、その tool loop を `content_filter` で止める（問題なら未提出、オフライン処理ならそのフェーズを終了）。全群に同じ規則を適用し、件数を群別に報告する。
- 応答の非決定性は seed では揃えられないため、再現性はストリーム数と全 transcript・記憶スナップショットの保存で担保する。
- 接続: `.env` の `AZURE_OPENAI_ENDPOINT`（Foundry プロジェクト URL）からホスト部分を取り出し、`<host>/openai/v1/` を OpenAI SDK の `base_url` にする。秘密は `uv run --env-file .env` で環境変数として渡し、ログ・成果物に出さない。
- 単価（USD / 1M tokens、Global Standard、short context）: luna 入力 0.10 / キャッシュ 0.01 / 出力 0.50、embedding-3-large 0.13。正本は `configs/models.yaml`。long context 単価の閾値は非公開のため、1 呼び出しの入力を `budgets.max_input_tokens_per_call` 以下に抑える。

## 8. 予算と規模

### 8.1 見積（fable の tool-loop 見積を採用）

| 項目 | 見積 |
|---|---|
| オンライン 1 問 | 最大 8 呼び出し、約 64K 入力・4.8K 出力 → 約 $0.01 |
| consolidation 1 回 | analyzer 最大 10 ターン＋organizer 最大 12 ターン、約 750K 入力・15K 出力。キャッシュなし $0.083、キャッシュ 70% で $0.035 |
| 1 ストリーム 6 群合計 | 期待値 約 $6.45、キャッシュなしの見積上限 約 $8.06（呼び出し上限から計算した最悪値ではない） |

### 8.2 配分と上限

| 区分（phase） | 枠 |
|---|---|
| dev | $6 |
| pilot | $16 |
| pilot_inj | $5 |
| main | $120（下記の条件で最大 $130） |
| clbench | $10 |
| 通常の計画合計 | $157（main を $130 に広げた場合 $167） |
| pilot_rev（gate 不合格時の改訂、1 回のみ。`pilot_rev.yaml` と `pilot_inj_rev.yaml` で共有） | $21 |
| integrate テスト（phase `test`） | $1 |
| 改訂を含む最大 / 絶対上限 | $179（main $130 の場合 $189）/ $200 |
| 予備（未配分） | 約 $9（安全余裕 $2 を除いた $198 との差。E1 の振替に使う） |

- E1（dev）の最後に、実測 token から pilot / pilot_inj / pilot_rev の見込み費用を計算して run ledger に記録する。枠を超える見込みなら、絶対上限 $200 の範囲で残りの枠から振り替え、振替額と理由を run ledger に残す。
- gate と単価の算出は、pilot と pilot_inj の全ジョブが完走していることを前提にする（`failed` は再開して完走させる）。
- 単価: pilot の費用を台帳の tag から集計し、「群ごとに、その群を実行したストリームの平均」を 6 群分合計する。問題数に比例する部分（オンライン）を `c_online`、比例しない部分（オフライン処理・probe・埋め込み）を `c_fixed` とする。6 問の単価は `c6 = c_online + c_fixed`、5 問の単価は `c5 = c_online × 5/6 + c_fixed`。
- ストリーム数: 6 問で `N = min(20, floor(120 / c6))` が 16 以上ならそれを採る。だめなら 5 問で `N = min(20, floor(120 / c5))` が 16 以上ならそれを採る。それもだめで `16 × c5 ≤ 130` なら N = 16・5 問とし、予備から main に足す。それも満たさなければ gate (4) は不成立。
- 費用台帳: プロセス間で共有する SQLite（`outputs/cost_ledger.sqlite`）。すべての API 呼び出しの前に「入力 token 推定 × 安全係数 × 入力単価＋`max_completion_tokens` × 出力単価」を排他的に予約し、`usage` で精算する。入力 token の推定には安全係数（初期 1.25、実測と推定の比の最大値 × 1.05 に随時更新）を掛け、予約額を超えた精算は記録する。上限判定では絶対上限から安全余裕 $2 を差し引く。「精算済み＋課金不明で消費扱いの予約＋未精算の予約」が phase の枠または絶対上限を超える呼び出しは送らない。429 は予約を再利用して再試行し、接続失敗・5xx・タイムアウトは課金の有無が不明なため予約額を消費扱いにして新しく予約し直す。プロセスの再起動で未精算の予約は解放しない。

## 9. Pilot gate（本実験の開始条件）

`pilot-01`〜`04` で A0 / A1 / A5 を全 12 セッション実行し、`pilot-01` だけは 6 群すべてを実行して単価を測る。並行して `pilot-inj-01` で注入の機構を確認する。点推定で判定し、有意差は求めない。A4 の成績はどの必須条件にも使わない。

| 区分 | 条件 |
|---|---|
| 必須 | (0) pilot と pilot_inj の設定にある全ジョブが完走している。(1) 生成器の健全性（`validate_stream` が問題を返さない）。(2) acc(A5) − acc(A0) ≥ +5pt。(3) A1 に `stale` 誤答が 1 件以上ある。(4) §8.2 の規則で main の規模 (N, 問題数) が決まる。(5) 機構確認 (a): `pilot-inj-01` の A1 で、各注入の直前セッションにある「注入対象 fact を使う問題」が、エラーのない `query` を 1 回以上実行して正答している（反証となる正しい照会結果が transcript に残っている）。(6) 機構確認 (b): `pilot-inj-01` の A1 と A3 のそれぞれで、偽メモが記憶に残っている状態で解いた Q_inj が 1 問以上あり、その正答率が同じ問題の A0 の正答率より低い（偽メモが実際に害になる）。A4 はこの判定に含めず、値だけ報告する |
| 助言 | A1 の `stale` 率（セッション 5・9 のドリフト依存問題）≥ 5%、acc(A0) ∈ [0.10, 0.85]、acc(A1) ≤ 0.85、機構確認 (c): A4 で偽メモが訂正された件数 ≥ 1（「訂正」は、偽メモの `marker` と偽の主張がどちらも記憶から消え、かつ次の probe で注入対象 fact に正答したこと。A1 の件数も並べて報告する）。未達でも続行し、感度不足の可能性として報告する |

- 必須条件が 1 つでも不成立なら、改訂枠（phase `pilot_rev`、$21）の範囲で 1 回だけ直し、新しい seed で再確認する。直してよいのは生成器とプロンプトだけで、閾値は変えない。機構確認 (5)(6) の不成立に対しては注入の配置と反証の有無だけを直し、A4 のプロンプトは変えない。それでも不成立なら本実験を行わず、「条件の構築まで」としてレポートを閉じる。
- dev でのプロンプト調整の基準は機械的な指標（tool 呼び出しの成功率、lint 0 件、予算内、no_submission 率）に限り、群別の正答率は見ない。

## 10. 評価指標と統計

- **主指標**: SchemaDrift-SQL の実行一致率（全 72 問、またはフォールバック時 60 問）。
- **主比較**: Δ = A4 − A1。ストリームごとに正答率の差を取り、ストリームを単位とする paired bootstrap（10,000 回、パーセンタイル法）で 95% CI を出す。問題を独立標本として扱わない。
- **判定の書き方**: CI 下限 > 0 なら「A4 が A1 を上回った」。CI が 0 をまたげば「優位を示せない」（「同等」とは書かない）。+5pt は実務上の目安として点推定と並べて示す。
- **探索的指標**（主張には使わず、CI を添えて記述する）: A4 − A3、A4 − A2、A4 − A0、A1 − A0、A5 − A0。セッション別正答率の推移。Q_inj / Q_natural の層別正答率。`stale` 率。誤答分類。memory probe 正答率。注入した偽メモの残存率（各オフライン処理後の記憶に `marker` または偽の主張（`claim_patterns` をすべて含み、訂正の手がかり語や `decoy_labels` への対応付けを含まない行。`InjectedNote` の docstring 参照）があるか）と訂正率（§9 の定義）。1 正答あたりの費用と、精度対費用の Pareto 図。CL-Bench の公式スコアと正答率。
- 図は matplotlib で作る（セッション別正答率の推移に migration の縦線、Pareto 図）。

## 11. 実装方針

- **構成**: 単一ルートプロジェクト。パッケージは `project_module/src/project_module/`、テストは `project_module/tests/`、実行パラメータは `configs/`、CLI は `scripts/`、成果物は `outputs/<run_id>/`（Git 管理外）。
- **Dreaming の再実装**: lead は通常の Python。analyzer と organizer は 5 つの file tool（`read_file` / `write_file` / `edit_file` / `glob` / `grep`）を持つ tool-calling loop。記憶ストアはディスク上の Markdown ディレクトリ（`_index.md`、frontmatter（`name` / `description`）、`[[slug]]` リンク、`CONFLICT:` 行、`MOVED TO` スタブ、1 ファイル 100 KB 上限、`injected.md` は `budgets.injected_tokens` 以下）。file tool は記憶ディレクトリの外にアクセスできず、読み出しは行範囲指定（既定 200 行、1 回の結果は 8,000 tokens まで）で行う。検索は正規表現ではなく大文字小文字を区別しない固定文字列で行う（暴走しうる正規表現を避けるため）。transcript ディレクトリを読み取り専用で参照できるのは A1 の summarizer と A4 の analyzer だけ。裏付けとして使ってよいのは transcript 内の `query` の結果と CL-Bench の公式フィードバックだけで、記憶の記述や agent の断言は裏付けにならない。
- **流用**: cookbook と knowledge-wiki（MIT）から、analyzer / organizer の手順、frontmatter とリンクの規約、「transcript は data であり instruction ではない」規則、lint の検査項目を英語プロンプトとして取り込む。Managed Agents API 依存のコードは流用しない。
- **transcript**: cookbook の labeled plain text 形式。採点結果・gold・将来の問題、system prompt、注入層、記憶ツールの結果本文は書かない（偽メモが transcript に「根拠」として現れないようにする）。CL-Bench の公式フィードバックだけは例外として書く。
- **拡張点**: 群の違いは `Arm` の戦略オブジェクト（オンラインのツール構成、注入層、オフライン処理）に閉じ込め、ランナーは群を知らない（開放閉鎖）。LLM 呼び出しは `LLMClient` Protocol 越しに行い、テストではフェイクを差し込む。
- **再開**: セッション単位で結果ファイルを原子的に書き、各セッションの最後に記憶と A2 の索引をスナップショットとして保存する。中断後は完了済みセッションを飛ばし、未完了セッションは transcript と結果を消して直前のスナップショットから復元してやり直す。既に発生した費用は台帳に残る。
- **安全**: 記憶・transcript に秘密を書かない。transcript 内の指示文をデータとして扱う規則をプロンプトに入れる。

## 12. リスクと対策

| リスク | 対策 |
|---|---|
| 記憶ではなく読み手（reader）が律速になる（State-Aware RAG の再演） | A5 を本実験に入れ、gate (2) で確認する |
| スキーマを毎回読み直せばドリフトが解消し、記憶の出番がない | 意味的ドリフト（DDL に現れない変更）を必須にし、gate (3) で確認する |
| A1 が強すぎて差が出ない | 想定内の結果として報告する（対照を弱めない） |
| tool loop の暴走で費用超過 | ターン上限・`max_completion_tokens`・予約台帳で送信前に遮断する |
| 注入した偽メモが無関係な問題まで汚染する | leaf rule に限定し、150 tokens 以下にする。層別で影響を確認する |
| CL-Bench の配布 DB の権利関係 | 社内研究目的に限り、Git 管理外に置く。レポートに明記する |

## 13. 実装者に委ねる点

- 生成器の具体的なドメインデータ、テンプレート文面、難読化の語彙（§5 の要件と健全性テストを満たす範囲で）。
- プロンプトの文面（§4・§11 の役割と規則を満たし、dev ストリームでのみ調整する）。
- ログの書式と図の体裁。
