# 3プラン比較・統合メモ: Dreaming 技術検証

作成日: 2026-11-10

## 入力

| 種別 | ファイル | 作成者（モデル / effort） |
|---|---|---|
| 共通ブリーフ | [00-brief.md](./00-brief.md) | Claude Code（メインセッション） |
| 調査 | [survey/survey-codex.md](./survey/survey-codex.md) | Codex gpt-6.1-sol / xhigh |
| 調査 | [survey/survey-claude.md](./survey/survey-claude.md) | Claude サブエージェント opus / xhigh |
| 調査 | [survey/survey-gemini.md](./survey/survey-gemini.md) | Gemini 3.8 Flash (High)、Antigravity CLI 経由 |
| プラン | [plans/plan-codex.md](./plans/plan-codex.md) | Codex gpt-6-astra / high |
| プラン | [plans/plan-claude.md](./plans/plan-claude.md) | Claude サブエージェント fable / high |
| プラン | [plans/plan-gemini.md](./plans/plan-gemini.md) | Gemini 4 Argon (High)、Antigravity CLI 経由 |

- プラン担当の3者には、ブリーフと調査3件を同じ形で渡した。他の担当のプランは見せていない。
- Gemini のプランは、1回目が出力上限で §0 の途中で切れた。§0 を末尾の付録に回すよう指示して再実行し、完全版を得た。

---

## 1. 3プランの要約比較

| 観点 | Codex（gpt-6-astra） | Claude（fable） | Gemini（4 Argon） |
|---|---|---|---|
| 主仮説 | Source-verified Dreaming が、同じ予算の **Rolling Summary（transcript も読める）** より最終正答率を **+5pt** 上回る | transcript で検証する Dreaming（A4）が、**transcript を読まずに整理だけする対照（A3）** より mean reward（CL-Bench 式）を **+0.05** 上回る | Verified Wiki Dreaming（C4）が Rolling Summary（C3a）に **+8pt**、Notes-only（C2）に **+10pt** の差をつけ、かつ退行率が 5% 未満（3条件の AND） |
| 主対照の意味 | 「強い要約」に対する実用上の上積み（検証・状態遷移管理を含む一式としての効果） | Dreaming 固有の成分（transcript による裏付け）だけを切り分ける | 実用上の上積みと Notes-only 比較を同時に要求する |
| 他の比較群 | No-memory、Raw-RAG、Online Notes、全文 ICL。ablation として D-flat と D-no-reverify | Stateless、Transcript-RAG、Notes-only、+Gate、Oracle memory、全文 ICL | Stateless、Hybrid RAG（+reranker）、Notes-only、Verify ablation（C3b）、**Mem0 v3（ADD-only）**、NoGate |
| 主データ | 自作 **Dreaming-Drift-JA**（テキスト業務環境。test 160環境 × 64セッション × 3反復） | 自作 **SchemaDrift-SQL**（難読化 SQLite。12セッション × 6問 × **20〜30ストリーム**） | 自作 **DriftOps-JA**（社内文書と SQLite。30セッション × 4問 × **5 seeds**）＋ CL-Bench DB を主仮説に統合 |
| 外部ベンチ | LongMemEval-S 150問 | LongMemEval-S 120問、CL-Bench アンカー（データライセンス確認が条件） | LongMemEval-S 150問、CL-Bench（主仮説に含む） |
| 誤記憶・ドリフト・注入 | 誤メモ注入 10%（80環境）、ドリフト、安全性 suite 300件 | migration 2回、誤記憶 6件（反証となる tool 結果を必ず配置）、命令文と canary | ドリフト 2回、誤メモ 20件、Recency 罠 15件、MINJA 型注入と canary 15件 |
| 記憶品質の測り方 | 根拠付き主張率、破壊率、訂正率、stale 率 | 記憶だけで答える **memory probe** を gold fact ledger で機械採点（corruption / repair / hallucination） | TrustMem 指標（omission / corruption / hallucination）、Toxin clearance |
| reader ボトルネック | oracle-source reader と Sol での診断 | Oracle memory 群（A6） | Reader error rate を集計し、Sol で感度分析 |
| 統計 | 環境単位の paired cluster bootstrap。デザイン効果を入れた検出力計算 | ストリーム単位の cluster bootstrap。仮定 SD 0.08 なら n≈20、SD が 0.11 を超えたら 30 に増やす | 2段階の paired cluster bootstrap、McNemar、Holm 補正 |
| モデル | agent・analyzer・organizer は luna。Judge は Claude Sonnet 5.5（Foundry）。感度分析に Sol。埋め込みはローカルの multilingual-e5-small | agent・dreaming は luna。Judge は Claude Haiku 5.5（Foundry）。感度分析に Sol。埋め込みはローカルの Qwen3-Embedding-0.6B | agent・dreaming は luna。Judge は Claude Sonnet 5.5。感度分析に Sol。埋め込み Qwen3-Embedding-0.6B と reranker をローカル（MPS） |
| SLEEP 型（重み更新） | 対象外 | 対象外（付録で触れるのみ） | 探索 E5: MLX で Qwen3.5-4B の QLoRA 蒸留プローブを行う |
| API 費用 | 中央値 約 $560、停止上限 $900 | 約 $750（必須部分 約 $600） | 約 $183 |
| API 実行時間 | 4〜11日 | 本実験 約2日 | 約25時間 |
| 期間 | 4〜5週 | 4〜5週（W5 は予備） | 4週 |

### 共通点（3者が独立に一致した点）

- 対象は**重みを更新しない外部メモリの Dreaming** に絞る。cookbook の analyzer→organizer 方式を Python で再実装し、memory store は Markdown＋Git で監査する。
- 主対照は「オフライン予算を揃えた強い整理・要約群」にする。そのうえで、Raw transcript RAG と全文 ICL（2026 年の報告で専用メモリに勝った方式）も比較に入れる。
- 主データは**自作の、ドリフトと誤記憶を含む逐次タスク**にし、機械採点する。外部ベンチは LongMemEval-S（MIT）を使う。LoCoMo（CC BY-NC）など、ライセンス上問題のあるものは除外する。
- 本実験の前に**操作チェック**を行う。記憶が読まれること、ドリフトが効くこと、誤メモが害を出すこと、文脈圧が生じることを確かめ、満たさなければ No-Go とする。
- **記憶内容の正しさ・最終回答の正しさ・reader 側の誤り**は、それぞれ別の指標で測る。
- モデル構成
  - agent と dreaming は `gpt-6-luna`（Foundry、Global Standard）で揃える。
  - Judge は別系列の Claude 5.5 系を Foundry で使う。
  - 感度分析には `gpt-6-sol` を使う。
  - 埋め込みはローカルで実行する。
- 一次情報で確認した事実（3プランが一致）
  - `gpt-6-luna` は short context で $0.10 / キャッシュ $0.01 / 出力 $0.50。
  - short / long の閾値は公開情報にない。
  - Anthropic の Dreams API は Foundry では使えない。

**メインセッションでの確認**: luna / sol / astra の単価は、Azure 公式ブログ（2026-09-22、"GPT-6 Astra, Sol, and Luna for production agents in Microsoft Foundry"）で直接確認した。ブログには short / long の閾値の記載がなく、temperature と reasoning effort への対応についても記述がない。

---

## 2. 各プランの強みと懸念

### Codex（gpt-6-astra）

- 強み
  - 統計設計が最も厳密。独立単位を環境とし、デザイン効果を入れて最小検出差を計算している。「有意性」と「差が 5pt 以上」を分けて説明している。
  - 「主仮説を支持した」と「導入を推奨する」を分けている。RAG や ICL が同等で安ければ推奨しない、という導入条件も明記している。
  - 「予算一致」の限界（実際に消費した token の差）を記録し、どう扱うかも決めている。
- 懸念
  - **規模が大きすぎる**。160環境 × 64セッション × 3反復で、約25万 calls・入力約 3B tokens。API 実行だけで 4〜11 日かかり、1人で 3〜5 週という枠を超えるおそれがある。
  - 主対照の Rolling Summary も transcript を読めるため、主仮説の差は「主張単位の再検証と状態遷移管理を行うか」という一式の効果になる。transcript で裏を取ること自体の効果を切り分けるのは、探索的 ablation（D-no-reverify、40環境 × 1反復）に回っている。

### Claude（fable）

- 強み
  - **最も実装しやすく、仕様も具体的**。SchemaDrift-SQL は CL-Bench DB task と同じ系統の SQLite 環境である。
  - 「誤記憶の直前セッションに、それを反証する tool 結果を必ず置く」設計で、Dreaming が検証できる状況を保証している。
  - gold fact ledger と memory probe で、記憶の正しさを機械採点できる。
  - Oracle memory 群で reader の限界を直接測れる。
  - 操作チェック C1〜C5 の閾値が具体的で、Go/No-Go 判断に使いやすい。
- 懸念
  - 主対照 A3 は transcript を読めない。そのため「transcript を読めて要約もする強い対照」より弱いと批判されうる。State-Aware RAG の先行検証では、まさに汎用要約が勝った。
  - A3 と A4 は入力の種類が違い（notes の差分か transcript か）、消費 token が自然にずれる。プランは ±20% のチェックで対処している。
  - 主指標の reward は、正誤と探索クエリ数を混ぜた値になる。CL-Bench に準拠した設計だが、accuracy を副指標として併記する必要がある。
  - 20 ストリームで足りるかは、仮定した SD（0.08）次第。pilot で増やす規則は決めてある。

### Gemini（4 Argon）

- 強み
  - **比較群の網羅性が最も高い**。Verify ablation（C3b）と Rolling（C3a）の両方を持ち、Mem0 v3 の ADD-only を外部 OSS の代表として入れている。
  - 敵対的な条件が豊富。Recency 罠、MINJA 型注入、Gate の有無の比較がある。
  - 調査3件の食い違いを丁寧に照合しており（付録）、判断の多くは他の2プランと一致する。
- 懸念
  - **seed が 5 では cluster 単位の統計として弱い**。独立単位が 5 だと、bootstrap の CI は不安定になる。
  - 主仮説が3条件の AND で、しかも CL-Bench（配布 DB のライセンスが未確認）を主仮説に統合している。ライセンスで使えなくなった場合に、主仮説が崩れる。
  - DriftOps-JA は、先行検証の **StateDrift-JA 472文書** を土台にしている。本リポジトリには無く、使えるかどうかも確認していない（Codex 版は「前提にしない」と明記）。
  - GPT-6 系に `temperature=0.0` を指定している。対応しているかは未確認で、他の2プランは「非対応の可能性あり、W1 で確認」としている。
  - 埋め込み・reranker・MLX をホストの MPS で動かす前提になっている。AGENTS.md §4 の「Docker が既定、ホスト実行は承認制」と整合させる必要がある。
  - 費用 $183 は、1タスクあたりの入力を約 37K tokens と見ている。他のプランは 1問あたり約 80K tokens 程度と見ており、楽観寄りである。
  - 誤記（「無제한」など）がある。

---

## 3. 推奨する統合案

3者の合意点を土台にし、規模と統計の頑健さは次のように選ぶ。

| 項目 | 推奨 | 採用元 |
|---|---|---|
| 主仮説 | Verified Dreaming が、**transcript を読める予算一致の Rolling Summary** より主指標を +5pt 以上改善する。cluster bootstrap の 95% CI 下限が 0 を超えることを条件とする | Codex・Gemini の主対照と、Codex の成功条件 |
| 主な探索的仮説 | 1. **Verify ablation**（transcript を見ない整理のみ）との差で、裏付けの固有効果を測る<br>2. Raw-RAG に対する非劣性と、注入 token の削減<br>3. 誤記憶の訂正率と、正→誤の破壊率<br>4. ドリフト後の stale 率<br>5. Gate による注入・canary の遮断<br>6. Oracle memory による reader の限界 | Claude（A3, A6）、Gemini（C3b, Gate）、Codex（E1〜E6） |
| 比較群 | Stateless / Raw-RAG（hybrid）/ Notes-only / Rolling Summary（主対照）/ Verify ablation / Verified Dreaming / Oracle memory。探索として全文 ICL と Mem0 v3 を加える | 3者の和集合から、必須ではないものは探索枠に移した |
| 主データ | **SchemaDrift-SQL 型**の自作 SQLite 逐次環境。反証となる tool 結果を必ず置く設計と、ledger＋probe を採用。規模は **30 ストリーム**（12〜16 セッション）を基本にし、pilot の分散次第で調整する | Claude をベースに、Gemini の Recency 罠・注入と、Codex の業務テーマ多様化を部分的に取り込む |
| 外部ベンチ | LongMemEval-S 120〜150 問（副次）。CL-Bench は配布 DB のライセンスを確認できた場合だけアンカーとして使い、主仮説には入れない | Claude・Codex |
| モデル | agent・analyzer・organizer・probe は `gpt-6-luna`。Judge は Foundry 上の Claude（Haiku 5.5 か Sonnet 5.5）。感度分析に `gpt-6-sol`。W1 に temperature / reasoning_effort への対応と quota を実機で確認する | 3者共通 |
| ローカル | Docker（CPU）で、Qwen3-Embedding-0.6B か multilingual-e5-small、bm25s、LanceDB か sqlite-vec、SQLite を動かす。ローカル LLM と SLEEP 縮小再現は初期スコープから外す（必要なら別の付録実験にする） | Claude・Codex。Gemini の E5 は保留 |
| 予算 | 停止上限 **$800**、中央見積 $400〜600 | 3者の見積の中間 |
| 期間 | 5週（W1 基盤と生成器、W2 全群実装・pilot・事前登録、W3 本実験、W4 副次実験と解析、W5 レポート） | Claude |

---

## 4. ユーザー判断が必要な点

1. **主対照をどちらにするか**
   - (a) transcript を読める Rolling Summary（実用上の上積みを問う。推奨）
   - (b) transcript を読まない Verify ablation（Dreaming 固有の成分を問う）
   - 主仮説は事前登録で1つに絞るので、どちらかに決める必要がある。
2. **規模と予算の上限**: 推奨は30ストリーム・$800。Codex 規模（約 $900・API 4〜11日）まで広げるかどうか。
3. **CL-Bench 配布 DB の扱い**: 元データ Amazon-Reviews-2023 由来の DB で、ライセンスが明記されていない。社内研究目的に限って使うことを許容するか。
4. **Foundry 上の Claude（Judge）の調達**: Marketplace 購読が要る。使えない場合は、Sol を Judge にして人手監査を増やすことになる。
5. **SLEEP 型（重み更新）の扱い**: 本検証から外し別テーマにするか、MLX の縮小プローブ（Gemini E5）を付録として入れるか。
6. **リポジトリ初期化**: 今回はスキップした。実装に入る前に初期化と commit 方針を決める必要がある（現在は `main` で、未コミットのファイルが多数ある）。
