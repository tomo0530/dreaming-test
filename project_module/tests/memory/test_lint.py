from pathlib import Path

import pytest
from project_module.memory.lint import lint_store
from project_module.memory.store import MemoryStore
from project_module.tokens import count_tokens


def _page(name: str, description: str = "A page.", body: str = "Body.\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def _wiki(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory", writable=True)
    store.write("_index.md", "- [[orders]] - orders rules\n- [[units]] - unit rules\n")
    store.write("notes/orders.md", _page("orders", body="See [[units]].\n"))
    store.write("wiki/units.md", _page("units", body="Prices are in cents.\n"))
    store.write("injected.md", "Prices are in cents.\n")
    return store


def _lint(store: MemoryStore, *, limit: int = 100) -> list[str]:
    return lint_store(store, profile="wiki", injected_token_limit=limit)


def test_lint_wiki_accepts_a_clean_store(tmp_path: Path) -> None:
    assert _lint(_wiki(tmp_path)) == []


def test_lint_wiki_reports_broken_link_missing_index_entry_and_stub_in_index(
    tmp_path: Path,
) -> None:
    store = _wiki(tmp_path)
    store.write("notes/orders.md", _page("orders", body="See [[units]] and [[nowhere]].\n"))
    store.write("notes/extra.md", _page("extra"))
    store.write("archive/old.md", "MOVED TO [[units]]\n")
    store.write(
        "_index.md",
        "- [[orders]] - o\n- [[units]] - u\n- [[old]] - retired\n",
    )

    problems = _lint(store)

    assert any("notes/orders.md" in p and "[[nowhere]]" in p for p in problems)
    assert any("notes/extra.md" in p and "not list" in p for p in problems)
    assert any("archive/old.md" in p and "stub" in p for p in problems)
    assert len(problems) == 3


def test_lint_wiki_index_may_list_files_by_path(tmp_path: Path) -> None:
    store = _wiki(tmp_path)
    store.write("_index.md", "- notes/orders.md - orders\n- wiki/units.md - units\n")
    assert _lint(store) == []


def test_lint_wiki_stub_sharing_a_stem_with_a_live_page_is_judged_by_path(
    tmp_path: Path,
) -> None:
    store = _wiki(tmp_path)
    store.write("notes/units.md", "MOVED TO [[units]]\n")
    assert _lint(store) == []
    store.write("_index.md", "- [[orders]] - o\n- [[units]] - u\n- notes/units.md - old\n")
    problems = _lint(store)
    assert len(problems) == 1 and "retired stub notes/units.md" in problems[0]


def test_lint_wiki_stub_is_not_linted_for_frontmatter_but_its_target_must_exist(
    tmp_path: Path,
) -> None:
    store = _wiki(tmp_path)
    store.write("archive/old.md", "MOVED TO [[units]]\n")
    assert _lint(store) == []
    store.write("archive/old.md", "---\nname: old\ndescription: gone\n---\nMOVED TO [[ghost]]\n")
    problems = _lint(store)
    assert len(problems) == 1 and "[[ghost]]" in problems[0]


def test_lint_wiki_checks_frontmatter(tmp_path: Path) -> None:
    store = _wiki(tmp_path)
    store.write("notes/no-front.md", "Just text.\n")
    store.write("notes/bad-name.md", _page("other-name"))
    store.write("notes/no-desc.md", "---\nname: no-desc\n---\nBody.\n")
    store.write("notes/blank-desc.md", "---\nname: blank-desc\ndescription: '  '\n---\nBody.\n")
    store.write("notes/bad-yaml.md", "---\nname: bad-yaml\ndescription: a: b: c\n---\nBody.\n")
    store.write("notes/not-map.md", "---\n- a\n- b\n---\nBody.\n")
    store.write("notes/unclosed.md", "---\nname: unclosed\ndescription: x\nBody.\n")
    store.write(
        "_index.md",
        "".join(
            f"- [[{s}]]\n"
            for s in [
                "orders",
                "units",
                "no-front",
                "bad-name",
                "no-desc",
                "blank-desc",
                "bad-yaml",
                "not-map",
                "unclosed",
            ]
        ),
    )

    problems = _lint(store)
    by_file = {p.split(":")[0]: p for p in problems}

    assert set(by_file) == {
        "notes/no-front.md",
        "notes/bad-name.md",
        "notes/no-desc.md",
        "notes/blank-desc.md",
        "notes/bad-yaml.md",
        "notes/not-map.md",
        "notes/unclosed.md",
    }
    assert "missing frontmatter" in by_file["notes/no-front.md"]
    assert "other-name" in by_file["notes/bad-name.md"]
    assert "description" in by_file["notes/no-desc.md"]
    assert "description" in by_file["notes/blank-desc.md"]
    assert "YAML" in by_file["notes/bad-yaml.md"]
    assert "mapping" in by_file["notes/not-map.md"]
    assert "missing frontmatter" in by_file["notes/unclosed.md"]


def test_lint_wiki_reports_missing_index(tmp_path: Path) -> None:
    store = _wiki(tmp_path)
    (store._root / "_index.md").unlink()
    assert _lint(store) == ["_index.md is missing"]


def test_lint_wiki_does_not_require_frontmatter_on_special_files(tmp_path: Path) -> None:
    store = _wiki(tmp_path)
    store.write("injected.md", "plain text without frontmatter\n")
    assert _lint(store) == []


def test_lint_flags_injected_over_the_token_limit(tmp_path: Path) -> None:
    store = _wiki(tmp_path)
    text = "word " * 50
    store.write("injected.md", text)
    problems = _lint(store, limit=count_tokens(text) - 1)
    assert len(problems) == 1 and "injected.md" in problems[0]
    assert _lint(store, limit=count_tokens(text)) == []


def test_lint_summary_profile(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory", writable=True)
    assert lint_store(store, profile="summary", injected_token_limit=50) == [
        "summary.md is missing"
    ]
    store.write("summary.md", "free-form summary with no frontmatter and a [[dangling]] link\n")
    assert lint_store(store, profile="summary", injected_token_limit=50) == []
    store.write("injected.md", "word " * 100)
    problems = lint_store(store, profile="summary", injected_token_limit=50)
    assert len(problems) == 1 and "injected.md" in problems[0]


def test_lint_rejects_unknown_profile(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory", writable=True)
    with pytest.raises(ValueError, match="profile"):
        lint_store(store, profile="other", injected_token_limit=10)  # type: ignore[arg-type]
