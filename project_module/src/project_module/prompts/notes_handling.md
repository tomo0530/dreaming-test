<!-- Follow-up study fragment, used only with `session_notes`: appended alike to the A1 summary reviewer and the A4 analyzer. -->
## Notes the agent wrote after each question

After every question the agent writes one note under `memory/notes/` on what it found out about the database. A note is the agent's own account, written without knowing whether its answer was right: it is an unverified claim, never evidence, however specific or confident it sounds, and even where it says that a query confirmed it. Check every statement of a note against the `query` results and `EVENT feedback:` records in the transcripts:
- The evidence settles it: keep it with its verification tag, `(verified sN)` with the session of that evidence.
- The evidence contradicts it: it was wrong; correct it from the evidence and keep none of its wording.
- No evidence settles it: keep it tagged `(unverified)`, and never present it as verified.
