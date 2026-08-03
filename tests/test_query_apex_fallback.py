"""Tests for Apex callers_of parent-class fallback (Phase 1)."""

import shutil
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "apex" / "acceptance_package_fixture"


class TestQueryApexFallback:
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
                    line_end=3,
                    language="apex",
                )
            )
            store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name="updateSampleRecordLinks",
                    file_path=self.utility_file,
                    line_start=2,
                    line_end=2,
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
                    target="StSampleRecordUtility",
                    file_path=self.handler_file,
                    line=4,
                    extra={"receiver": "StSampleRecordUtility", "method": "updateSampleRecordLinks"},
                )
            )
            store.commit()

    def test_callers_of_method_falls_back_to_parent_class(self):
        target = (
            f"{self.utility_file}::StSampleRecordUtility"
            ".updateSampleRecordLinks"
        )
        result = query_graph(
            pattern="callers_of",
            target=target,
            repo_root=str(self.root),
        )

        assert result["status"] == "ok"
        assert result["match_tier"] == "parent_class_fallback"
        names = {r["name"] for r in result["results"]}
        assert "andFinally" in names
