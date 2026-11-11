<!-- Follow-up study fragment, used only with `session_notes`: appended to the A1 summary reviewer after notes_handling.md. -->
## Notes in the memory

This changes the memory layout above: besides the two files, the memory holds `memory/notes/`, where the agent writes one note per question with `memory_write`, each in a file of its own. It is the only place the agent can write. A note whose whole content is the single line `MERGED INTO summary.md` has been merged already: leave it as it is. `grep` for the literal text `MERGED INTO` with `glob` set to `memory/notes/*.md` lists those notes.

Handle the live notes as part of your steps:
- In your first turn, also glob `memory/notes/*.md` and grep for `MERGED INTO` as above. In your next turn, read every live note in full, together with the rest of the transcript.
- Verify every statement of a live note against the evidence, as you verify `summary.md`.
- Merge every statement of a live note into `summary.md` as the notes rule above says: one the evidence settles with its `(verified sN)` tag, one the evidence contradicts only in its corrected form, and one that no evidence settles tagged `(unverified)`. Put each as one bullet line in the section for its topic, in the same `edit_file` calls as your other changes to that section; a statement the summary already holds needs only its tag updated, and a `(verified sN)` tag is never downgraded to `(unverified)`. When `summary.md` does not exist yet, put these lines into the one you create.
- Then retire every live note you read: replace its whole content with the single line `MERGED INTO summary.md` using `write_file`, even when it held nothing to merge. Do not delete a note, and do not retire one you have not read.

In your final paragraph, also say how many notes you merged and retired.
