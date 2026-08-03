"""Integration tests for Apex static call resolver (Phase 3)."""

import shutil
from pathlib import Path

from code_review_graph.custom_languages import CONFIG_RELATIVE_PATH
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.query import query_graph

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "apex" / "acceptance_package_fixture"


def _build_apex_fixture(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    config_dir = tmp_path / ".code-review-graph"
    config_dir.mkdir()
    shutil.copy(FIXTURE / "languages.toml", config_dir / "languages.toml")
    for name in (
        "StSampleRecordUtility.cls",
        "StSampleFormTriggerHandler.cls",
        "StSampleDocumentTriggerHandler.cls",
        "StSampleRecordTriggerHandler.cls",
        "StSampleRecordTrigger.trigger",
    ):
        shutil.copy(FIXTURE / name, tmp_path / name)

    store = GraphStore(get_db_path(tmp_path))
    result = full_build(tmp_path, store)
    run_post_processing(store)
    return store, result, tmp_path


class TestApexStaticResolver:
    def test_resolver_runs_and_reports(self, tmp_path):
        _, result, _ = _build_apex_fixture(tmp_path)
        stats = result.get("apex_static_resolution")
        assert stats is not None
        assert stats["files_indexed"] > 0
        assert stats["calls_resolved"] >= 2

    def test_callers_of_method_without_fallback_tier(self, tmp_path):
        store, _, root = _build_apex_fixture(tmp_path)
        utility = str(root / "StSampleRecordUtility.cls")
        target = f"{utility}::StSampleRecordUtility.updateSampleRecordLinks"
        result = query_graph(pattern="callers_of", target=target, repo_root=str(root))
        assert result["status"] == "ok"
        assert result.get("match_tier") is None
        names = {r["name"] for r in result["results"]}
        assert "andFinally" in names
        assert "andFinallyExtended" in names
        store.close()

    def test_resolved_edge_targets_qualified_method(self, tmp_path):
        store, _, _ = _build_apex_fixture(tmp_path)
        rows = store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE kind='CALLS' "
            "AND extra LIKE '%apex_resolved%'"
        ).fetchall()
        assert rows
        for row in rows:
            assert "updateSampleRecordLinks" in row["target_qualified"]
        store.close()


class TestApexTriggerResolver:
    def test_invokes_edge_links_trigger_to_handler(self, tmp_path):
        store, result, root = _build_apex_fixture(tmp_path)
        stats = result.get("apex_trigger_resolution")
        assert stats is not None
        assert stats.get("handlers_linked", 0) >= 1

        trigger = str(root / "StSampleRecordTrigger.trigger")
        trigger_qn = f"{trigger}::StSampleRecordTrigger"
        callees = query_graph(
            pattern="callees_of",
            target=trigger_qn,
            repo_root=str(root),
        )
        names = {r["name"] for r in callees["results"]}
        assert "StSampleRecordTriggerHandler" in names
        store.close()
