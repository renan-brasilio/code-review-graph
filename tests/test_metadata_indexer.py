"""Tests for Salesforce metadata indexer (Phase 6)."""

import json
import shutil
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.metadata_indexer import index_salesforce_metadata
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
