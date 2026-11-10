"""Mechanical checks of a memory directory, used as the quality signal for consolidation."""

import re
from pathlib import PurePosixPath
from typing import Literal

import yaml

from project_module.memory.store import MemoryStore
from project_module.tokens import count_tokens

_INDEX = "_index.md"
_INJECTED = "injected.md"
_SUMMARY = "summary.md"
# Index and injection layer are navigation aids, not wiki pages.
_SPECIAL_PAGES = frozenset({_INDEX, _INJECTED})

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*$\n?", re.DOTALL | re.MULTILINE)
_LINK = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_STUB_MARK = "MOVED TO"


def lint_store(
    store: MemoryStore, *, profile: Literal["wiki", "summary"], injected_token_limit: int
) -> list[str]:
    """Return the problems found in `store`; an empty list means it is healthy.

    ``wiki`` expects frontmatter (`name` equal to the file stem, a `description`) on every page
    except `_index.md`, `injected.md` and `MOVED TO` stubs, `[[slug]]` links that resolve to a
    page of that stem, and an `_index.md` that lists every page except stubs and never lists a
    stub. A page is listed when the index contains its `[[slug]]` or its relative path.
    ``summary`` expects `summary.md`. Both require `injected.md`, if present, to fit the limit.
    """
    match profile:
        case "wiki":
            problems = _lint_wiki(store)
        case "summary":
            problems = _lint_summary(store)
        case _:
            raise ValueError(f"unknown lint profile: {profile!r}")
    problems.extend(_lint_injected(store, injected_token_limit))
    return problems


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    match = _FRONTMATTER.match(text)
    if match is None:
        return None, text
    return match.group(1), text[match.end() :]


def _is_stub(text: str) -> bool:
    _, body = _split_frontmatter(text)
    return body.lstrip().startswith(_STUB_MARK)


def _lint_summary(store: MemoryStore) -> list[str]:
    return [] if store.exists(_SUMMARY) else [f"{_SUMMARY} is missing"]


def _lint_injected(store: MemoryStore, limit: int) -> list[str]:
    if not store.exists(_INJECTED):
        return []
    tokens = count_tokens(store.read_all(_INJECTED))
    if tokens <= limit:
        return []
    return [f"{_INJECTED} has {tokens} tokens, over the limit of {limit}"]


def _lint_wiki(store: MemoryStore) -> list[str]:
    pages = {rel: store.read_all(rel) for rel in store.glob("**/*.md")}
    stubs = {rel for rel, text in pages.items() if _is_stub(text)}
    stems = {PurePosixPath(rel).stem for rel in pages}
    problems: list[str] = []
    for rel, text in pages.items():
        if rel not in _SPECIAL_PAGES and rel not in stubs:
            problems.extend(_frontmatter_problems(rel, text))
        problems.extend(_link_problems(rel, text, stems))
    problems.extend(_index_problems(pages, stubs))
    return problems


def _frontmatter_problems(rel: str, text: str) -> list[str]:
    raw, _ = _split_frontmatter(text)
    if raw is None:
        return [f"{rel}: missing frontmatter"]
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError:
        return [f"{rel}: frontmatter is not valid YAML"]
    if not isinstance(meta, dict):
        return [f"{rel}: frontmatter must be a mapping with name and description"]
    problems: list[str] = []
    stem = PurePosixPath(rel).stem
    if meta.get("name") != stem:
        problems.append(f"{rel}: frontmatter name {meta.get('name')!r} does not match '{stem}'")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append(f"{rel}: frontmatter description is missing")
    return problems


def _link_problems(rel: str, text: str, stems: set[str]) -> list[str]:
    slugs = dict.fromkeys(slug.strip() for slug in _LINK.findall(text))
    return [f"{rel}: broken link [[{slug}]]" for slug in slugs if slug not in stems]


def _index_problems(pages: dict[str, str], stubs: set[str]) -> list[str]:
    index = pages.get(_INDEX)
    if index is None:
        return [f"{_INDEX} is missing"]
    problems: list[str] = []
    for rel in sorted(pages):
        if rel in _SPECIAL_PAGES:
            continue
        listed = f"[[{PurePosixPath(rel).stem}]]" in index or rel in index
        if rel in stubs and listed:
            problems.append(f"{_INDEX} lists the retired stub {rel}")
        elif rel not in stubs and not listed:
            problems.append(f"{_INDEX} does not list {rel}")
    return problems
