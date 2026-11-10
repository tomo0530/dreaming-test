# Diagnosis: main run main-20261110T161346Z (read-only)

> Post-hoc exploratory diagnosis (not preregistered). The scripts were one-off and are not in the repository. Corrections from the R2 review: (1) the example at line 47 mixes two questions. The quoted A4 query belongs to s09q1; on s09q3 ("orders since April") A4 read no code table at all. Across the 113 soft-delete questions, A4 read the code table as none 85 / filtered 13 / full 15, and the ".32 (n=85)" figure is the "none" group. (2) "14/130" at line 101 is 14/110 at s12 on recomputation. (3) Correlations here are observational; they do not establish causation.

Scripts / raw outputs (scratchpad): build.py -> rows.jsonl (8640 question rows = 20 streams x 6 arms x 72),
agg.py -> agg.txt, softdelete.py -> softdelete.txt / sd_samples.txt, decomp.py -> decomp.txt,
codetable.py -> codetable.txt, table_samples.txt.
Coverage: automated features over the full census of stale errors (A0 86, A1 130, A2 95, A3 102, A4 143, A5 12)
and all drift questions; manual reading of ~14 soft-delete failures and 9 stale-table failures (A0/A1/A4).

Memory-at-session-s = `snapshots/session_{s-1}/memory/injected.md` (snapshot taken after consolidation and
false-note injection of s-1; runner.py `_run_one_session`, session.py `system_prompt`). Verified alignment:
0 transcript/question text mismatches.

## Drift design recap (from data/streams/main-XX/stream.json)
Epochs: s1-4, s5-8, s9-12. Each stream has 2 drift kinds at s5 and 2 at s9 drawn from
{status_code, stale_table, join_key, column_name, soft_delete, unit}. Loud drifts (column/join rename -> "no such
column") are handled by all arms (0-1 stale). Silent drifts are where errors happen:
- stale_table: old table stays in the DB with frozen data; a new table holds current rows.
- soft_delete: new void column + new code set (e.g. ORDER_VOID_FLAG "Voided order - exclude from all reporting").
- unit: amount switches cents<->dollars.

## Q1. Why A1/A4 < A0

Stale errors by drift kind changed at that epoch (question-level, multi-kind counted; n questions per arm in brackets):

| kind | A0 | A1 | A2 | A3 | A4 | A5 |
|---|---|---|---|---|---|---|
| softdelete [113] | 8 | 32 | 5 | 8 | 45 | 6 |
| stale_table [119] | 50 | 69 | 56 | 64 | 57 | 6 |
| unit [132] | 28 | 22 | 34 | 30 | 39 | 0 |
| status [192] | 0 | 6 | 0 | 0 | 2 | 0 |

Accuracy on soft-delete questions: A0 .91, A2 .92, A3 .88, A5 .89 vs **A1 .44, A4 .40**.
Correct-count gap vs A0 (A4 total 88 = -2 e1, +25 e2, +65 e3): softdelete **+58 (66%)**, unit +18, table +5,
column +2. A1 total 72: softdelete **+53 (74%)**, table +15, column +8, status +7, unit -13.

Hypothesis check ("skips schema inspection, trusts stale memory fact"):
- Schema inspection is NOT skipped: sqlite_master/pragma queried in 95-100% of questions in every arm
  (A1 stale 0.95, A4 stale 0.99).
- What is skipped is value-level exploration. Mean queries/question: A0 5.2-5.7, A2 5.2-5.8, A3 5.0-5.7 vs
  **A1 3.3-3.6, A4 3.5-3.8**. Full scan of the code table (lkp_values/ref_codes/code_lookup without a code_set
  filter): A0 84-90%, A2 85-91%, A3 80-85% vs **A4 41% -> 25% -> 22%** (epoch 1/2/3), **A1 30% -> 19% -> 15%**.
  Memory tells the agent the code-set name, so it queries `WHERE code_set='ORDER_STATUS'` and never sees the new
  ORDER_VOID_FLAG set. Soft-delete accuracy given exploration: A4 full scan .80 (n=15) vs none .32 (n=85);
  A1 full 1.00 (n=8) vs none .37 (n=89); A0 full .94 (n=109).
  Example: main-01 s9 "orders since April" — A0 transcripts/session_009.txt:90 `SELECT * FROM lkp_values`,
  then filters void_flg; A4 transcripts/session_009.txt:22 `... WHERE code_set='ORDER_STATUS'`, submits
  without void filter (line 123).
- Even at the FIRST drift session (memory has no void info yet: stance "absent"), A1/A4 soft-delete accuracy
  is .48 vs A0 .92 -> primary cause is memory-induced narrow exploration, not a stale fact in memory.
- Consolidation DOES notice structural drift (A4 notes "void_flg absent from s8", "earlier mapping changed in
  s5"; A4 probe accuracy on drifted facts 45/50 at s12, softdelete 9/9), but it encodes a wrong *policy*
  derived from the agent's own ungraded past answers. A4 injected stance on the void column in post-drift
  sessions: no-exclude/unknown n=28 acc **.07**; mixed n=28 acc .46; exclude n=29 acc .62 (A1: .08/.42/.65).
  Examples:
  - main-01/A4/snapshots/session_009/memory/injected.md:7 "Do not add a `void_flg` exclusion by assumption:
    no required general filter was established." -> session_010 s10q6 stale (transcripts/session_010.txt:174
    `SELECT COUNT(*) FROM ord_hdr WHERE plc_ts >= '2025-03-01'`; A0 same question line 300 adds `void_flg = 0`).
  - main-15/A4/snapshots/session_010/memory/injected.md:13 "meaning is unknown. Do not infer that it excludes
    orders. Include all rows when asked simply how many orders were placed".
  - main-03/A1/snapshots/session_007/memory/injected.md:5 "For orders placed, do not exclude void rows unless
    instructed."
  - Even when memory says exclude (main-18 A4 s10q4), the agent omitted the filter with 3 queries.
- Stale tables: A1/A4 injected memory names the old table in 119/119 drift questions and flags it as
  stale/legacy in only 5/119 (A4) and 17/119 (A1); the new table is described as a "separate" table, e.g.
  main-01/A4/snapshots/session_009/memory/injected.md:9-10 (`shp_log` documented as the shipping table,
  "Separate `parcel_tracking` ... Do not conflate"); main-16/A4 session_005 injected.md:10 same pattern.
  Submitted SQL on old table: A0 59/119, A1 75, A3 72, A4 63, A5 8. This trap is hard for every arm (A0 acc .46);
  memory adds only modestly (A1 +15, A4 +5 correct vs A0).
- Unit: A4 .55 vs A0 .68 (A1 better, .78). Not dissected further.

Verdict: the "stale memory fact" story is secondary. Main mechanism = (a) memory makes the agent explore less
and in a targeted way (fewer queries, filtered code-table reads), so new silent semantics are missed;
(b) consolidation without outcome feedback canonises the agent's own unverified choices ("don't filter
void", "old table is the shipping table") as rules, which then lock in the error.

## Q2. Late-session decline (s9-12)
- Drift-specific, not general: epoch-3 non-drift questions A4 .86 vs A0 .89; epoch-3 drift questions
  A4 .61 / A1 .64 vs A0 .77 (epoch 2: .74/.75 vs .81).
- Drift timing: soft_delete occurs at s9 in 8 of the 12 streams that have it (s5 in 4: main-03/07/16/19). The
  soft-delete gap for A4 is +15 in e2 vs +43 in e3.
- No recovery after drift: A4 soft-delete accuracy by offset from drift +0/+1/+2/+3 = .48/.39/.32/.41
  (A1 .48/.39/.42/.48); A0 stays ~.90. Consolidation never corrects the policy.
- Exploration decays over the run (A4 full code-table scan 41% -> 22%; questions with no code-table read
  29% -> 44%), i.e. growing reliance on memory.
- Not memory size / truncation: injected.md mean stays ~750-810 tokens (A4) / ~1000 (A1), max 1829 < 2000
  budget; total memory grows 1.7k -> 5.4k tokens but only injected.md is in the prompt.

## Q3. Why A2 does not suffer
A2 never used RAG online: **0 `search_transcripts` calls in 1440 questions**; cost ledger has 0 online
embedding calls for A2 (240 indexing + 480 probe embedding calls only). A2 therefore behaves like A0
(5.2-5.8 queries, full code-table scan 85-91%), and its result (.835 vs .826) is A0 + noise, not evidence
that RAG resists drift. Same reason A3 explores like A0: its memory is nearly empty (see below).

## Q4. Harness / validity flags
1. [Validity, high] A2 tool never used online (above). A2 vs others does not test RAG.
2. [Validity, high] `memory_write` was called 0 times by the online agent in A3 and A4 (2880 questions),
   though the tool is offered (memory_arms.py `_WikiArm.notes_prefix`). A3 memory = only the two injected
   false notes, promoted to "Verified database rules" (e.g. main-05/A3/memory/injected.md); A3 probe accuracy
   14/130 on original facts. A3 = "A0 + false notes" (injected-target acc .74, 120/120 markers active).
   A4's notes layer is likewise empty; its wiki is built solely by the analyzer from transcripts, so A4 is
   structurally close to A1. The A3-vs-A4 contrast does not measure what the design intends.
3. [OK] No truncation of injected memory (max 1829 < 2000). Snapshot s-1 is what is injected at s.
   Transcript/result alignment OK.
4. [OK, design note] Grader stale logic looks consistent; 0 stale from loud drifts. For soft_delete,
   "stale" means "ignored the new void flag" — no pre-drift fact exists in memory for it. The gold
   (exclude voids from "how many orders placed") is only discoverable from the code-table text
   "exclude from all reporting"; arguably a semantic trap, but A0/A5 handle it (~.90).
5. [Unverified] no_submission is higher in memory arms (A1 29, A4 30, A5 32 vs A0 13, A2 11); maybe turns
   spent on memory tools under max_turns=8. Not investigated.
