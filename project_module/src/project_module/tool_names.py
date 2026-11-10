"""Names of the memory and retrieval tools, shared by the tool sets and the transcript writer.

This module imports nothing from the package, so any module can depend on it without cycles.
"""

from typing import Final

MEMORY_READ_TOOL: Final = "memory_read"
MEMORY_SEARCH_TOOL: Final = "memory_search"
MEMORY_WRITE_TOOL: Final = "memory_write"
SEARCH_TRANSCRIPTS_TOOL: Final = "search_transcripts"

# Tools whose results are retrieved memory or earlier transcripts. A transcript must not record
# their bodies: they would be re-read as new evidence, and A2 would re-index its own retrievals.
RETRIEVAL_CONTENT_TOOLS: Final = frozenset(
    {MEMORY_READ_TOOL, MEMORY_SEARCH_TOOL, SEARCH_TRANSCRIPTS_TOOL}
)
