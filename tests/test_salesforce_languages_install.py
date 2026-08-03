"""Tests for Salesforce languages.toml auto-install."""

from pathlib import Path

from code_review_graph.incremental import ensure_salesforce_languages_config


def test_ensure_salesforce_languages_skips_non_sfdx(tmp_path):
    assert ensure_salesforce_languages_config(tmp_path) == "skipped"


def test_ensure_salesforce_languages_creates_for_sfdx(tmp_path):
    (tmp_path / "sfdx-project.json").write_text("{}", encoding="utf-8")
    state = ensure_salesforce_languages_config(tmp_path)
    assert state == "created"
    dest = tmp_path / ".code-review-graph" / "languages.toml"
    assert dest.is_file()
    assert "[languages.apex]" in dest.read_text(encoding="utf-8")


def test_ensure_salesforce_languages_idempotent(tmp_path):
    (tmp_path / "sfdx-project.json").write_text("{}", encoding="utf-8")
    ensure_salesforce_languages_config(tmp_path)
    assert ensure_salesforce_languages_config(tmp_path) == "already-present"
