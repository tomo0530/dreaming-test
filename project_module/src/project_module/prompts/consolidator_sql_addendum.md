<!-- Follow-up study fragment, used only with `consolidator_sql`: appended to the A4 analyzer. It extends the tools and the evidence rule of the shared core prompt, which stays unchanged. -->
## The query tool

This extends "Tools and paths" and "What counts as evidence" above. Besides the five file tools you hold a sixth, `query`: it runs one read-only SQL statement against the database as it is in the session that has just ended, and returns the result as text, or an error starting with `ERROR:`. You cannot see the database of any later session. A result is cut to $query_row_limit rows, so use aggregates, filters or LIMIT. You may run at most $consolidator_max_queries queries in this run; further calls are not run.

The results of your own `query` calls are evidence too, of the same kind as the `query` results in the transcript, and they hold for the session that has just ended. Tag a fact they support `(verified sN)` with this session's number, as for any other evidence, and record that it rests on your own query when no query in the transcript shows it.

Before you write, keep or confirm a policy or an exclusion rule (which rows a correct answer must leave out, what a code or flag value means, which codes belong to a set), query the current values it depends on: the full contents of the lookup or code table, and the distinct values of the flag column, without filtering by the codes that memory or the transcript names. Put several such queries in one turn, and do not re-run a query whose result the transcript already shows.
