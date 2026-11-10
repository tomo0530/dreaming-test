"""Hybrid retrieval over raw session transcripts (arm A2).

BM25 (SQLite FTS5) catches exact identifiers such as column names; cosine similarity over
embeddings catches paraphrases. The two rankings are fused with reciprocal rank fusion.
"""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

from project_module.contracts import CostTag, LLMClient, ToolCall, ToolSet
from project_module.llm.ledger import BudgetExceeded
from project_module.tokens import count_tokens

logger = logging.getLogger(__name__)

FloatMatrix = npt.NDArray[np.float32]

_CHUNK_TOKENS = 400
_EMBED_BATCH_SIZE = 64
_CANDIDATES_PER_RANKER = 20
# Upper bound on how many characters can hold `_CHUNK_TOKENS` tokens in typical text; it only
# bounds the window searched when cutting an over-long line.
_WINDOW_CHARS_PER_TOKEN = 4

_BLOCK_START = re.compile(r"USER:|AGENT:|TOOL CALL |TOOL RESULT \(|EVENT ")
_HEADER_LINE = re.compile(r"SESSION |NOTE ABOUT THIS FILE:")
_QUERY_TOKEN = re.compile(r"[A-Za-z0-9_]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    chunk_id TEXT NOT NULL UNIQUE,
    session INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_session ON chunks(session);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text, content='chunks', content_rowid='id');
"""

SEARCH_TOOL_NAME = "search_transcripts"


def rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Fuse ranked lists of unique ids by reciprocal rank fusion.

    Ties keep the order in which an id first appears across the rankings.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: scores[item], reverse=True)


class Chunk(BaseModel):
    chunk_id: str
    session: int
    text: str


def _blocks(text: str) -> list[str]:
    """Split a labeled transcript into blocks that each start at a `USER:` / `AGENT:` /
    `TOOL CALL` / `TOOL RESULT` / `EVENT` line. File header lines carry no content and are
    dropped."""
    grouped: list[list[str]] = []
    for line in text.split("\n"):
        if _HEADER_LINE.match(line):
            continue
        if _BLOCK_START.match(line):
            grouped.append([line])
        elif grouped:
            grouped[-1].append(line)
        elif line.strip():
            grouped.append([line])
    return [block for lines in grouped if (block := "\n".join(lines).rstrip())]


def _fitting_prefix_length(text: str) -> int:
    upper = min(len(text), _CHUNK_TOKENS * _WINDOW_CHARS_PER_TOKEN)
    if count_tokens(text[:upper]) <= _CHUNK_TOKENS:
        return upper
    # Invariant: a prefix of `lower` characters fits and one of `upper` characters does not.
    lower = 0
    while upper - lower > 1:
        middle = (lower + upper) // 2
        if count_tokens(text[:middle]) <= _CHUNK_TOKENS:
            lower = middle
        else:
            upper = middle
    return lower


def _split_line(line: str) -> list[str]:
    pieces: list[str] = []
    rest = line
    while rest:
        cut = _fitting_prefix_length(rest)
        pieces.append(rest[:cut])
        rest = rest[cut:]
    return pieces


def _pack(parts: list[str], split_oversized: Callable[[str], list[str]]) -> list[str]:
    """Join consecutive parts with newlines into chunks of at most `_CHUNK_TOKENS` tokens.

    A part that is too large on its own is replaced by `split_oversized(part)`, whose pieces
    become chunks of their own so neighbouring parts are never merged into them.
    """
    chunks: list[str] = []
    current: str | None = None
    for part in parts:
        if count_tokens(part) > _CHUNK_TOKENS:
            if current is not None:
                chunks.append(current)
                current = None
            chunks.extend(split_oversized(part))
        elif current is None:
            current = part
        elif count_tokens(joined := f"{current}\n{part}") <= _CHUNK_TOKENS:
            current = joined
        else:
            chunks.append(current)
            current = part
    if current is not None:
        chunks.append(current)
    return chunks


def _split_block(block: str) -> list[str]:
    return _pack(block.split("\n"), _split_line)


def _chunk_transcript(text: str) -> list[str]:
    return [chunk for chunk in _pack(_blocks(text), _split_block) if chunk.strip()]


def _fts_query(query: str) -> str | None:
    """Quote every alphanumeric/underscore token so FTS5 never sees operators or syntax."""
    tokens = [t for t in dict.fromkeys(_QUERY_TOKEN.findall(query)) if t.strip("_")]
    return " OR ".join(f'"{token}"' for token in tokens) or None


def _normalize(vectors: FloatMatrix) -> FloatMatrix:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)


class HybridIndex:
    """Transcript chunks searchable by BM25 and by embedding similarity.

    State lives in three plain files under `root` (`index.sqlite`, `embeddings.npy`,
    `ids.json`), so copying the directory snapshots the index. Existing files are loaded.
    """

    def __init__(self, root: Path, client: LLMClient, embedding_model: str) -> None:
        self._client = client
        self._embedding_model = embedding_model
        self._db_path = root / "index.sqlite"
        self._ids_path = root / "ids.json"
        self._embeddings_path = root / "embeddings.npy"
        root.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
        self._ids, self._matrix = self._load()

    def add_transcript(self, session: int, text: str, *, tag: CostTag) -> int:
        """Chunk a transcript, embed the chunks, and add them to the index.

        Chunks hold at most 400 tokens and break at block boundaries; a block that is too large
        is cut at line boundaries, and a line that is too large inside the line. Nothing is
        written unless every embedding request succeeds.

        Returns
        -------
        int
            The number of chunks added.

        Raises
        ------
        ValueError
            If `session` is already indexed.
        """
        if self._has_session(session):
            raise ValueError(f"session {session} is already indexed")
        texts = _chunk_transcript(text)
        if not texts:
            return 0
        vectors = self._embed(texts, tag)
        chunk_ids = [f"s{session}-c{seq:04d}" for seq in range(len(texts))]
        ids = [*self._ids, *chunk_ids]
        matrix = vectors if self._matrix is None else np.vstack([self._matrix, vectors])
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO chunks(chunk_id, session, text) VALUES (?, ?, ?)",
                [(cid, session, chunk) for cid, chunk in zip(chunk_ids, texts, strict=True)],
            )
            conn.execute(
                "INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks WHERE session = ?",
                (session,),
            )
            self._write_files(ids, matrix)
        self._ids, self._matrix = ids, matrix
        logger.info("indexed session %d: %d chunks", session, len(texts))
        return len(texts)

    def search(self, query: str, *, k: int = 8, token_budget: int, tag: CostTag) -> list[Chunk]:
        """Return up to `k` chunks ranked by BM25 and cosine similarity fused with RRF.

        Chunks are taken from the top of the ranking while their cumulative token count stays
        within `token_budget`; the first chunk that does not fit ends the result.
        """
        matrix = self._matrix
        if matrix is None or token_budget <= 0 or not query.strip():
            return []
        fused = rrf([self._bm25_ranking(query), self._cosine_ranking(matrix, query, tag)])
        return self._take_within_budget(fused[:k], token_budget)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _load(self) -> tuple[list[str], FloatMatrix | None]:
        ids: list[str] = (
            json.loads(self._ids_path.read_text(encoding="utf-8"))
            if self._ids_path.exists()
            else []
        )
        matrix: FloatMatrix | None = (
            np.asarray(np.load(self._embeddings_path), dtype=np.float32)
            if self._embeddings_path.exists()
            else None
        )
        embedded = 0 if matrix is None else matrix.shape[0]
        with closing(self._connect()) as conn:
            stored: int = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if not len(ids) == embedded == stored:
            raise ValueError(
                f"index files are inconsistent: {len(ids)} ids, {embedded} embeddings, "
                f"{stored} stored chunks"
            )
        return ids, matrix

    def _write_files(self, ids: list[str], matrix: FloatMatrix) -> None:
        ids_tmp = self._ids_path.with_name(self._ids_path.name + ".tmp")
        embeddings_tmp = self._embeddings_path.with_name(self._embeddings_path.name + ".tmp")
        ids_tmp.write_text(json.dumps(ids), encoding="utf-8")
        with embeddings_tmp.open("wb") as f:
            np.save(f, matrix)
        ids_tmp.replace(self._ids_path)
        embeddings_tmp.replace(self._embeddings_path)

    def _has_session(self, session: int) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT 1 FROM chunks WHERE session = ? LIMIT 1", (session,))
            return row.fetchone() is not None

    def _embed(self, texts: list[str], tag: CostTag) -> FloatMatrix:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            embedded = self._client.embed(model=self._embedding_model, texts=batch, tag=tag)
            if len(embedded) != len(batch):
                raise ValueError(f"expected {len(batch)} embeddings, got {len(embedded)}")
            vectors.extend(embedded)
        return _normalize(np.asarray(vectors, dtype=np.float32))

    def _bm25_ranking(self, query: str) -> list[str]:
        match = _fts_query(query)
        if match is None:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT c.chunk_id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts), c.id LIMIT ?",
                (match, _CANDIDATES_PER_RANKER),
            ).fetchall()
        return [row[0] for row in rows]

    def _cosine_ranking(self, matrix: FloatMatrix, query: str, tag: CostTag) -> list[str]:
        scores = matrix @ self._embed([query], tag)[0]
        order = np.argsort(-scores, kind="stable")[:_CANDIDATES_PER_RANKER]
        return [self._ids[i] for i in order]

    def _take_within_budget(self, chunk_ids: list[str], token_budget: int) -> list[Chunk]:
        chunks: list[Chunk] = []
        used = 0
        with closing(self._connect()) as conn:
            for chunk_id in chunk_ids:
                row = conn.execute(
                    "SELECT chunk_id, session, text FROM chunks WHERE chunk_id = ?", (chunk_id,)
                ).fetchone()
                chunk = Chunk(chunk_id=row[0], session=row[1], text=row[2])
                used += count_tokens(chunk.text)
                if used > token_budget:
                    break
                chunks.append(chunk)
        return chunks


def _search_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SEARCH_TOOL_NAME,
            "description": (
                "Search the raw logs of your earlier sessions (questions, your replies, SQL "
                "queries and their results) for passages relevant to a query. Keyword and "
                "semantic matching are combined; exact identifiers such as table or column "
                "names work well as keywords. Each passage is labelled with its session. "
                "The text you can read through this tool is limited per question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords, identifiers or a short natural-language question.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


class TranscriptSearchTools(ToolSet):
    """`search_transcripts` over a `HybridIndex`, with a cumulative read budget.

    Build a new instance for every question so the budget starts full. The budget counts the
    tokens of the passage text returned; each call only returns passages that fit what is left.
    """

    def __init__(self, index: HybridIndex, *, token_budget: int, tag: CostTag) -> None:
        self._index = index
        self._token_budget = token_budget
        self._tag = tag
        self._used = 0

    def schemas(self) -> list[dict[str, Any]]:
        return [_search_tool_schema()]

    def dispatch(self, call: ToolCall) -> str:
        if call.name != SEARCH_TOOL_NAME:
            return f"ERROR: unknown tool {call.name!r}."
        if "__invalid__" in call.arguments:
            return "ERROR: the tool arguments are not valid JSON."
        query = call.arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return "ERROR: 'query' must be a non-empty string."
        remaining = self._token_budget - self._used
        if remaining <= 0:
            return "ERROR: the transcript read budget for this question is exhausted."
        try:
            chunks = self._index.search(query, token_budget=remaining, tag=self._tag)
        except BudgetExceeded:
            # Budget exhaustion is control flow for the runner, not a tool failure.
            raise
        except Exception as exc:  # other failures reach the model as text
            logger.warning("search_transcripts failed: %s", type(exc).__name__)
            return f"ERROR: transcript search failed ({type(exc).__name__})."
        self._used += sum(count_tokens(chunk.text) for chunk in chunks)
        if not chunks:
            return (
                "No matching passages found within the remaining read budget "
                f"of {remaining} tokens."
            )
        return "\n\n".join(f"[session {chunk.session}]\n{chunk.text}" for chunk in chunks)
