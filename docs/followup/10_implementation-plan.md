# Dreaming 追試 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 既存ハーネスに 6 つの処置スイッチ（採点フィードバック、回答後のメモ作成、A2 の検索結果注入、確認指示、統合役の照会、drift なしストリーム）と、複数 run をまたぐ解析・gate を足す。そのうえで、dev → pilot gate → 事前登録の凍結 → 本番 → レポートまでを、追加費用 $100 以内で実行する。

**Architecture:**
- 処置はすべて `ExperimentConfig` の設定キーで切り替える。群名（A0〜A5）は増やさない。
- 条件は「run × 群」で表す。フィードバックの有無などの条件ごとに run を分け、同じストリームで同時に走らせる。
- 解析は「ラベル → run ディレクトリ」の対応で複数 run を読み、ストリームごとのスコアを突き合わせて、既存の `paired_bootstrap` に渡す。

**Tech Stack:** 既存のまま（Python 3.14、uv、openai SDK、pydantic、numpy、SQLite、pytest）。依存は追加しない。

**Spec:** [00_design.md](00_design.md)（設計の正本）、[20_preregistration.md](20_preregistration.md)（凍結する判定条件）。前回の設計 [../plan/00_design.md](../plan/00_design.md) を引き継ぐ。

## Global Constraints

- 前回の `docs/plan/10_implementation-plan.md` の Global Constraints をすべて引き継ぐ。
  - Python 3.14 / `uv run`、寛容ライセンスのみ、`exclude-newer` を維持する。
  - 全呼び出しは `MeteredClient` と台帳 `outputs/cost_ledger.sqlite` を通す。
  - `.env` とホスト名を出力しない。
  - 実験の文言は英語、docs は日本語。
- 依存は追加しない（`uv add` なし）。統計は numpy と既存の `evaluation/stats.py` だけで書く。
- 新しい設定キーの既定値は、前回の挙動と完全に一致させる。既存 7 run（`dev`、`pilot`、`pilot_inj`、`pilot_rev`、`pilot_inj_rev`、`main`、`clbench`）の manifest は、`matches_config(digest, config)` で一致し続けること。
- 共通の統合プロンプト `prompts/_consolidation_core.md` と、既存のプロンプトファイルの本文は変更しない。処置に必要な文言は、新しい断片ファイルとして足す。
- 新しい phase は `fu_dev`、`fu_pilot`、`fu_pilot_rev`、`fu_main`、`fu_aux`、`fu_clbench` の 6 つ。枠は 00_design §8 の値にする。
- 前回の文書（`docs/plan/`）と前回の設定ファイル（`configs/experiments/*.yaml`）は変更しない。追試の設定は `configs/experiments/followup/` に置く。
- A0・A1・A4・A5 のオンラインの入力に、フィードバックも採点結果も入れない（A2 の例外は 00_design F17）。
- 予算超過（`BudgetExceeded`）は、前回と同じく呼び出し元に伝える。新しい処理で握りつぶさない。

## Review Focus

- **前回 run の再開互換**: 前回の 7 run の manifest を読み直したとき、新しいキーを足した後も設定 digest が一致する。一方で、新しいキーを既定値以外にした設定は、旧 digest と一致しない。→ T0 `test_old_manifests_still_match_after_new_fields`、`test_changed_switch_does_not_match_old_digest`
- **メモのターンの会話の整合**: 提出の呼び出しと同じ応答に別の呼び出しがあった場合や、ターン上限で終わった場合でも、出力のない function call が残らない。メモのターンが API に拒否されない。→ T3 `test_note_turn_closes_all_unanswered_calls`、`test_note_turn_runs_after_no_submission`
- **フィードバックの漏れ**: `graded` の run でも、A0・A1・A4 の system prompt・問題文・メモのターンに、フィードバックや期待値が含まれない。→ T3 `test_feedback_never_reaches_agent_messages`
- **メモの上書き**: 同じセッションの 6 問のメモが、別々のファイルに残る。→ T3 `test_notes_of_one_session_do_not_overwrite`
- **drift なしストリームの取り違え**: `drift: false` の設定で drift ありのストリームを読む場合（またはその逆）は、実行前に `ValueError` で止まる。→ T1 `test_make_env_rejects_drift_mismatch`

---

## 実行体制

| 波 | タスク | 担当（モデル / effort） | 並列 |
|---|---|---|---|
| 0 | T0 設定・契約・digest 互換 | メインセッション | 直列（以降すべての前提） |
| 1 | T1 フィードバックと env / T2 drift なし生成器 / T3 オンラインのセッション処理 / T4 統合処理 / T5 追試の解析と gate | T1: Sonnet xhigh、T2: Opus high、T3: Opus high、T4: Opus high、T5: Opus high | 5 並列 |
| 2 | T6 追試の設定ファイル・E2E | Sonnet xhigh | 単独 |
| R1 | 実装一式の最終レビュー（T0〜T6 まとめて） | Opus xhigh サブエージェント ＋ Codex gpt-6.1-sol xhigh | 2 並列 |
| E | E1 dev → E2 本番ストリームの生成と pilot / gate → E3 凍結と本番 ‖ E4 CL-Bench → E5 感度分析（任意）→ E6 解析・レポート・Notion | メインセッション（解析補助に Sonnet xhigh） | E3 と E4 は並列 |
| R2 | 結果とレポートの最終レビュー | Opus xhigh ＋ Codex gpt-6.1-sol xhigh | 2 並列 |

- **波 1 の編集範囲**: 各タスクは、自分の **Files** に挙げたファイルだけを編集する。共有の型と Protocol は T0 で凍結する。
  - `agent/session.py` は T3 だけが編集する。T4 は、そこから `QueryTool` と `CombinedToolSet` を import するだけにする。
  - `agent/memory_arms.py` は T4 だけが編集する。
  - T3 のメモのターンは、「agent の tool 群に `memory_write` があれば回す」という、群に依存しない作りにする。A1 に `memory_write` を与えるのは T4 である。
- **波の終わり**: メインセッションが `make test` と `make all-lint` を通し、波ごとに 1 commit にまとめる。
- **協議**: 実装中に解けない判断が出たときだけ、Opus xhigh と Codex gpt-6.1-sol xhigh に並行で相談する。設計の根幹に関わるものだけ、Codex gpt-6-astra high と fable high に相談し、割れたら Gemini 4 Argon High を加える。
- **ブランチ**:
  - 設計文書: `docs/followup-design`（本 PR）
  - 実装: `feature/followup-harness`（T0〜T6、R1 の後に PR）
  - 実行と結果: `feature/followup-results`（E1〜E6、R2 の後に PR）
  - 各 PR をマージしてから、次のブランチを `main` から切る。worktree を使った場合は、最後に削除する。

---

### Task 0: 設定・契約・digest 互換（メインセッション）

**Files:**
- Modify: `project_module/src/project_module/config.py`, `contracts.py`, `manifest.py`, `agent/context.py`, `agent/online_arms.py`・`agent/memory_arms.py`（`question_context` の空実装を足すだけ）, `consolidation.py`（`PhaseSpec` のフィールド追加だけ）, `schemadrift/models.py`, `schemadrift/generator.py`（引数の追加だけ）
- Test: `project_module/tests/test_contracts.py`, `project_module/tests/test_runner.py`

**Interfaces:**
- Produces（`config.py`）:
  - `Phase` に `"fu_dev"`, `"fu_pilot"`, `"fu_pilot_rev"`, `"fu_main"`, `"fu_aux"`, `"fu_clbench"` を追加する。
  - `ExperimentConfig` に次のフィールドを足す。
    - `feedback: Literal["none", "graded"] = "none"`
    - `session_notes: bool = False`
    - `a2_retrieval: Literal["tool", "auto"] = "tool"`
    - `agent_prompt_addendum: Literal["none", "verify_memory"] = "none"`
    - `consolidator_sql: bool = False`
    - `drift: bool = True`
  - validator: `task == "clbench"` で、上記 6 つのどれかが既定値以外なら `ValueError`。
- Produces（`manifest.py`）:
  - `LATER_DEFAULTED_FIELDS: tuple[str, ...]` は、後から足した既定値つきフィールドの名前。`false_note_timing` と上記 6 つを持つ。既定値は `ExperimentConfig.model_fields[name].default` から引く。
  - `config_sha256(config) -> str` は、`LATER_DEFAULTED_FIELDS` のうち既定値と等しいものだけを除外した dump の SHA-256 にする（`exclude_defaults` は使わない）。
  - `matches_config(digest: str, config: ExperimentConfig) -> bool`（シグネチャは現行のまま）は、次のどちらかが一致すれば真にする。
    1. 新しい digest
    2. commit `d71c2f4` 形式（`false_note_timing` を含み、他の 6 つを除外した dump）の digest。ただし 6 つがすべて既定値のときに限る。
- Produces（`contracts.py`）:
  - `QuestionResult` に `note_written: bool = False`、`retrieved_tokens: int = 0` を追加する。
  - `ConsolidationRecord` に `notes_files_read: int = 0`、`sql_queries: int = 0` を追加する。
- Produces（`agent/context.py`）:
  - `Arm` Protocol に `def question_context(self, ctx: ArmContext, question: TaskQuestion) -> str: ...` を追加する。
  - 戻り値は問題文の前に置く文字列で、何も置かないときは `""`。既存の全群に `""` を返す実装を足す。A2 の中身は T3 が書く。
- Produces（`consolidation.py`）: `PhaseSpec` に `sql_probe: bool = False` を追加する（配線は T4）。
- Produces（`schemadrift/models.py`）:
  - `Stream` に `drift: bool = True` を追加する。
  - 既定値（真）のときは `stream.json` の dump から除く。既存ストリームのハッシュを変えないためである。
- Produces（`schemadrift/generator.py`）:
  - `generate_stream(stream_id, seed, out_dir, *, sessions=12, questions_per_session=6, inject: bool, drift: bool = True) -> Stream`。
  - T0 では `drift=False` で `NotImplementedError` を投げておき、T2 が実装する。

- [x] **Step 1: 失敗するテストを書く**

```python
def test_new_fields_default_to_previous_behaviour(): ...        # 既定値の dump と、前回の main.yaml の config_sha256 が一致する
def test_clbench_rejects_schemadrift_switches(): ...            # task=clbench で 6 つのいずれかを既定値以外 → ValidationError（parametrize）
def test_old_manifests_still_match_after_new_fields(): ...      # test_runner.py:505-530 の新旧 2 形式に加え、d71c2f4 形式でも True
def test_changed_switch_does_not_match_old_digest(): ...        # feedback="graded" の config は、旧形式の digest と一致しない
def test_followup_phases_are_valid(): ...
def test_stream_dump_omits_default_drift(tmp_path): ...         # drift=True の dump に "drift" キーがない。False ならある
```

- [x] **Step 2: 失敗を確認する** — Run: `uv run pytest project_module/tests/test_contracts.py project_module/tests/test_runner.py -q` / Expected: 上記が FAIL
- [x] **Step 3: 上記 Interfaces を実装する**
- [x] **Step 4: 互換を実データで確かめる** — Run: `outputs/*/manifest.json` の 7 件を読み、それぞれの当時の yaml を `load_experiment` で読んで `matches_config` を確かめる一回限りのスクリプト / Expected: 7 件すべて True（前回 R4 と同じ手順）
- [x] **Step 5: `make test` と `make all-lint` を通して commit する** — `git commit -m "feat: add follow-up switches, phases and digest compatibility"`

---

### Task 1: 採点フィードバックと env の照合（Sonnet xhigh）

**Files:**
- Modify: `project_module/src/project_module/schemadrift/env.py`, `runner.py`（`make_env` と `generate_streams` だけ）
- Test: `project_module/tests/schemadrift/test_env.py`, `project_module/tests/test_runner.py`

**Interfaces:**
- Consumes: T0 の `ExperimentConfig.feedback` / `drift`、`Stream.drift`、`generate_stream(..., drift=)`
- Produces:
  - `SchemaDriftEnv.__init__(..., feedback: bool = False)`。`feedback(q, grade) -> str | None` は、`feedback` が偽なら None、真なら 00_design §4.1 の文字列を返す。
  - 定数 `FEEDBACK_MAX_ROWS = 20`、`FEEDBACK_MAX_CHARS = 2000`。
  - `make_env` は `config.feedback == "graded"` を env に渡す。`config.drift != stream.drift` なら `ValueError`。
  - `generate_streams` は `drift=config.drift` を渡す。

- [x] **Step 1: 失敗するテストを書く**

```python
def test_feedback_disabled_returns_none(): ...             # 既存 test_env.py:120 の期待を「既定では None」と読み替える
def test_feedback_correct_line():
    assert env.feedback(q7, Grade(category="correct", score=1.0)) == "Question 7: CORRECT!"
def test_feedback_incorrect_lists_expected_rows(): ...     # "Question 7: INCORRECT.\nCorrect answer:\n" + 期待値の行（" | " 区切り）
def test_feedback_no_submission_wording(): ...             # CL-Bench と同じ文言
def test_feedback_truncates_long_expected(): ...           # 25 行の期待値 → 20 行 + "[truncated]"、2000 文字以内
def test_make_env_rejects_drift_mismatch(tmp_path): ...
def test_make_env_passes_graded_feedback(tmp_path): ...
```

- [x] **Step 2: 失敗を確認する** — Run: `uv run pytest project_module/tests/schemadrift/test_env.py project_module/tests/test_runner.py -q`
- [x] **Step 3: 実装する**
  - 期待値の各セルは `env.py` の `_cell` で整える。
  - `{n}` は、ストリーム内の問題の並び順（セッション順、セッション内の出題順）で振る 1 始まりの番号とする。
- [x] **Step 4: テストが通ることを確認する**（同じコマンド）/ Expected: PASS

---

### Task 2: drift なしストリームの生成と検証（Opus high）

**Files:**
- Modify: `project_module/src/project_module/schemadrift/generator.py`, `schemadrift/validate.py`
- Test: `project_module/tests/schemadrift/test_generator.py`, `project_module/tests/schemadrift/test_validate.py`

**Interfaces:**
- Consumes: T0 の `generate_stream(..., drift: bool = True)`、`Stream.drift`
- Produces:
  - `drift=False` のストリームは `Stream.drift = False` を持つ。
  - そのストリームでは、全 Fact が `valid_from_session == 1` かつ `valid_until_session is None`。全問題が `drift_dependent == False`。
  - `validate_stream` は `stream.drift` が偽のとき、`_check_migrations` と `_check_drift_majority` を飛ばす。

- [x] **Step 1: 失敗するテストを書く**

```python
def test_no_drift_stream_has_static_facts(tmp_path): ...
def test_no_drift_keeps_names_of_same_seed(tmp_path): ...       # 同じ seed の drift あり版と、エポック 1 の表名・列名・コード表が一致する
def test_no_drift_streams_validate_for_many_seeds(tmp_path): ...  # seed 4501〜4520 で validate_stream が問題を返さない
def test_drift_default_is_unchanged(tmp_path): ...              # drift=True（既定）の stream.json と DB が変更前と同じハッシュ（seed 4521 で、変更前の値を固定値として埋める）
```

- [x] **Step 2: 失敗を確認する** — Run: `uv run pytest project_module/tests/schemadrift -q`
- [x] **Step 3: 実装する**
  - `_make_design`: `visible` と `semantic` を変更前と同じ順で抽出してから、`change_epoch` を `{}` にする。
  - `_plan_slots`: エポック 2 以降も drift 枠を 0 にし、`"prime"` の役割を `"any"` に替える。
  - `_Selector._try`: `"prime"` の役割を前提にしないようにする。
- [x] **Step 4: テストが通ることを確認する** — 同じコマンドと、`-m slow` の 100 seed テストを実行する / Expected: PASS

---

### Task 3: オンラインのセッション処理（Opus high）

**Files:**
- Modify: `project_module/src/project_module/agent/session.py`, `llm/loop.py`, `agent/online_arms.py`
- Create: `project_module/src/project_module/prompts/note_turn.md`, `prompts/agent_verify_addendum.md`
- Test: `project_module/tests/agent/test_session.py`, `project_module/tests/agent/test_online_arms.py`, `project_module/tests/llm/test_loop.py`

**Interfaces:**
- Consumes: T0 の `ExperimentConfig.session_notes` / `a2_retrieval` / `agent_prompt_addendum`、`Arm.question_context`、`QuestionResult.note_written` / `retrieved_tokens`
- Produces（`llm/loop.py`）:
  - `LoopResult` に `messages: list[dict[str, Any]]` を足す。中身は、system を除いた、このループで積んだ会話である。
  - `run_forced_turn(client: LLMClient, *, model: str, reasoning_effort: str, system: str, history: list[dict[str, Any]], user: str, tools: ToolSet, terminal_tools: Sequence[dict[str, Any]], forced_tool: str, max_completion_tokens: int, max_input_tokens_per_call: int, tag: CostTag, on_event: EventHandler | None = None) -> str | None`
    - `history` の最後の assistant 応答で、出力のない function call すべてに出力を補う。`terminal_tools` に名前がある呼び出しには `Submitted.`、それ以外には `Not executed: the question is closed.` を返す。
    - 続けて `user` を積み、回答ループと同じ並び `[*tools.schemas(), *terminal_tools]` の tool スキーマを渡し、`tool_choice` で `forced_tool` を強制して 1 回だけ呼ぶ。
    - 送る前に入力 token を数え、`max_input_tokens_per_call` を超えるなら呼ばずに None を返す。
    - 返った呼び出しを `tools.dispatch` し、その結果の文字列を返す。
    - 捕捉するのはコンテンツフィルタの拒否だけで、その場合は None を返す。`BudgetExceeded` などは伝える。
- Produces（`agent/session.py`）:
  - system prompt は、`agent_prompt_addendum == "verify_memory"` のとき、`system_prompt(base, injected)` の結果（記憶ブロックを含む）の末尾に `agent_verify_addendum.md` を足す。ファイル名は `Literal` から対応表で引き、設定値の文字列をパスに使わない。
  - `_solve` は、`arm.question_context(ctx, question)` を問題文の前に置いて `run_tool_loop` の `user` にする。transcript には問題文だけを書く。
  - `_solve` は回答ループと採点の間で、`ctx.config.session_notes` が真で、かつ agent の tool 群に `memory_write` があるときだけ `run_forced_turn` を呼ぶ。
    - `user` は `note_turn.md` にパス `notes/s{session:02d}-{qid}.md` を埋めた文。`max_completion_tokens=1024`、tag の component は `online`。
    - 呼び出しは `_QuestionRecorder` に渡し、transcript に残す。ただし、結果行の `tool_calls`・`tool_errors`・`turns` には数えない。
    - 実行後にそのパスのファイルが空でなく存在すれば、`note_written=True` にする。
    - フィードバック行は、その後に書く。
- Produces（`agent/online_arms.py`）:
  - `RawRagArm.question_context` は、`a2_retrieval == "auto"` のとき、`ctx.index.search(question.text, k=8, token_budget=budgets.online.memory_read_tokens, tag=ctx.tag("A2", question.session, "online"))` の断片を、見出し `Relevant excerpts from past sessions:` の下に並べて返す。結果が空なら `""` を返す。
  - `auto` のとき、`online_tools` は None を返す。
  - `retrieved_tokens` は `count_tokens(question_context の結果)` で、`_solve` が記録する。
- `prompts/agent_verify_addendum.md` の本文は、00_design §4.4 の英文そのままとする。

- [x] **Step 1: 失敗するテストを書く**

```python
def test_note_turn_writes_note_after_submit(): ...            # FakeLLM の台本: query → submit → memory_write。note_written=True、notes/s01-<qid>.md がある
def test_note_turn_closes_all_unanswered_calls(): ...         # 同じ応答に submit と query → 両方に出力を補った会話が送られる（FakeTransport.calls で call_id の対応を確かめる）
def test_note_turn_runs_after_no_submission(): ...            # ターン上限で未提出 → メモのターンは回り、会話に出力のない call が残らない
def test_note_turn_skipped_without_memory_write_tool(): ...   # A0 では呼び出し回数が増えない
def test_note_turn_content_filter_is_recorded_not_raised(): ...  # note_written=False、例外なし
def test_note_turn_propagates_budget_exceeded(): ...
def test_notes_of_one_session_do_not_overwrite(): ...         # 2 問で 2 ファイル
def test_note_turn_not_counted_in_result_tool_calls(): ...
def test_feedback_never_reaches_agent_messages(): ...         # graded の env の A0/A1/A4 で、FakeTransport.calls の全 messages に "CORRECT" / "Correct answer" が現れない
def test_feedback_line_written_after_note(): ...              # transcript で TOOL CALL memory_write が EVENT feedback より前
def test_verify_addendum_follows_memory_block(): ...          # 有効時だけ、記憶ブロックの後に足される
def test_auto_retrieval_prepends_excerpts(): ...              # 2 セッション目の user に見出しと断片がある。retrieved_tokens > 0
def test_auto_retrieval_not_written_to_transcript(): ...      # transcript の USER 行は問題文だけ
def test_auto_mode_has_no_search_tool(): ...
def test_run_forced_turn_dispatches_once(): ...               # llm/test_loop.py
def test_note_turn_sends_same_tool_schemas_as_answer_loop(): ...  # FakeTransport.calls の tools 引数が、回答ループと同じ並びで一致する
```

- [x] **Step 2: 失敗を確認する** — Run: `uv run pytest project_module/tests/agent project_module/tests/llm/test_loop.py -q`
- [x] **Step 3: 上記 Interfaces を実装する** — `note_turn.md` は 00_design §4.2 の要件を満たす英文で書く（dev で調整してよい）。
- [x] **Step 4: テストが通ることを確認する**（同じコマンド）/ Expected: PASS

---

### Task 4: 統合処理（Opus high）

**Files:**
- Modify: `project_module/src/project_module/consolidation.py`, `agent/memory_arms.py`
- Create: `project_module/src/project_module/prompts/notes_handling.md`, `prompts/rolling_review_notes.md`, `prompts/consolidator_sql_addendum.md`
- Test: `project_module/tests/test_consolidation.py`, `project_module/tests/agent/test_memory_arms.py`

**Interfaces:**
- Consumes: T0 の `PhaseSpec.sql_probe`、`ConsolidationRecord.notes_files_read` / `sql_queries`、`ExperimentConfig.session_notes` / `consolidator_sql`。`agent/session.py` の `QueryTool` と `CombinedToolSet`（import のみ）。
- Produces:
  - `_MemoryArm.phases(self, ctx: ArmContext) -> list[PhaseSpec]` に変える（旧: `budget` を受け取る）。設定を見て、プロンプト断片と `sql_probe` を決める。`test_consolidation.py:321-425` の呼び出しも合わせて直す。
  - `RollingSummaryArm`:
    - `session_notes` が真のとき、`online_tools` の書き込み先を `notes/` に限って許可する。
    - review フェーズの `prompt_files` の末尾に、`notes_handling.md` と `rolling_review_notes.md` を足す。
    - `rolling_review_notes.md` の内容: `notes/` の各メモを読み、裏付けた内容を `summary.md` に取り込み、そのメモの内容を 1 行 `MERGED INTO summary.md` に置き換えること。`rolling_review.md` の「two files」の記述は、この断片で上書きする。
    - 偽のときは前回と同じ（`notes_prefix = None`。既存テスト `test_memory_arms.py:252` は維持する）。
  - `VerifiedDreamingArm`:
    - `session_notes` が真のとき、analyzer の末尾に `notes_handling.md` を足す。
    - `consolidator_sql` が真のとき、analyzer を `sql_probe=True` にし、`consolidator_sql_addendum.md` を足す。
  - `consolidation.consolidator_query_tool(env: TaskEnv, session: int)`（R1 で `agent/session.py` の `QueryTool` に上限と説明文を渡す形へまとめた。上限は 10 回）。実行経路は `QueryTool` と同じ `env.run_query(session, sql)` で、説明文と上限だけを替える。11 回目以降は実行せず、`ERROR:` の文字列を返す。
  - `run_consolidation`:
    - `sql_probe` のフェーズでは、tool 群を `CombinedToolSet([FileToolset(mounts), consolidator_query_tool(ctx.env, session)])` にする。
    - on_event で次を数える。
      - `notes_files_read`: `notes/` 配下の、退役していない（内容が `MOVED TO` / `MERGED INTO` の 1 行ではない）ファイルへの `read_file` のうち、`ERROR:` でなく、ファイルの本文を 1 行以上返したもの（範囲外の offset で `[lines 0-0 of N]` だけを返した結果は数えない）。
      - `sql_queries`: 実行されて `ERROR:` でない結果を返した `query`。
  - `_consolidation_core.md` は変更しない。A1 と A4 の共通性を検査する既存テスト（`test_consolidation.py:318`、`:372-400`）は、既定設定で引き続き通ること。

- [x] **Step 1: 失敗するテストを書く**

```python
def test_a1_gets_notes_write_only_with_session_notes(): ...
def test_a1_review_has_notes_fragments_only_with_session_notes(): ...
def test_a4_analyzer_gets_query_tool_only_with_consolidator_sql(): ...
def test_consolidator_query_uses_finished_session_epoch(): ...  # session=4 の統合はエポック 1 の DB を照会する
def test_consolidator_query_limit(): ...                       # 11 回目は ERROR の文字列
def test_record_counts_live_notes_reads_and_executed_queries(): ...  # 退役メモとエラー結果は数えない
def test_default_config_phases_unchanged(): ...                # 既定設定の A1/A4 の PhaseSpec が変更前と同じ
def test_core_prompt_untouched(): ...                          # _consolidation_core.md の SHA-256 が変更前と同じ
```

- [x] **Step 2: 失敗を確認する** — Run: `uv run pytest project_module/tests/test_consolidation.py project_module/tests/agent/test_memory_arms.py -q`
- [x] **Step 3: 上記 Interfaces を実装する** — 3 つの断片の文面は、00_design §4.2・§4.5 の要件を満たす英文で書く。
- [x] **Step 4: テストが通ることを確認する**（同じコマンド）/ Expected: PASS

---

### Task 5: 追試の解析と gate（Opus high）

**Files:**
- Create: `project_module/src/project_module/evaluation/followup.py`, `evaluation/exploration.py`, `evaluation/followup_gate.py`, `scripts/analyze_followup.py`
- Test: `project_module/tests/evaluation/test_followup.py`, `test_exploration.py`, `test_followup_gate.py`

**Interfaces:**
- Consumes: 既存の `load_run`、`RunData`、`RunLayout`、`per_stream_score`、`paired_bootstrap`、`CI`、`Stream`。T0 の結果・記録フィールド。
- Produces（`evaluation/followup.py`）:
  - `Condition = tuple[str, str]`（run ラベル, 群）
  - `def condition_scores(runs: Mapping[str, RunData], cond: Condition, streams: Sequence[str], *, where: Callable[[QuestionResult], bool] | None = None) -> dict[str, float]`
  - `def common_streams(runs: Mapping[str, RunData], conds: Sequence[Condition]) -> list[str]`（全条件が完走したストリーム。事前登録 §5）
  - `def exclusions(runs: Mapping[str, RunData], conds: Sequence[Condition]) -> dict[str, dict[str, int]]`（除外理由ごと・条件ごとの件数）
  - `def contrast(runs, treat: Condition, ctrl: Condition, streams, *, where=None) -> CI`
  - `def interaction(runs, a1: Condition, a0: Condition, b1: Condition, b0: Condition, streams, *, where=None) -> CI`（ストリームごとの (a1 − a0) − (b1 − b0)）
  - `def fixed_sequence(results: Sequence[tuple[str, CI]], *, n_streams: int, required_streams: int = 40) -> list[dict[str, Any]]`
    - 各仮説の `verdict`（`supported` / `not_shown` / `reversed`）と `confirmatory: bool` を返す。
    - 最初に支持されなかった仮説より後ろは、`confirmatory=False` にする。`n_streams < required_streams` なら、すべて `confirmatory=False` にする（事前登録 §2 の確証的判定の前提）。
  - `def summarize_followup(run_dirs: Mapping[str, Path], *, ledger: Path, streams_root: Path, clbench_dirs: Sequence[Path]) -> dict[str, Any]`
    - 事前登録 §2〜§5 の全指標を出す（bootstrap の seed は 20261111）。欠測の感度分析も含める。
    - drift 種別の層別は、事前登録 §3 の定義に従い、各ストリームの `stream.json` から作る。
    - transcript とスナップショットは `RunLayout(run_dir)` で読む。
    - CL-Bench は、前回の run と追試の run を合わせて扱う（ストリーム ID は重ならない）。
- Produces（`evaluation/exploration.py`）:
  - `class QuestionFeatures(BaseModel)`: `qid: str`, `queries: int`, `unfiltered_ref_query: bool`, `schema_inspection: bool`
  - `def question_features(transcript: str, *, ref_table: str) -> list[QuestionFeatures]`
    - `USER:` 行で問題を区切る。`TOOL CALL query` と、その直後の `TOOL RESULT (query)` を対応づける。
    - 成功した照会だけを、事前登録 §4 の定義で判定する。
  - `def reference_table(stream_root: Path) -> str`（エポック 1 の DB に実在する参照表の名前）
  - `def count_change_records(text: str) -> int`、`def count_void_policy_lines(text: str, *, void_columns: Sequence[str]) -> int`、`def void_columns(stream: Stream) -> list[str]`（事前登録 §4 の正規表現・否定語・列名の取り出し方そのまま）
- Produces（`evaluation/followup_gate.py`）:
  - `def evaluate_followup_gate(run_dirs: Mapping[str, Path], *, ledger: Path, validation_problems: Mapping[str, list[str]], main_jobs: Mapping[str, Mapping[str, int]], phase_caps: Mapping[str, float]) -> dict[str, Any]`
    - `run_dirs` のキーは pilot の run ラベル（`fb0`、`fb1`、`verify`、`probe`、`null`）。
    - `main_jobs` は、phase ごと・条件ごとの本番ジョブ数。
    - 出力は、G0〜G7（00_design §7）の各値と、`passed`。
  - 閾値は定数として持つ: `NOTE_RATE_MIN = 0.90`、`NOTES_READ_RATE_MIN = 0.90`、`RETRIEVAL_RATE_MIN = 0.95`、`NO_SUBMISSION_MAX = 0.10`、`SQL_PROBE_RATE_MIN = 0.50`、`COST_SAFETY = 1.3`。
- Produces（`scripts/analyze_followup.py`）:
  - `--run label=dir`（複数）、`--clbench dir`（複数）、`--streams data/streams`、`--ledger`、`--out` を受け付ける。
  - `--gate` のときは、`--run` を pilot の run として扱う。本番の設定ファイルからジョブ数を数え、`followup-gate.json` を出す。不合格なら終了コード 1。
  - それ以外のときは `followup-metrics.json` を出す。

- [x] **Step 1: 失敗するテストを書く**（`tests/evaluation/builders.py` の既存ビルダーで RunData を組む）

```python
def test_common_streams_drops_stream_missing_any_condition(): ...
def test_exclusions_count_by_reason_and_condition(): ...
def test_contrast_pairs_by_stream_across_runs(): ...           # 2 run、3 ストリームで手計算の差と一致
def test_interaction_is_difference_of_differences(): ...
def test_fixed_sequence_stops_confirmatory_after_first_failure():
    out = fixed_sequence([("H1", CI(0.03, 0.01, 0.05)), ("H2", CI(0.0, -0.02, 0.02)), ("H3", CI(0.05, 0.02, 0.08))], n_streams=40)
    assert [r["confirmatory"] for r in out] == [True, True, False]
    assert [r["verdict"] for r in out] == ["supported", "not_shown", "supported"]
def test_fixed_sequence_with_missing_streams_is_exploratory(): ...  # n_streams=39 → すべて False
def test_unfiltered_ref_query_definition():
    # "select * from lkp_values" → True
    # "select * from lkp_values where lkp_type='x'"、"... limit 5"、"select count(*) from lkp_values"、"pragma table_info(lkp_values)"、ERROR を返した照会 → False
    ...
def test_schema_inspection_definition(): ...                   # "pragma table_info(x)" と "select name from sqlite_master" → True、"select 'pragma'" → False
def test_change_records_skip_negated_lines(): ...              # "status was 3 in s1-s4" → 1、"No schema changes since s1" → 0、"CONFLICT: ..." → 1
def test_void_policy_lines_need_column_and_negation(): ...     # 列名は soft_delete Fact の value（"ord_hdr.void_flg = 1" → "void_flg"）から取る
def test_drift_kind_matches_drift_dependent(): ...            # 既存の生成器のストリームで、drift 種別を持つ問題の集合が drift_dependent の集合と一致する
def test_gate_fails_on_low_note_rate(): ...                    # A1 の note_written 0.85 → G3 不合格
def test_gate_requires_no_feedback_in_fb0(): ...
def test_gate_passes_on_all_mechanical_conditions(): ...
```

- [x] **Step 2: 失敗を確認する** — Run: `uv run pytest project_module/tests/evaluation -q`
- [x] **Step 3: 上記 Interfaces を実装する**
- [x] **Step 4: テストが通ることを確認する**（同じコマンド）/ Expected: PASS

---

### Task 6: 追試の設定ファイル・E2E（Sonnet xhigh、波 1 の後）

**Files:**
- Create（すべて `configs/experiments/followup/` の下）:
  - `fu_dev.yaml`, `fu_dev_null.yaml`
  - `fu_pilot_fb0.yaml`, `fu_pilot_fb1.yaml`, `fu_pilot_verify.yaml`, `fu_pilot_probe.yaml`, `fu_pilot_null.yaml`
  - `fu_pilot_rev_fb0.yaml`, `fu_pilot_rev_fb1.yaml`, `fu_pilot_rev_verify.yaml`, `fu_pilot_rev_probe.yaml`, `fu_pilot_rev_null.yaml`
  - `fu_fb0.yaml`, `fu_fb1.yaml`, `fu_verify.yaml`, `fu_probe.yaml`, `fu_null.yaml`, `fu_clbench.yaml`, `fu_sens.yaml`
- Modify: `project_module/tests/test_contracts.py`（`configs/experiments/**/*.yaml` を読むように glob を広げる）, `project_module/tests/test_e2e_fake.py`, `README.md`（追試の実行コマンドを数行）
- Test: `project_module/tests/test_e2e_fake.py`

**Interfaces:**
- Consumes: T0〜T5 のすべて
- Produces（設定値）:
  - 各 yaml の値は、00_design §5.1・§6・§7・§8 の表のとおりにする。
  - `fu_dev.yaml`:
    - 全スイッチを有効にする（`feedback: graded`、`session_notes: true`、`a2_retrieval: auto`、`agent_prompt_addendum: verify_memory`、`consolidator_sql: true`）。
    - 群は A0, A1, A2, A4, A5、ストリームは `fu-dev-01`〜`02`。
  - `fu_pilot_*.yaml` と `fu_pilot_rev_*.yaml`: 群と設定は、同じ名前の本番 run と同じにする。ストリームと phase だけを替える。
  - `*_null.yaml`: `drift: false`、群は A1, A4。
  - `fu_clbench.yaml`: 前回の `clbench.yaml` と同じ値にする。`phase: fu_clbench`、`stream_ids: clbench-r3`〜`r17`、`seeds: 3`〜`17`。
  - `budgets.phase_cap_usd` は 00_design §8 の値、`hard_cap_usd` は 200。
  - `concurrency` は、同時に始める run の合計が 24 になるように配る（`fu_fb0` 7、`fu_fb1` 7、`fu_verify` 2、`fu_probe` 3、`fu_null` 3、`fu_clbench` 2。R1 レビューで `fu_clbench` を含めた）。
- `scripts/generate_streams.py` と `scripts/run_experiment.py` は、新しい yaml のパスを渡すだけで動くこと。CLI の変更が要る場合だけ、最小限に直す。

- [x] **Step 1: 失敗するテストを書く**

```python
def test_followup_configs_load_and_match_design(): ...        # 全 yaml が読め、phase・群・ストリーム・スイッチが 00_design §6・§7 と一致する
def test_fb0_fb1_share_streams_and_differ_only_in_feedback(): ...
def test_pilot_configs_mirror_main_except_streams_and_phase(): ...
def test_e2e_fake_followup_pilot_gate(tmp_path): ...           # FakeLLM で pilot の 5 run を 1 ストリーム × 2 セッション回し、analyze_followup --gate が G0〜G7 の値を出す
def test_e2e_fake_no_drift_stream(tmp_path): ...
```

- [x] **Step 2: 失敗を確認し、実装し、通す** — Run: `uv run pytest project_module/tests/test_contracts.py project_module/tests/test_e2e_fake.py -q` / Expected: PASS
- [x] **Step 3: 全体を確認して commit する** — Run: `make test && make all-lint` / Expected: すべて成功し、lint は 0 件。`git commit -m "feat: follow-up configs and end-to-end check"`

---

### R1: 実装一式の最終レビュー

- [x] Opus xhigh サブエージェントと Codex gpt-6.1-sol xhigh に、`main..feature/followup-harness` の差分を並行でレビューさせる。対象範囲として、T0〜T6、00_design §4・§7、本計画の Review Focus を明示する。
- [x] 指摘を反映し、両者の APPROVE を得てから PR を作り、マージする。

---

## 実行（E1〜E6）

各段の結果は、`docs/followup/30_run-ledger.md` に日付見出しで時系列に記録する。

- [x] **E1 dev**（phase `fu_dev`）:
  - `fu_dev.yaml` と `fu_dev_null.yaml` のストリームを生成して実行する。
  - 確認するのは機械的な指標だけ（tool 呼び出しの成功率、G2〜G5・G7 の値、`no_submission` 率、費用）。群別の正答率は見ない。
  - 必要なら `note_turn.md` と 3 つの断片の文面を直す。実測 token を run ledger に残す。
- [x] **E2 本番ストリームの生成、pilot と gate**（phase `fu_pilot`）:
  - 本番ストリーム（`fu-01`〜`40`、`fu-null-01`〜`10`）を生成し、検証が通ることを確かめる（G1。API 費用なし）。
  - `fu_pilot_*.yaml` の 5 run のストリームを生成して、同時に実行する。
  - `scripts/analyze_followup.py --gate` で `followup-gate.json` を出し、`docs/followup/artifacts/` に複製する。
  - **不合格なら**:
    - phase `fu_pilot_rev` で 1 回だけ直し、`fu_pilot_rev_*.yaml`（新しい seed）で、もう一度 gate を確かめる。
    - それでも不合格なら E3〜E5 を行わず、E6 で「処置の構築まで」のレポートを書いて閉じる。
- [x] **E3 凍結と本番**（phase `fu_main`・`fu_aux`）:
  - `20_preregistration.md` §6 を埋めて commit し、凍結する。
  - `fu_fb0`・`fu_fb1`・`fu_verify`・`fu_probe`・`fu_null` を同時に開始する。中断したジョブは `--resume` で完走させる。
- [x] **E4 CL-Bench**（phase `fu_clbench`）: E3 と並行で `fu_clbench.yaml` を実行する（15 反復）。
- [x] **E5 感度分析**（任意）:
  - `fu_aux` の必須 run が終わった後、`fu_aux` の残りが $8 以上あれば `fu_sens.yaml` を実行する。
  - 満たさなければ実行せず、その旨を run ledger に書く。
- [x] **E6 解析・レポート・Notion**:
  - `scripts/analyze_followup.py` で `followup-metrics.json` を出し、`docs/followup/artifacts/` に複製する。
  - `docs/followup/40_results.md`（日本語）を書く。内容は、主仮説の判定、副次・探索的指標、除外と欠測の感度分析、逸脱の有無、費用、示せたこと / 示せていないこと。
  - 既存の Notion ページ（Dreaming: Trying out Dreaming）に、追試の節を追記する。
- [x] **R2**:
  - Opus xhigh と Codex gpt-6.1-sol xhigh に、`40_results.md`・artifacts・run ledger をまとめてレビューさせる。
  - APPROVE を得てから PR を作ってマージし、不要な worktree を削除する。
