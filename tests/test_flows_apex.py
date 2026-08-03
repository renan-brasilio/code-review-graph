"""Tests for Salesforce flow entry-point patterns (Phase 5)."""

import pytest

from code_review_graph.flows import _matches_entry_name
from code_review_graph.graph import GraphNode


def _node(name: str) -> GraphNode:
    return GraphNode(
        id=1,
        kind="Function",
        name=name,
        qualified_name=f"file.cls::{name}",
        file_path="file.cls",
        line_start=1,
        line_end=5,
        language="apex",
        parent_name="Handler",
        params=None,
        return_type=None,
        is_test=False,
        file_hash=None,
        extra={},
    )


class TestFlowsApex:
    @pytest.mark.parametrize(
        "name",
        [
            "afterInsert",
            "beforeUpdate",
            "andFinally",
            "bulkBefore",
            "initialize",
        ],
    )
    def test_salesforce_handler_methods_match_entry_patterns(self, name):
        assert _matches_entry_name(_node(name)) is True

    def test_regular_helper_does_not_match(self):
        assert _matches_entry_name(_node("generateSampleRecordLinks")) is False
