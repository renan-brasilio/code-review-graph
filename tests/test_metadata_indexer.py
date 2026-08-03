"""Tests for Salesforce metadata indexer (Phase 6)."""

import json
import shutil
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.metadata_indexer import index_salesforce_metadata
from code_review_graph.parser import NodeInfo
from code_review_graph.search import hybrid_search

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "salesforce"


def _copy_object(fixture_object: str, dest_objects_dir: Path) -> None:
    src = FIXTURE / "objects" / fixture_object
    dest = dest_objects_dir / fixture_object
    shutil.copytree(src, dest)


class TestMetadataIndexer:
    def test_indexes_field_with_formula(self, tmp_path):
        objects_dir = (
            tmp_path / "force-app" / "main" / "default" / "objects"
            / "Sample_Record_Link__c" / "fields"
        )
        objects_dir.mkdir(parents=True)
        shutil.copy(
            FIXTURE / "objects" / "Sample_Record_Link__c" / "fields"
            / "Record_Identifier_Key__c.field-meta.xml",
            objects_dir / "Record_Identifier_Key__c.field-meta.xml",
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["fields_indexed"] == 1
        assert stats["references_created"] >= 1

        rows = store._conn.execute(
            "SELECT name, extra FROM nodes WHERE kind='Field'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "Record_Identifier_Key__c"
        assert "formula" in rows[0]["extra"]
        store.close()

    def test_semantic_search_finds_field_by_api_name(self, tmp_path):
        objects_dir = (
            tmp_path / "force-app" / "main" / "default" / "objects"
            / "Sample_Record_Link__c" / "fields"
        )
        objects_dir.mkdir(parents=True)
        shutil.copy(
            FIXTURE / "objects" / "Sample_Record_Link__c" / "fields"
            / "Record_Identifier_Key__c.field-meta.xml",
            objects_dir / "Record_Identifier_Key__c.field-meta.xml",
        )

        store = GraphStore(get_db_path(tmp_path))
        index_salesforce_metadata(store, tmp_path)
        from code_review_graph.postprocessing import run_post_processing

        run_post_processing(store)
        hits = hybrid_search(store, "Record_Identifier_Key__c", limit=5)
        names = {h.get("name") for h in hits}
        assert "Record_Identifier_Key__c" in names
        store.close()

    def test_resolves_relationship_and_cross_object_formula_refs(self, tmp_path):
        objects_dir = tmp_path / "force-app" / "main" / "default" / "objects"
        objects_dir.mkdir(parents=True)
        _copy_object("Sample_Record_Link__c", objects_dir)
        _copy_object("Sample_Record_Template__c", objects_dir)

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["fields_indexed"] == 3
        assert stats["references_unresolved"] == 0

        node_qns = {
            row["qualified_name"]
            for row in store._conn.execute(
                "SELECT qualified_name FROM nodes WHERE kind='Field'"
            ).fetchall()
        }
        edges = store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE kind='REFERENCES'"
        ).fetchall()
        assert len(edges) == 2
        for row in edges:
            assert row["target_qualified"] in node_qns, (
                f"REFERENCES edge target {row['target_qualified']!r} is not a real "
                "Field node — formula reference resolution regressed"
            )
            assert "unresolved_reference" not in (row["extra"] or "")

        lookup_field = store._conn.execute(
            "SELECT extra FROM nodes WHERE kind='Field' AND name='Sample_Record_Link_Template__c'"
        ).fetchone()
        assert "Sample_Record_Template__c" in lookup_field["extra"]
        store.close()

    def test_uses_sfdx_project_package_directories(self, tmp_path):
        pkg_dir = tmp_path / "my-custom-pkg"
        objects_dir = pkg_dir / "objects" / "Sample_Record_Link__c" / "fields"
        objects_dir.mkdir(parents=True)
        shutil.copy(
            FIXTURE / "objects" / "Sample_Record_Link__c" / "fields"
            / "Record_Identifier_Key__c.field-meta.xml",
            objects_dir / "Record_Identifier_Key__c.field-meta.xml",
        )
        (tmp_path / "sfdx-project.json").write_text(
            json.dumps({"packageDirectories": [{"path": "my-custom-pkg", "default": True}]}),
            encoding="utf-8",
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["fields_indexed"] == 1
        store.close()

    def test_field_belongs_to_object_stub(self, tmp_path):
        objects_dir = (
            tmp_path / "force-app" / "main" / "default" / "objects"
            / "Sample_Record_Link__c" / "fields"
        )
        objects_dir.mkdir(parents=True)
        shutil.copy(
            FIXTURE / "objects" / "Sample_Record_Link__c" / "fields"
            / "Record_Identifier_Key__c.field-meta.xml",
            objects_dir / "Record_Identifier_Key__c.field-meta.xml",
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["objects_indexed"] == 1

        obj = store._conn.execute(
            "SELECT qualified_name, extra FROM nodes WHERE kind='Object' AND name='Sample_Record_Link__c'"
        ).fetchone()
        assert obj is not None
        assert "synthesized" in obj["extra"]

        edge = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='BELONGS_TO'"
        ).fetchone()
        assert edge is not None
        assert edge["target_qualified"] == obj["qualified_name"]
        store.close()


class TestFlowIndexer:
    def _write_flows(self, tmp_path: Path) -> Path:
        flows_dir = tmp_path / "force-app" / "main" / "default" / "flows"
        flows_dir.mkdir(parents=True)
        for name in ("Sample_Record_After_Save", "Sample_Record_Link_Generator"):
            shutil.copy(
                FIXTURE / "flows" / f"{name}.flow-meta.xml",
                flows_dir / f"{name}.flow-meta.xml",
            )
        return flows_dir

    def test_indexes_flow_with_apex_subflow_and_object_refs(self, tmp_path):
        self._write_flows(tmp_path)

        store = GraphStore(get_db_path(tmp_path))
        # Seed an Apex Class node so the actionCalls -> Apex edge can resolve.
        store.upsert_node(
            NodeInfo(
                kind="Class",
                name="StSampleRecordUtility",
                file_path=str(tmp_path / "StSampleRecordUtility.cls"),
                line_start=1,
                line_end=3,
                language="apex",
            )
        )
        store.commit()

        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["flows_indexed"] == 2
        assert stats["flow_invokes_created"] == 2
        assert stats["flow_invokes_unresolved"] == 0
        assert stats["flow_references_created"] == 2
        assert stats["objects_indexed"] == 2

        flow_a = store._conn.execute(
            "SELECT qualified_name, extra FROM nodes WHERE kind='SalesforceFlow' "
            "AND name='Sample_Record_After_Save'"
        ).fetchone()
        assert flow_a is not None
        assert "\"process_type\": \"AutoLaunchedFlow\"" in flow_a["extra"]
        assert "\"trigger_object\": \"Sample_Record__c\"" in flow_a["extra"]

        invoke_edges = store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE kind='INVOKES' "
            "AND source_qualified = ?",
            (flow_a["qualified_name"],),
        ).fetchall()
        assert len(invoke_edges) == 2
        for row in invoke_edges:
            assert "unresolved_reference" not in (row["extra"] or "")

        apex_class = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Class' AND name='StSampleRecordUtility'"
        ).fetchone()
        subflow_b = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='SalesforceFlow' "
            "AND name='Sample_Record_Link_Generator'"
        ).fetchone()
        targets = {row["target_qualified"] for row in invoke_edges}
        assert apex_class["qualified_name"] in targets
        assert subflow_b["qualified_name"] in targets
        store.close()

    def test_unresolved_apex_action_and_subflow_are_flagged(self, tmp_path):
        self._write_flows(tmp_path)
        store = GraphStore(get_db_path(tmp_path))
        # No Apex Class node seeded this time — StSampleRecordUtility can't resolve.
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["flow_invokes_unresolved"] == 1
        store.close()


class TestStaleMetadataFileCleanup:
    """A deleted *.field-meta.xml/*.flow-meta.xml/CustomLabels.labels-meta.xml
    has no configured Tree-sitter language, so the general stale-file
    reconciliation in incremental.py never sees it — metadata_indexer must
    clean these up itself or a full rebuild leaves a permanent phantom node."""

    def test_deleted_field_file_is_removed_on_next_index_run(self, tmp_path):
        objects_dir = (
            tmp_path / "force-app" / "main" / "default" / "objects"
            / "Sample_Record_Link__c" / "fields"
        )
        objects_dir.mkdir(parents=True)
        field_file = objects_dir / "Record_Identifier_Key__c.field-meta.xml"
        shutil.copy(
            FIXTURE / "objects" / "Sample_Record_Link__c" / "fields"
            / "Record_Identifier_Key__c.field-meta.xml",
            field_file,
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["fields_indexed"] == 1
        assert stats["stale_metadata_files_removed"] == 0

        field_file.unlink()
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["fields_indexed"] == 0
        assert stats["stale_metadata_files_removed"] == 1

        remaining = store._conn.execute("SELECT name FROM nodes WHERE kind='Field'").fetchall()
        assert remaining == []
        store.close()

    def test_deleted_flow_file_is_removed_on_next_index_run(self, tmp_path):
        flows_dir = tmp_path / "force-app" / "main" / "default" / "flows"
        flows_dir.mkdir(parents=True)
        flow_file = flows_dir / "Sample_Record_After_Save.flow-meta.xml"
        shutil.copy(
            FIXTURE / "flows" / "Sample_Record_After_Save.flow-meta.xml", flow_file,
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["flows_indexed"] == 1

        flow_file.unlink()
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["flows_indexed"] == 0
        assert stats["stale_metadata_files_removed"] == 1

        remaining = store._conn.execute(
            "SELECT name FROM nodes WHERE kind='SalesforceFlow'"
        ).fetchall()
        assert remaining == []
        store.close()

    def test_unrelated_field_files_survive_a_sibling_deletion(self, tmp_path):
        objects_dir = tmp_path / "force-app" / "main" / "default" / "objects"
        objects_dir.mkdir(parents=True)
        _copy_object("Sample_Record_Link__c", objects_dir)
        _copy_object("Sample_Record_Template__c", objects_dir)

        store = GraphStore(get_db_path(tmp_path))
        index_salesforce_metadata(store, tmp_path)

        (
            objects_dir / "Sample_Record_Template__c" / "fields"
            / "Record_Template__c.field-meta.xml"
        ).unlink()
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["stale_metadata_files_removed"] == 1

        remaining = {
            row["name"]
            for row in store._conn.execute("SELECT name FROM nodes WHERE kind='Field'").fetchall()
        }
        assert remaining == {"Record_Identifier_Key__c", "Sample_Record_Link_Template__c"}
        store.close()
