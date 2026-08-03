"""Tests for class-level callers_of aggregation and trace_symbol_context."""

import shutil
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph
from code_review_graph.tools.symbol_context import trace_symbol_context


class TestClassCallersAggregation:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir).resolve()
        (self.root / ".git").mkdir()
        (self.root / ".code-review-graph").mkdir()

        self.utility_file = str(self.root / "StSampleRecordUtility.cls")
        self.handler_file = str(self.root / "StSampleFormTriggerHandler.cls")
        self.db_path = str(self.root / ".code-review-graph" / "graph.db")
        self._seed_data()

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _seed_data(self):
        with GraphStore(self.db_path) as store:
            store.upsert_node(
                NodeInfo(
                    kind="Class",
                    name="StSampleRecordUtility",
                    file_path=self.utility_file,
                    line_start=1,
                    line_end=10,
                    language="apex",
                )
            )
            store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name="updateSampleRecordLinks",
                    file_path=self.utility_file,
                    line_start=2,
                    line_end=8,
                    language="apex",
                    parent_name="StSampleRecordUtility",
                )
            )
            store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name="andFinally",
                    file_path=self.handler_file,
                    line_start=3,
                    line_end=5,
                    language="apex",
                    parent_name="StSampleFormTriggerHandler",
                )
            )
            store.upsert_edge(
                EdgeInfo(
                    kind="CALLS",
                    source=f"{self.handler_file}::StSampleFormTriggerHandler.andFinally",
                    target=(
                        f"{self.utility_file}::StSampleRecordUtility"
                        ".updateSampleRecordLinks"
                    ),
                    file_path=self.handler_file,
                    line=4,
                )
            )
            store.commit()

    def test_callers_of_class_aggregates_method_callers(self):
        result = query_graph(
            pattern="callers_of",
            target="StSampleRecordUtility",
            repo_root=str(self.root),
        )
        assert result["status"] == "ok"
        assert result.get("match_tier") == "class_method_aggregation"
        names = {r["name"] for r in result["results"]}
        assert "andFinally" in names

    def test_trace_symbol_context_returns_callers_and_files(self):
        result = trace_symbol_context(
            target="StSampleRecordUtility",
            repo_root=str(self.root),
        )
        assert result["status"] == "ok"
        assert result["symbol"]["name"] == "StSampleRecordUtility"
        caller_names = {c["name"] for c in result["production_callers"]}
        assert "andFinally" in caller_names
        assert any("FormTriggerHandler" in f for f in result["key_files"])
