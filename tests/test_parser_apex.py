"""Tests for Apex method_invocation parsing (Phase 2)."""

from pathlib import Path

from code_review_graph.custom_languages import CONFIG_RELATIVE_PATH
from code_review_graph.parser import CodeParser

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apex"

APEX_TOML = """\
[languages.apex]
extensions = [".cls", ".trigger"]
grammar = "apex"
function_node_types = ["method_declaration", "constructor_declaration"]
class_node_types = ["class_declaration", "trigger_declaration"]
call_node_types = ["method_invocation"]
"""


class TestParserApex:
    def _repo(self, tmp_path: Path) -> tuple[Path, Path]:
        config = tmp_path / CONFIG_RELATIVE_PATH
        config.parent.mkdir(parents=True)
        config.write_text(APEX_TOML, encoding="utf-8")
        src = tmp_path / "static_call.cls"
        src.write_text((FIXTURES / "static_call.cls").read_text(encoding="utf-8"))
        return tmp_path, src

    def test_static_call_stores_receiver_and_method_in_extra(self, tmp_path):
        repo, src = self._repo(tmp_path)
        parser = CodeParser(repo)
        nodes, edges = parser.parse_file(src)

        calls = [e for e in edges if e.kind == "CALLS"]
        static_calls = [
            e for e in calls
            if e.extra.get("receiver") == "StSampleRecordUtility"
        ]
        assert static_calls, f"expected static CALLS edge, got {calls}"
        edge = static_calls[0]
        assert edge.extra.get("method") == "updateSampleRecordLinks"
        assert edge.extra.get("apex_static") is True
        assert edge.target == "updateSampleRecordLinks"

    def test_detect_language_apex(self, tmp_path):
        repo, src = self._repo(tmp_path)
        assert CodeParser(repo).detect_language(src) == "apex"
