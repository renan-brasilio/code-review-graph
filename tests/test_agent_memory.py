"""Tests for cross-tool persistent agent memory."""

from pathlib import Path
import pytest

from code_review_graph.agent_memory import (
    forget_agent_memory,
    global_memory_path,
    list_agent_memories,
    recall_memories,
    save_memory,
)
from code_review_graph.tools.memory_tools import (
    forget_memory_func,
    list_memories_func,
    recall_memories_func,
    save_memory_func,
)
from code_review_graph.main import (
    forget_memory_tool,
    list_memories_tool,
    recall_memories_tool,
    save_memory_tool,
)


class _StubProvider:
    dimension = 2

    def __init__(self, name: str = "local:test-model") -> None:
        self.name = name
        self.embedded: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


@pytest.fixture(autouse=True)
def _patch_global_memory(tmp_path, monkeypatch):
    """Use a temporary path for global_memory.db in all tests."""
    global_db = tmp_path / "global_memory.db"
    monkeypatch.setattr(
        "code_review_graph.agent_memory.global_memory_path",
        lambda: global_db,
    )
    return global_db


def test_save_and_recall_repo_scope(tmp_path):
    mem = save_memory(
        content="Uses repository pattern for data access",
        scope="repo",
        category="architecture",
        repo_root=tmp_path,
    )
    assert mem["id"] > 0
    assert mem["content"] == "Uses repository pattern for data access"
    assert mem["category"] == "architecture"
    assert mem["scope"] == "repo"
    assert mem["embedded"] is False

    results = recall_memories("repository", scope="repo", repo_root=tmp_path)
    assert len(results) == 1
    assert results[0]["id"] == mem["id"]
    assert results[0]["content"] == mem["content"]
    assert results[0]["category"] == mem["category"]
    assert results[0]["scope"] == "repo"


def test_save_and_recall_global_scope():
    mem = save_memory(
        content="Prefer 4-space tab indentation",
        scope="global",
        category="preference",
    )
    assert mem["id"] > 0
    assert mem["scope"] == "global"

    results = recall_memories("indentation", scope="global")
    assert len(results) == 1
    assert results[0]["id"] == mem["id"]
    assert results[0]["content"] == "Prefer 4-space tab indentation"


def test_recall_both_scopes(tmp_path):
    repo_mem = save_memory(
        content="Repo fact: uses SQLite",
        scope="repo",
        category="db",
        repo_root=tmp_path,
    )
    global_mem = save_memory(
        content="Global fact: dark mode preference",
        scope="global",
        category="ui",
    )

    both = recall_memories("", scope="both", repo_root=tmp_path)
    assert len(both) == 2
    scopes = {m["scope"] for m in both}
    assert scopes == {"repo", "global"}

    # Keyword search across both
    sql_matches = recall_memories("SQLite", scope="both", repo_root=tmp_path)
    assert len(sql_matches) == 1
    assert sql_matches[0]["id"] == repo_mem["id"]

    ui_matches = recall_memories("dark mode", scope="both", repo_root=tmp_path)
    assert len(ui_matches) == 1
    assert ui_matches[0]["id"] == global_mem["id"]


def test_keyword_recall_content_and_category(tmp_path):
    save_memory(
        content="Python 3.12 features",
        category="convention",
        scope="repo",
        repo_root=tmp_path,
    )
    save_memory(
        content="React UI components",
        category="frontend",
        scope="repo",
        repo_root=tmp_path,
    )

    # Match by content substring
    match_content = recall_memories("3.12", scope="repo", repo_root=tmp_path)
    assert len(match_content) == 1
    assert match_content[0]["content"] == "Python 3.12 features"

    # Match by category substring
    match_cat = recall_memories("frontend", scope="repo", repo_root=tmp_path)
    assert len(match_cat) == 1
    assert match_cat[0]["content"] == "React UI components"


def test_embed_opt_in_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "code_review_graph.agent_memory.get_provider",
        lambda provider: _StubProvider(),
    )

    mem = save_memory(
        content="Semantic indexing test memory",
        scope="repo",
        repo_root=tmp_path,
        embed=True,
    )
    assert mem["embedded"] is True

    results = recall_memories(
        query="indexing",
        scope="repo",
        repo_root=tmp_path,
        embed=True,
    )
    assert len(results) == 1
    assert results[0]["id"] == mem["id"]


def test_list_and_forget_memory(tmp_path):
    repo_mem1 = save_memory(content="Repo 1", scope="repo", repo_root=tmp_path)
    repo_mem2 = save_memory(content="Repo 2", scope="repo", repo_root=tmp_path)
    global_mem = save_memory(content="Global 1", scope="global")

    all_mems = list_agent_memories(scope="both", repo_root=tmp_path)
    assert len(all_mems) == 3

    # Delete repo_mem1 from repo scope
    deleted = forget_agent_memory(repo_mem1["id"], scope="repo", repo_root=tmp_path)
    assert deleted is True

    repo_mems = list_agent_memories(scope="repo", repo_root=tmp_path)
    assert len(repo_mems) == 1
    assert repo_mems[0]["id"] == repo_mem2["id"]

    # Attempting to delete global memory from repo scope should fail and leave global memory intact
    deleted_wrong = forget_agent_memory(global_mem["id"], scope="repo", repo_root=tmp_path)
    assert deleted_wrong is False

    global_mems = list_agent_memories(scope="global")
    assert len(global_mems) == 1
    assert global_mems[0]["id"] == global_mem["id"]


def test_validation_errors(tmp_path):
    with pytest.raises(ValueError, match="Memory content cannot be empty"):
        save_memory("", scope="repo", repo_root=tmp_path)

    with pytest.raises(ValueError, match="Memory content cannot be empty"):
        save_memory("   ", scope="repo", repo_root=tmp_path)

    with pytest.raises(ValueError, match="scope must be one of"):
        save_memory("content", scope="invalid", repo_root=tmp_path)

    with pytest.raises(ValueError, match="repo_root is required when scope='repo'"):
        save_memory("content", scope="repo", repo_root=None)

    with pytest.raises(ValueError, match="scope must be"):
        recall_memories("query", scope="invalid")

    with pytest.raises(ValueError, match="scope must be one of"):
        forget_agent_memory(1, scope="invalid")


def test_recall_repo_no_repo_root_returns_empty():
    assert recall_memories(query="test", scope="repo", repo_root=None) == []
    non_existent = Path("/non/existent/path/for/crg/test")
    assert recall_memories(query="test", scope="repo", repo_root=non_existent) == []


def test_memory_tools_mcp_wrappers(tmp_path):
    (tmp_path / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    saved = save_memory_func(
        content="Tool wrapper test",
        scope="repo",
        category="testing",
        repo_root=str(tmp_path),
    )
    assert saved["id"] > 0

    recalled = recall_memories_func(
        query="wrapper",
        scope="repo",
        repo_root=str(tmp_path),
    )
    assert recalled["count"] == 1
    assert recalled["memories"][0]["content"] == "Tool wrapper test"

    listed = list_memories_func(scope="repo", repo_root=str(tmp_path))
    assert listed["count"] == 1

    forgot = forget_memory_func(
        memory_id=saved["id"],
        scope="repo",
        repo_root=str(tmp_path),
    )
    assert forgot == {"deleted": True, "id": saved["id"], "scope": "repo"}


def test_main_mcp_tools(tmp_path):
    (tmp_path / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    res_save = save_memory_tool(
        content="Main tool test",
        scope="repo",
        repo_root=str(tmp_path),
    )
    assert res_save["id"] > 0
    assert res_save["content"] == "Main tool test"
    assert res_save["scope"] == "repo"

    res_recall = recall_memories_tool(
        query="Main",
        scope="repo",
        repo_root=str(tmp_path),
    )
    assert res_recall["count"] == 1
    assert res_recall["memories"][0]["content"] == "Main tool test"

    res_list = list_memories_tool(scope="repo", repo_root=str(tmp_path))
    assert res_list["count"] == 1

    mem_id = res_save["id"]
    res_forget = forget_memory_tool(
        memory_id=mem_id,
        scope="repo",
        repo_root=str(tmp_path),
    )
    assert res_forget["deleted"] is True
