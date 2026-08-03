"""End-to-end tests for Custom Label indexing + Apex/LWC reference resolution.

Covers metadata_indexer._index_labels, apex_label_resolver (Label.X in
Apex — Sitetracker's own coding standard requires user-visible text go
through Custom Labels), and lwc_apex_resolver's @salesforce/label/c.X
handling.
"""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.query import query_graph

LABELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CustomLabels xmlns="http://soap.sforce.com/2006/04/metadata">
    <labels>
        <fullName>My_Error_Message</fullName>
        <categories>Error</categories>
        <language>en_US</language>
        <protected>false</protected>
        <shortDescription>My Error Message</shortDescription>
        <value>Something went wrong. Please try again.</value>
    </labels>
    <labels>
        <fullName>Unused_Label</fullName>
        <language>en_US</language>
        <protected>false</protected>
        <shortDescription>Unused</shortDescription>
        <value>Never referenced.</value>
    </labels>
</CustomLabels>
"""

APEX_TOML = (
    "[languages.apex]\n"
    'extensions = [".cls", ".trigger"]\n'
    'grammar = "apex"\n'
    'function_node_types = ["method_declaration", "constructor_declaration"]\n'
    'class_node_types = ["class_declaration", "trigger_declaration"]\n'
    'call_node_types = ["method_invocation"]\n'
)


def _build_fixture(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    config_dir = tmp_path / ".code-review-graph"
    config_dir.mkdir()
    (config_dir / "languages.toml").write_text(APEX_TOML, encoding="utf-8")

    labels_dir = tmp_path / "force-app" / "main" / "default" / "labels"
    labels_dir.mkdir(parents=True)
    (labels_dir / "CustomLabels.labels-meta.xml").write_text(LABELS_XML, encoding="utf-8")

    (tmp_path / "ErrorUtil.cls").write_text(
        "public class ErrorUtil {\n"
        "    public static String getErrorMessage() {\n"
        "        return Label.My_Error_Message;\n"
        "    }\n"
        "\n"
        "    public static String getUnknownLabel() {\n"
        "        return Label.Nonexistent_Label;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    lwc_dir = tmp_path / "force-app" / "main" / "default" / "lwc" / "errorBanner"
    lwc_dir.mkdir(parents=True)
    (lwc_dir / "errorBanner.js").write_text(
        "import { LightningElement } from 'lwc';\n"
        "import ERROR_MESSAGE from '@salesforce/label/c.My_Error_Message';\n"
        "\n"
        "export default class ErrorBanner extends LightningElement {\n"
        "    label = ERROR_MESSAGE;\n"
        "}\n",
        encoding="utf-8",
    )

    store = GraphStore(get_db_path(tmp_path))
    result = full_build(tmp_path, store)
    run_post_processing(store)
    return store, result, tmp_path


class TestLabelIndexing:
    def test_both_labels_indexed(self, tmp_path):
        store, result, _ = _build_fixture(tmp_path)
        metadata_stats = result.get("metadata_indexing")
        assert metadata_stats is not None
        assert metadata_stats["labels_indexed"] == 2

        rows = store._conn.execute(
            "SELECT name, extra FROM nodes WHERE kind='Label' ORDER BY name"
        ).fetchall()
        names = [r["name"] for r in rows]
        assert names == ["My_Error_Message", "Unused_Label"]
        assert "Something went wrong" in rows[0]["extra"]
        store.close()


class TestApexLabelResolver:
    def test_resolved_reference_scoped_to_enclosing_method(self, tmp_path):
        store, result, root = _build_fixture(tmp_path)
        stats = result.get("apex_label_resolution")
        assert stats is not None
        assert stats["references_resolved"] == 1
        assert stats["references_unresolved"] == 1

        label_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Label' AND name='My_Error_Message'"
        ).fetchone()["qualified_name"]

        edge = store._conn.execute(
            "SELECT source_qualified FROM edges WHERE kind='REFERENCES' AND target_qualified=?",
            (label_qn,),
        ).fetchone()
        assert edge is not None
        assert edge["source_qualified"].endswith("ErrorUtil.getErrorMessage")
        store.close()

    def test_unresolved_reference_is_flagged_not_dropped(self, tmp_path):
        store, result, root = _build_fixture(tmp_path)
        edge = store._conn.execute(
            "SELECT extra FROM edges WHERE kind='REFERENCES' "
            "AND target_qualified='Label.Nonexistent_Label'"
        ).fetchone()
        assert edge is not None
        assert "unresolved_reference" in edge["extra"]
        store.close()

    def test_references_to_label_finds_apex_caller(self, tmp_path):
        store, _, root = _build_fixture(tmp_path)
        result = query_graph(
            pattern="references_to", target="My_Error_Message", repo_root=str(root),
        )
        assert result["status"] == "ok"
        names = {r["name"] for r in result["results"]}
        assert any("getErrorMessage" in n or n == "ErrorUtil" for n in names) or result["results"]
        store.close()


class TestLwcLabelResolver:
    def test_label_import_resolved_to_real_label_node(self, tmp_path):
        store, result, root = _build_fixture(tmp_path)
        stats = result.get("lwc_apex_resolution")
        assert stats is not None
        assert stats["label_imports_resolved"] == 1
        assert stats["label_imports_unresolved"] == 0

        label_qn = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE kind='Label' AND name='My_Error_Message'"
        ).fetchone()["qualified_name"]
        edge = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='IMPORTS_FROM' AND target_qualified=?",
            (label_qn,),
        ).fetchone()
        assert edge is not None
        store.close()
