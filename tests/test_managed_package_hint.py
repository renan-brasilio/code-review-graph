"""Tests for the Sitetracker managed-package redirect hint.

A customer repo only ever contains its own org's code; the package's own
Apex/metadata source is never present there. When a query dead-ends on a
symbol that's clearly a package reference, the tool should redirect to
strk-mcp instead of returning a bare "not found" that invites a wasted
Grep/Read pass.
"""

import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import EdgeInfo, GraphStore, NodeInfo
from code_review_graph.incremental import get_db_path
from code_review_graph.tools._resolve import (
    managed_package_namespace_hint,
    managed_package_not_found,
)
from code_review_graph.tools.query import query_graph, traverse_graph_func
from code_review_graph.tools.symbol_context import trace_symbol_context


@pytest.fixture
def store(tmp_path):
    s = GraphStore(get_db_path(tmp_path))
    yield s
    s.close()


class TestNamespaceStringDetection:
    def test_metadata_namespace_marker(self, store):
        assert managed_package_namespace_hint(store, "sitetracker__Some_Field__c") == "sitetracker"
        assert managed_package_namespace_hint(store, "strk__Other_Object__c") == "strk"

    def test_apex_dot_notation(self, store):
        assert managed_package_namespace_hint(store, "sitetracker.StTriggerFactory") == "sitetracker"

    def test_ordinary_customer_symbol_is_not_flagged(self, store):
        # "St" prefix alone is a legitimate customer-code convention too
        # (see StSampleRecordUtility fixtures) — must not false-positive.
        assert managed_package_namespace_hint(store, "StSampleRecordUtility") is None
        assert managed_package_namespace_hint(store, "Some_Custom_Field__c") is None


class TestNamespaceEdgeDetection:
    def test_unresolved_receiver_call_flags_namespace(self, store):
        handler_file = "Handler.cls"
        store.upsert_node(
            NodeInfo(
                kind="Function", name="andFinally", file_path=handler_file,
                line_start=1, line_end=3, language="apex", parent_name="Handler",
            )
        )
        store.upsert_edge(
            EdgeInfo(
                kind="CALLS",
                source=f"{handler_file}::Handler.andFinally",
                target="StTriggerFactory",
                file_path=handler_file, line=2,
                extra={"receiver": "sitetracker", "method": "createAndExecuteHandler"},
            )
        )
        store.commit()

        assert (
            managed_package_namespace_hint(store, "createAndExecuteHandler")
            == "sitetracker"
        )
        assert managed_package_namespace_hint(store, "sitetracker") == "sitetracker"

    def test_no_matching_edge_returns_none(self, store):
        assert managed_package_namespace_hint(store, "createAndExecuteHandler") is None


class TestManagedPackageNotFound:
    def test_returns_redirect_shape(self, store):
        result = managed_package_not_found(store, "sitetracker__Some_Field__c")
        assert result is not None
        assert result["status"] == "not_found"
        assert result["managed_package_namespace"] == "sitetracker"
        assert any("strk-mcp" in s for s in result["next_tool_suggestions"])

    def test_returns_none_for_unrelated_miss(self, store):
        assert managed_package_not_found(store, "TotallyUnrelatedClass") is None


def _seed_apex_repo(tmp_path: Path) -> Path:
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(get_db_path(root))
    handler_file = str(root / "Handler.cls")
    store.upsert_node(
        NodeInfo(
            kind="Class", name="Handler", file_path=handler_file,
            line_start=1, line_end=5, language="apex",
        )
    )
    store.upsert_node(
        NodeInfo(
            kind="Function", name="andFinally", file_path=handler_file,
            line_start=2, line_end=4, language="apex", parent_name="Handler",
        )
    )
    store.upsert_edge(
        EdgeInfo(
            kind="CALLS",
            source=f"{handler_file}::Handler.andFinally",
            target="StTriggerFactory",
            file_path=handler_file, line=3,
            extra={"receiver": "sitetracker", "method": "createAndExecuteHandler"},
        )
    )
    store.commit()
    store.close()
    return root


class TestQueryGraphRedirect:
    def test_callers_of_managed_package_symbol_redirects(self, tmp_path):
        root = _seed_apex_repo(tmp_path)
        result = query_graph(
            pattern="callers_of",
            target="sitetracker.StTriggerFactory",
            repo_root=str(root),
        )
        assert result["status"] == "not_found"
        assert result["managed_package_namespace"] == "sitetracker"

    def test_callers_of_repo_derived_symbol_redirects(self, tmp_path):
        root = _seed_apex_repo(tmp_path)
        result = query_graph(
            pattern="callers_of",
            target="createAndExecuteHandler",
            repo_root=str(root),
        )
        assert result["status"] == "not_found"
        assert result["managed_package_namespace"] == "sitetracker"

    def test_ordinary_miss_is_unaffected(self, tmp_path):
        root = _seed_apex_repo(tmp_path)
        result = query_graph(
            pattern="callers_of",
            target="TotallyUnrelatedClass",
            repo_root=str(root),
        )
        assert result["status"] == "not_found"
        assert "managed_package_namespace" not in result


class TestTraceSymbolContextRedirect:
    def test_redirects_for_managed_package_symbol(self, tmp_path):
        root = _seed_apex_repo(tmp_path)
        result = trace_symbol_context(target="sitetracker__Some_Field__c", repo_root=str(root))
        assert result["status"] == "not_found"
        assert result["managed_package_namespace"] == "sitetracker"


class TestTraverseGraphRedirect:
    def test_redirects_for_managed_package_symbol(self, tmp_path):
        root = _seed_apex_repo(tmp_path)
        result = traverse_graph_func(query="sitetracker.StTriggerFactory", repo_root=str(root))
        assert result["status"] == "not_found"
        assert result["managed_package_namespace"] == "sitetracker"
        assert result["nodes"] == []
        assert result["traversal"] == []
