"""Tests for the Aura component -> Apex controller resolver."""

from pathlib import Path

from code_review_graph.aura_apex_resolver import resolve_aura_apex_wiring
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.query import query_graph

APEX_TOML = (
    "[languages.apex]\n"
    'extensions = [".cls", ".trigger"]\n'
    'grammar = "apex"\n'
    'function_node_types = ["method_declaration", "constructor_declaration"]\n'
    'class_node_types = ["class_declaration", "trigger_declaration"]\n'
    'call_node_types = ["method_invocation"]\n'
)

CMP_XML = (
    '<aura:component controller="ContactController" '
    'xmlns:aura="http://schema.salesforce.com/aura/2010/2000#">\n'
    "    <aura:attribute name=\"contacts\" type=\"Contact[]\"/>\n"
    "</aura:component>\n"
)

CONTROLLER_JS = (
    "({\n"
    "    doInit: function(component, event, helper) {\n"
    "        var action = component.get(\"c.getContacts\");\n"
    "        action.setCallback(this, function(response) {\n"
    "            component.set(\"v.contacts\", response.getReturnValue());\n"
    "        });\n"
    "        $A.enqueueAction(action);\n"
    "    }\n"
    "})\n"
)

HELPER_JS = (
    "({\n"
    "    refresh: function(component, helper) {\n"
    "        var action = component.get(\"c.doesNotExist\");\n"
    "        $A.enqueueAction(action);\n"
    "    }\n"
    "})\n"
)


def _build_fixture(tmp_path: Path, seed_apex: bool = True):
    (tmp_path / ".git").mkdir()
    config_dir = tmp_path / ".code-review-graph"
    config_dir.mkdir()
    (config_dir / "languages.toml").write_text(APEX_TOML, encoding="utf-8")

    if seed_apex:
        (tmp_path / "ContactController.cls").write_text(
            "public class ContactController {\n"
            "    @AuraEnabled\n"
            "    public static List<Contact> getContacts() { return null; }\n"
            "}\n",
            encoding="utf-8",
        )

    bundle_dir = tmp_path / "force-app" / "main" / "default" / "aura" / "contactList"
    bundle_dir.mkdir(parents=True)
    cmp_file = bundle_dir / "contactList.cmp"
    cmp_file.write_text(CMP_XML, encoding="utf-8")
    (bundle_dir / "contactListController.js").write_text(CONTROLLER_JS, encoding="utf-8")
    (bundle_dir / "contactListHelper.js").write_text(HELPER_JS, encoding="utf-8")

    store = GraphStore(get_db_path(tmp_path))
    result = full_build(tmp_path, store)
    run_post_processing(store)
    return store, result, tmp_path, cmp_file


class TestAuraApexResolver:
    def test_component_indexed_with_controller_in_extra(self, tmp_path):
        store, result, root, _ = _build_fixture(tmp_path)
        stats = result.get("aura_apex_resolution")
        assert stats is not None
        assert stats["aura_components_indexed"] == 1

        row = store._conn.execute(
            "SELECT extra FROM nodes WHERE kind='AuraComponent' AND name='contactList'"
        ).fetchone()
        assert row is not None
        assert "ContactController" in row["extra"]
        store.close()

    def test_resolves_action_call_to_real_apex_method(self, tmp_path):
        store, result, root, _ = _build_fixture(tmp_path)
        stats = result.get("aura_apex_resolution")
        assert stats["aura_invokes_created"] == 2
        assert stats["aura_invokes_unresolved"] == 1

        method_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Function' AND name='getContacts'"
        ).fetchone()["qualified_name"]
        edge = store._conn.execute(
            "SELECT source_qualified FROM edges WHERE kind='INVOKES' AND target_qualified=?",
            (method_qn,),
        ).fetchone()
        assert edge is not None
        store.close()

    def test_unresolved_action_is_flagged_not_dropped(self, tmp_path):
        store, result, root, _ = _build_fixture(tmp_path)
        edge = store._conn.execute(
            "SELECT extra FROM edges WHERE kind='INVOKES' "
            "AND target_qualified='ContactController.doesNotExist'"
        ).fetchone()
        assert edge is not None
        assert "unresolved_reference" in edge["extra"]
        store.close()

    def test_callers_of_apex_method_finds_aura_component(self, tmp_path):
        store, _, root, _ = _build_fixture(tmp_path)
        result = query_graph(pattern="callers_of", target="getContacts", repo_root=str(root))
        assert result["status"] == "ok"
        assert any(
            "contactList.cmp" in (r.get("file_path") or "") for r in result["results"]
        ), result["results"]
        store.close()

    def test_no_controller_attribute_indexes_component_without_edges(self, tmp_path):
        (tmp_path / ".git").mkdir()
        bundle_dir = tmp_path / "force-app" / "main" / "default" / "aura" / "plain"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "plain.cmp").write_text(
            '<aura:component xmlns:aura="http://schema.salesforce.com/aura/2010/2000#"/>\n',
            encoding="utf-8",
        )
        store = GraphStore(get_db_path(tmp_path))
        stats = resolve_aura_apex_wiring(store, tmp_path)
        assert stats["aura_components_indexed"] == 1
        assert stats["aura_invokes_created"] == 0
        store.close()

    def test_deleted_bundle_file_is_removed_on_next_index_run(self, tmp_path):
        store, _, root, cmp_file = _build_fixture(tmp_path)
        assert store._conn.execute(
            "SELECT 1 FROM nodes WHERE kind='AuraComponent'"
        ).fetchone()

        cmp_file.unlink()
        stats = resolve_aura_apex_wiring(store, root)
        assert stats["stale_aura_bundles_removed"] == 1
        assert store._conn.execute(
            "SELECT 1 FROM nodes WHERE kind='AuraComponent'"
        ).fetchone() is None
        store.close()
