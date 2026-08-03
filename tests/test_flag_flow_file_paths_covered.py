"""Tests for do_not_read_paths coverage on SalesforceFlow node results.

A SalesforceFlow node's extra["steps"] already summarizes the flow — the
raw *.flow-meta.xml can be large in real orgs, so any tool surfacing a
SalesforceFlow node (not just trace_pipeline/trace_symbol_context) should
tell the agent not to re-read it.
"""

import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import NodeInfo
from code_review_graph.tools._common import flag_flow_file_paths_covered


class TestFlagFlowFilePathsCovered:
    def test_query_graph_shape_flags_flow_paths(self):
        result = {
            "status": "ok",
            "results": [
                {"kind": "SalesforceFlow", "name": "MyFlow", "file_path": "flows/MyFlow.flow-meta.xml"},
                {"kind": "Class", "name": "SomeClass", "file_path": "SomeClass.cls"},
            ],
        }
        out = flag_flow_file_paths_covered(result)
        assert out["do_not_read_paths"] == ["flows/MyFlow.flow-meta.xml"]
        assert "token_hint" in out

    def test_traverse_graph_shape_flags_flow_paths(self):
        result = {
            "status": "ok",
            "traversal": [
                {"kind": "SalesforceFlow", "name": "MyFlow", "file": "flows/MyFlow.flow-meta.xml"},
            ],
        }
        out = flag_flow_file_paths_covered(result, entries_key="traversal", file_key="file")
        assert out["do_not_read_paths"] == ["flows/MyFlow.flow-meta.xml"]

    def test_no_flow_nodes_is_a_no_op(self):
        result = {"status": "ok", "results": [{"kind": "Class", "name": "X", "file_path": "X.cls"}]}
        out = flag_flow_file_paths_covered(result)
        assert out is result
        assert "do_not_read_paths" not in out

    def test_merges_with_existing_do_not_read_paths(self):
        result = {
            "status": "ok",
            "do_not_read_paths": ["already/covered.cls"],
            "results": [
                {"kind": "SalesforceFlow", "name": "MyFlow", "file_path": "flows/MyFlow.flow-meta.xml"},
            ],
        }
        out = flag_flow_file_paths_covered(result)
        assert out["do_not_read_paths"] == ["already/covered.cls", "flows/MyFlow.flow-meta.xml"]


def _seed_flow_repo(tmp_path: Path) -> Path:
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    flow_file = str(root / "MyFlow.flow-meta.xml")
    store = GraphStore(get_db_path(root))
    store.upsert_node(
        NodeInfo(
            kind="SalesforceFlow", name="MyFlow", file_path=flow_file,
            line_start=1, line_end=1, language="salesforce_metadata",
            extra={"metadata_type": "Flow", "steps": [{"name": "start", "type": "start"}]},
        )
    )
    store.commit()
    store.close()
    return root


class TestIntegration:
    def test_query_graph_tool_flags_flow_file(self, tmp_path):
        from code_review_graph.main import query_graph_tool

        root = _seed_flow_repo(tmp_path)
        result = query_graph_tool(
            pattern="file_summary", target="MyFlow.flow-meta.xml", repo_root=str(root),
        )
        assert result.get("do_not_read_paths"), result

    def test_traverse_graph_tool_flags_flow_file(self, tmp_path):
        from code_review_graph.main import traverse_graph_tool

        root = _seed_flow_repo(tmp_path)
        result = traverse_graph_tool(query="MyFlow", repo_root=str(root))
        assert result.get("do_not_read_paths"), result
