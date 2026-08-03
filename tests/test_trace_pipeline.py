"""Tests for trace_pipeline multi-hop architecture tracing."""

import shutil
import tempfile
from pathlib import Path

from code_review_graph.graph import EdgeInfo, GraphStore, NodeInfo
from code_review_graph.tools.pipeline_trace import trace_pipeline


class TestTracePipeline:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir).resolve()
        (self.root / ".git").mkdir()
        (self.root / ".code-review-graph").mkdir()

        self.utility_file = str(self.root / "StSampleRecordUtility.cls")
        self.handler_file = str(self.root / "StSampleFormTriggerHandler.cls")
        self.pack_handler_file = str(self.root / "StSampleRecordTriggerHandler.cls")
        self.trigger_file = str(self.root / "StSampleRecordTrigger.trigger")
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
                    kind="Class",
                    name="StSampleFormTriggerHandler",
                    file_path=self.handler_file,
                    line_start=1,
                    line_end=20,
                    language="apex",
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
            store.upsert_node(
                NodeInfo(
                    kind="Class",
                    name="StSampleRecordTriggerHandler",
                    file_path=self.pack_handler_file,
                    line_start=1,
                    line_end=15,
                    language="apex",
                )
            )
            store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name="generateSampleRecordLinks",
                    file_path=self.pack_handler_file,
                    line_start=2,
                    line_end=4,
                    language="apex",
                    parent_name="StSampleRecordTriggerHandler",
                )
            )
            store.upsert_node(
                NodeInfo(
                    kind="Trigger",
                    name="StSampleRecordTrigger",
                    file_path=self.trigger_file,
                    line_start=1,
                    line_end=3,
                    language="apex",
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
            store.upsert_edge(
                EdgeInfo(
                    kind="INVOKES",
                    source=f"{self.trigger_file}::StSampleRecordTrigger",
                    target=f"{self.pack_handler_file}::StSampleRecordTriggerHandler",
                    file_path=self.trigger_file,
                    line=2,
                )
            )
            store.commit()

        for path, body in (
            (self.utility_file, "public class StSampleRecordUtility {\n"
             "  public static void updateSampleRecordLinks(Map<String,Id> m, String t) {}\n}\n"),
            (self.handler_file, "public class StSampleFormTriggerHandler {\n"
             "  public void andFinally() {\n"
             "    StSampleRecordUtility.updateSampleRecordLinks(null,'Form');\n"
             "  }\n}\n"),
            (self.pack_handler_file, "public class StSampleRecordTriggerHandler {\n"
             "  void generateSampleRecordLinks() {}\n"
             "  public void andFinally() { generateSampleRecordLinks(); }\n}\n"),
            (self.trigger_file, "trigger StSampleRecordTrigger on Foo (after insert) {\n"
             "  StTriggerFactory.createAndExecuteHandler(StSampleRecordTriggerHandler.class);\n"
             "}\n"),
        ):
            Path(path).write_text(body, encoding="utf-8")

    def test_trace_pipeline_from_task_finds_handlers(self):
        result = trace_pipeline(
            task=(
                "How does sample record link generation work? "
                "StSampleRecordUtility handlers triggers"
            ),
            repo_root=str(self.root),
        )
        assert result["status"] == "ok"
        step_names = {s["name"] for s in result["pipeline_steps"]}
        assert "updateSampleRecordLinks" in step_names or "andFinally" in step_names
        assert "StSampleRecordUtility" in {
            s.get("parent_name") or s.get("name") for s in result["pipeline_steps"]
        }

    def test_trace_pipeline_with_anchor_and_snippets(self):
        result = trace_pipeline(
            anchor="StSampleRecordUtility",
            task="sample record link generation triggers handlers",
            include_source=True,
            repo_root=str(self.root),
        )
        assert result["status"] == "ok"
        assert result.get("source_snippets")
        assert result.get("do_not_read_paths")
        assert result.get("token_hint")
        step_names = {s["name"] for s in result["pipeline_steps"]}
        assert "generateSampleRecordLinks" in step_names
        assert "updateSampleRecordLinks" in step_names
        parents = {s.get("parent_name") for s in result["pipeline_steps"]}
        assert "StSampleFormTriggerHandler" in parents or any(
            "FormTriggerHandler" in f for f in result["key_files"]
        )

    def test_multi_seed_from_utility_anchor(self):
        result = trace_pipeline(
            task=(
                "How does sample record link generation work? "
                "StSampleRecordUtility triggers handlers"
            ),
            anchor="StSampleRecordUtility",
            repo_root=str(self.root),
        )
        assert result["status"] == "ok"
        assert result.get("seed_count", 0) >= 2
        step_names = {s["name"] for s in result["pipeline_steps"]}
        assert "generateSampleRecordLinks" in step_names
