"""Tests for Salesforce metadata indexer (Phase 6)."""

import shutil
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.metadata_indexer import index_salesforce_metadata
from code_review_graph.search import hybrid_search

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "salesforce"


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
