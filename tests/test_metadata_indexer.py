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

    def test_removed_label_entry_is_cleaned_up_even_though_the_file_survives(self, tmp_path):
        """All of an org's Custom Labels live in one bundled file, unlike the
        one-file-per-entry convention fields/flows use — removing a single
        <labels> entry doesn't change the file-level discovery scan at all,
        so this needs its own diff, separate from stale_metadata_files_removed."""
        labels_dir = tmp_path / "force-app" / "main" / "default" / "labels"
        labels_dir.mkdir(parents=True)
        labels_file = labels_dir / "CustomLabels.labels-meta.xml"
        labels_file.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<CustomLabels xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <labels><fullName>Label_A</fullName><value>A</value></labels>\n"
            "    <labels><fullName>Label_B</fullName><value>B</value></labels>\n"
            "</CustomLabels>\n",
            encoding="utf-8",
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["labels_indexed"] == 2
        assert stats["labels_removed"] == 0

        labels_file.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<CustomLabels xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <labels><fullName>Label_A</fullName><value>A</value></labels>\n"
            "</CustomLabels>\n",
            encoding="utf-8",
        )
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["labels_indexed"] == 1
        assert stats["labels_removed"] == 1
        assert stats["stale_metadata_files_removed"] == 0  # the file itself never disappeared

        remaining = {
            row["name"]
            for row in store._conn.execute("SELECT name FROM nodes WHERE kind='Label'").fetchall()
        }
        assert remaining == {"Label_A"}
        store.close()


OBJECT_META_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Sample Record</label>
    <pluralLabel>Sample Records</pluralLabel>
    <description>A sample custom object.</description>
    <sharingModel>ReadWrite</sharingModel>
    <deploymentStatus>Deployed</deploymentStatus>
</CustomObject>
"""

CMT_OBJECT_META_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Sample Config</label>
    <pluralLabel>Sample Configs</pluralLabel>
    <description>A custom metadata type.</description>
    <deploymentStatus>Deployed</deploymentStatus>
</CustomObject>
"""


class TestObjectIndexing:
    def test_indexes_real_object_metadata(self, tmp_path):
        obj_dir = tmp_path / "force-app" / "main" / "default" / "objects" / "Sample_Record__c"
        obj_dir.mkdir(parents=True)
        (obj_dir / "Sample_Record__c.object-meta.xml").write_text(
            OBJECT_META_XML, encoding="utf-8",
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["real_objects_indexed"] == 1

        row = store._conn.execute(
            "SELECT extra FROM nodes WHERE kind='Object' AND name='Sample_Record__c'"
        ).fetchone()
        assert row is not None
        assert '"synthesized": false' in row["extra"]
        assert "Sample Record" in row["extra"]
        assert "ReadWrite" in row["extra"]
        store.close()

    def test_custom_metadata_type_uses_same_format_flagged_distinctly(self, tmp_path):
        obj_dir = (
            tmp_path / "force-app" / "main" / "default" / "objects" / "Sample_Config__mdt"
        )
        obj_dir.mkdir(parents=True)
        (obj_dir / "Sample_Config__mdt.object-meta.xml").write_text(
            CMT_OBJECT_META_XML, encoding="utf-8",
        )

        store = GraphStore(get_db_path(tmp_path))
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["real_objects_indexed"] == 1

        row = store._conn.execute(
            "SELECT extra FROM nodes WHERE kind='Object' AND name='Sample_Config__mdt'"
        ).fetchone()
        assert '"is_custom_metadata_type": true' in row["extra"]
        store.close()

    def test_field_stub_upgrades_to_real_object_without_new_node(self, tmp_path):
        objects_dir = tmp_path / "force-app" / "main" / "default" / "objects"
        field_dir = objects_dir / "Sample_Record_Link__c" / "fields"
        field_dir.mkdir(parents=True)
        shutil.copy(
            FIXTURE / "objects" / "Sample_Record_Link__c" / "fields"
            / "Record_Identifier_Key__c.field-meta.xml",
            field_dir / "Record_Identifier_Key__c.field-meta.xml",
        )

        store = GraphStore(get_db_path(tmp_path))
        index_salesforce_metadata(store, tmp_path)
        stub = store._conn.execute(
            "SELECT qualified_name, extra FROM nodes WHERE kind='Object' "
            "AND name='Sample_Record_Link__c'"
        ).fetchone()
        assert '"synthesized": true' in stub["extra"]

        obj_dir = objects_dir / "Sample_Record_Link__c"
        (obj_dir / "Sample_Record_Link__c.object-meta.xml").write_text(
            OBJECT_META_XML, encoding="utf-8",
        )
        index_salesforce_metadata(store, tmp_path)
        upgraded = store._conn.execute(
            "SELECT qualified_name, extra FROM nodes WHERE kind='Object' "
            "AND name='Sample_Record_Link__c'"
        ).fetchone()
        assert '"synthesized": false' in upgraded["extra"]
        # Same node identity — BELONGS_TO/REFERENCES edges created before the
        # upgrade must still resolve, not dangle.
        assert upgraded["qualified_name"] == stub["qualified_name"]
        store.close()

    def test_real_object_downgrades_to_stub_when_metadata_file_removed(self, tmp_path):
        objects_dir = tmp_path / "force-app" / "main" / "default" / "objects"
        obj_dir = objects_dir / "Sample_Record__c"
        obj_dir.mkdir(parents=True)
        meta_file = obj_dir / "Sample_Record__c.object-meta.xml"
        meta_file.write_text(OBJECT_META_XML, encoding="utf-8")

        store = GraphStore(get_db_path(tmp_path))
        index_salesforce_metadata(store, tmp_path)

        meta_file.unlink()
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["objects_downgraded"] == 1

        row = store._conn.execute(
            "SELECT extra FROM nodes WHERE kind='Object' AND name='Sample_Record__c'"
        ).fetchone()
        assert row is not None  # never deleted, only downgraded
        assert '"synthesized": true' in row["extra"]
        store.close()


PERMISSION_SET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Sample Permission Set</label>
    <description>Grants access for sample record processing.</description>
    <classAccesses>
        <apexClass>StSampleRecordUtility</apexClass>
        <enabled>true</enabled>
    </classAccesses>
    <classAccesses>
        <apexClass>DisabledClass</apexClass>
        <enabled>false</enabled>
    </classAccesses>
    <classAccesses>
        <apexClass>NonexistentClass</apexClass>
        <enabled>true</enabled>
    </classAccesses>
    <fieldPermissions>
        <field>Sample_Record_Link__c.Record_Identifier_Key__c</field>
        <readable>true</readable>
        <editable>false</editable>
    </fieldPermissions>
    <fieldPermissions>
        <field>Sample_Record_Link__c.Nonexistent_Field__c</field>
        <readable>true</readable>
        <editable>false</editable>
    </fieldPermissions>
    <objectPermissions>
        <object>Sample_Record_Link__c</object>
        <allowRead>true</allowRead>
        <allowCreate>false</allowCreate>
        <allowEdit>false</allowEdit>
        <allowDelete>false</allowDelete>
        <modifyAllRecords>false</modifyAllRecords>
        <viewAllRecords>false</viewAllRecords>
    </objectPermissions>
    <objectPermissions>
        <object>No_Access_Object__c</object>
        <allowRead>false</allowRead>
        <allowCreate>false</allowCreate>
        <allowEdit>false</allowEdit>
        <allowDelete>false</allowDelete>
        <modifyAllRecords>false</modifyAllRecords>
        <viewAllRecords>false</viewAllRecords>
    </objectPermissions>
</PermissionSet>
"""


class TestPermissionSetIndexing:
    def _build_fixture(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        objects_dir = tmp_path / "force-app" / "main" / "default" / "objects"
        objects_dir.mkdir(parents=True)
        _copy_object("Sample_Record_Link__c", objects_dir)

        ps_dir = tmp_path / "force-app" / "main" / "default" / "permissionsets"
        ps_dir.mkdir(parents=True)
        ps_file = ps_dir / "Sample_Permission_Set.permissionset-meta.xml"
        ps_file.write_text(PERMISSION_SET_XML, encoding="utf-8")

        store = GraphStore(get_db_path(tmp_path))
        store.upsert_node(
            NodeInfo(
                kind="Class", name="StSampleRecordUtility",
                file_path=str(tmp_path / "StSampleRecordUtility.cls"),
                line_start=1, line_end=3, language="apex",
            )
        )
        store.commit()
        return store, ps_file

    def test_indexes_permission_set_with_label_and_description(self, tmp_path):
        store, _ = self._build_fixture(tmp_path)
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["permission_sets_indexed"] == 1

        row = store._conn.execute(
            "SELECT extra FROM nodes WHERE kind='PermissionSet' "
            "AND name='Sample_Permission_Set'"
        ).fetchone()
        assert row is not None
        assert "Sample Permission Set" in row["extra"]
        assert "sample record processing" in row["extra"]
        store.close()

    def test_enabled_class_access_resolves_disabled_and_missing_do_not(self, tmp_path):
        store, _ = self._build_fixture(tmp_path)
        stats = index_salesforce_metadata(store, tmp_path)
        # classAccesses: 1 enabled+resolved, 1 disabled (skipped), 1 enabled+unresolved.
        # fieldPermissions also has one unresolved field, for 2 total.
        assert stats["grants_unresolved"] == 2

        ps_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='PermissionSet'"
        ).fetchone()["qualified_name"]
        class_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Class' "
            "AND name='StSampleRecordUtility'"
        ).fetchone()["qualified_name"]

        edges = store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE kind='GRANTS' "
            "AND source_qualified = ? AND extra LIKE '%classAccesses%'",
            (ps_qn,),
        ).fetchall()
        # Only the enabled, resolvable, and enabled-but-unresolved entries create edges —
        # the disabled DisabledClass access is skipped entirely (2 edges, not 3).
        assert len(edges) == 2
        targets = {row["target_qualified"] for row in edges}
        assert class_qn in targets
        assert "NonexistentClass" in targets
        store.close()

    def test_field_and_object_permissions_resolve_to_real_nodes(self, tmp_path):
        store, _ = self._build_fixture(tmp_path)
        index_salesforce_metadata(store, tmp_path)

        field_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Field' "
            "AND name='Record_Identifier_Key__c'"
        ).fetchone()["qualified_name"]
        object_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Object' "
            "AND name='Sample_Record_Link__c'"
        ).fetchone()["qualified_name"]

        field_edge = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='GRANTS' AND target_qualified=?",
            (field_qn,),
        ).fetchone()
        object_edge = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='GRANTS' AND target_qualified=?",
            (object_qn,),
        ).fetchone()
        assert field_edge is not None
        assert object_edge is not None

        # No object-level permission granted for No_Access_Object__c -> no edge at all.
        no_access_edges = store._conn.execute(
            "SELECT 1 FROM edges WHERE kind='GRANTS' AND target_qualified LIKE '%No_Access_Object__c%'"
        ).fetchall()
        assert no_access_edges == []
        store.close()

    def test_deleted_permission_set_file_is_removed_on_next_index_run(self, tmp_path):
        store, ps_file = self._build_fixture(tmp_path)
        index_salesforce_metadata(store, tmp_path)
        assert store._conn.execute(
            "SELECT 1 FROM nodes WHERE kind='PermissionSet'"
        ).fetchone()

        ps_file.unlink()
        stats = index_salesforce_metadata(store, tmp_path)
        assert stats["stale_metadata_files_removed"] == 1
        assert store._conn.execute(
            "SELECT 1 FROM nodes WHERE kind='PermissionSet'"
        ).fetchone() is None
        store.close()
