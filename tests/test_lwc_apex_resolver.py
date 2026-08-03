"""Tests for the LWC/Aura @salesforce/apex + @salesforce/schema import resolver."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.lwc_apex_resolver import resolve_lwc_apex_imports
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.query import query_graph


def _build_lwc_fixture(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    config_dir = tmp_path / ".code-review-graph"
    config_dir.mkdir()
    (config_dir / "languages.toml").write_text(
        "[languages.apex]\n"
        'extensions = [".cls", ".trigger"]\n'
        'grammar = "apex"\n'
        'function_node_types = ["method_declaration", "constructor_declaration"]\n'
        'class_node_types = ["class_declaration", "trigger_declaration"]\n'
        'call_node_types = ["method_invocation"]\n',
        encoding="utf-8",
    )

    (tmp_path / "ContactController.cls").write_text(
        "public class ContactController {\n"
        "    public static List<Contact> getContacts() { return null; }\n"
        "}\n",
        encoding="utf-8",
    )

    objects_dir = (
        tmp_path / "force-app" / "main" / "default" / "objects" / "Contact" / "fields"
    )
    objects_dir.mkdir(parents=True)
    (objects_dir / "My_Custom_Field__c.field-meta.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <fullName>My_Custom_Field__c</fullName>\n"
        "    <type>Text</type>\n"
        "</CustomField>\n",
        encoding="utf-8",
    )

    lwc_dir = tmp_path / "force-app" / "main" / "default" / "lwc" / "myComponent"
    lwc_dir.mkdir(parents=True)
    (lwc_dir / "myComponent.js").write_text(
        "import { LightningElement, wire } from 'lwc';\n"
        "import getContacts from '@salesforce/apex/ContactController.getContacts';\n"
        "import CUSTOM_FIELD from '@salesforce/schema/Contact.My_Custom_Field__c';\n"
        "import missingMethod from '@salesforce/apex/ContactController.doesNotExist';\n"
        "\n"
        "export default class MyComponent extends LightningElement {\n"
        "    @wire(getContacts)\n"
        "    contacts;\n"
        "\n"
        "    callImperative() {\n"
        "        getContacts({ someParam: 1 }).then(result => {\n"
        "            console.log(result, CUSTOM_FIELD);\n"
        "        });\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    store = GraphStore(get_db_path(tmp_path))
    result = full_build(tmp_path, store)
    run_post_processing(store)
    return store, result, tmp_path


class TestLwcApexResolver:
    def test_resolver_runs_and_reports(self, tmp_path):
        _, result, _ = _build_lwc_fixture(tmp_path)
        stats = result.get("lwc_apex_resolution")
        assert stats is not None
        assert stats["files_indexed"] == 1
        assert stats["apex_imports_resolved"] == 1
        assert stats["apex_imports_unresolved"] == 1
        assert stats["schema_imports_resolved"] == 1
        assert stats["apex_calls_resolved"] == 2  # @wire reference + imperative call

    def test_imports_from_edge_resolved_to_real_apex_node(self, tmp_path):
        store, _, root = _build_lwc_fixture(tmp_path)
        method_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Function' AND name='getContacts'"
        ).fetchone()["qualified_name"]

        edge = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='IMPORTS_FROM' "
            "AND target_qualified = ?",
            (method_qn,),
        ).fetchone()
        assert edge is not None
        store.close()

    def test_schema_import_resolved_to_real_field_node(self, tmp_path):
        store, _, root = _build_lwc_fixture(tmp_path)
        field_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Field' AND name='My_Custom_Field__c'"
        ).fetchone()["qualified_name"]

        edge = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='IMPORTS_FROM' "
            "AND target_qualified = ?",
            (field_qn,),
        ).fetchone()
        assert edge is not None
        store.close()

    def test_callers_of_apex_method_finds_lwc_caller(self, tmp_path):
        store, _, root = _build_lwc_fixture(tmp_path)
        result = query_graph(pattern="callers_of", target="getContacts", repo_root=str(root))
        assert result["status"] == "ok"
        names = {r["name"] for r in result["results"]}
        # The LWC file's own top-level scope is the caller (no enclosing
        # method wraps the .then()/@wire usage in this fixture).
        assert any("myComponent.js" in (r.get("file_path") or "") for r in result["results"]), (
            names
        )
        store.close()

    def test_unresolved_import_leaves_bare_target_and_is_counted(self, tmp_path):
        store, result, root = _build_lwc_fixture(tmp_path)
        stats = result.get("lwc_apex_resolution")
        assert stats["apex_imports_unresolved"] == 1

        edge = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='IMPORTS_FROM' "
            "AND target_qualified = '@salesforce/apex/ContactController.doesNotExist'"
        ).fetchone()
        assert edge is not None
        store.close()

    def test_no_op_when_no_salesforce_imports_present(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "plain.js").write_text("import x from './local';\n", encoding="utf-8")
        store = GraphStore(get_db_path(tmp_path))
        full_build(tmp_path, store)
        stats = resolve_lwc_apex_imports(store)
        assert stats["files_indexed"] == 0
        store.close()
