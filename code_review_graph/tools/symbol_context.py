"""One-shot symbol tracing for token-efficient architecture discovery."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..graph import node_to_dict
from ._common import _get_store, snippet_coverage_fields
from ._resolve import (
    collect_callees,
    collect_callers,
    collect_class_callees,
    collect_class_callers,
    compact_node,
    managed_package_not_found,
    partition_production_and_tests,
    resolve_query_target,
)

logger = logging.getLogger(__name__)

_LIFECYCLE_METHODS = frozenset({
    "andFinally", "andFinallyExtended", "afterInsert", "afterUpdate",
    "afterDelete", "beforeInsert", "beforeUpdate", "beforeDelete",
    "generateSampleRecordLinks", "generateSampleRecords",
    "updateSampleRecordLinks",
})


def _relative_path(root: Path, file_path: str) -> str:
    try:
        return str(Path(file_path).relative_to(root))
    except ValueError:
        return file_path


def _snippet_for_nodes(
    root: Path,
    nodes: list[dict],
    max_files: int,
    max_lines_per_file: int,
) -> dict[str, str]:
    """Return compact source snippets for the most relevant nodes only."""
    snippets: dict[str, str] = {}
    seen_files: set[str] = set()

    ranked = sorted(
        nodes,
        key=lambda n: (
            0 if n.get("name") in _LIFECYCLE_METHODS else 1,
            0 if n.get("kind") == "Function" else 1,
            n.get("file_path") or "",
        ),
    )

    for node in ranked:
        file_path = node.get("file_path")
        if not file_path or file_path in seen_files:
            continue
        if len(seen_files) >= max_files:
            break
        full_path = Path(file_path)
        if not full_path.is_file():
            alt = root / _relative_path(root, file_path)
            full_path = alt if alt.is_file() else full_path
        if not full_path.is_file():
            continue
        try:
            lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        start = max(0, (node.get("line_start") or 1) - 3)
        end = min(len(lines), (node.get("line_end") or start + 1) + 2)
        if end - start > max_lines_per_file:
            end = start + max_lines_per_file

        chunk = "\n".join(
            f"{i + 1}: {line}" for i, line in enumerate(lines[start:end], start=start)
        )
        rel = _relative_path(root, str(full_path))
        snippets[rel] = chunk
        seen_files.add(file_path)

    return snippets


def trace_symbol_context(
    target: str,
    repo_root: str | None = None,
    include_source: bool = False,
    max_source_files: int = 4,
    max_lines_per_file: int = 40,
    detail_level: str = "minimal",
) -> dict[str, Any]:
    """Trace callers, callees, and key files for a symbol in one compact response.

    Designed to replace multiple query_graph + Read passes for architecture
    questions like "how does X connect to Y?".
    """
    store, root = _get_store(repo_root)
    try:
        node = resolve_query_target(store, root, target, "callers_of")
        if not node:
            managed_hint = managed_package_not_found(store, target)
            if managed_hint:
                return managed_hint
            return {
                "status": "not_found",
                "summary": f"No graph node found for '{target}'.",
                "next_tool_suggestions": [
                    "semantic_search_nodes",
                    "list_graph_stats",
                ],
            }

        if node.kind == "Class":
            callers, _ = collect_class_callers(store, node)
            callees, _ = collect_class_callees(store, node)
        else:
            callers, _, _ = collect_callers(store, node)
            callees, _ = collect_callees(store, node)

        prod_callers, test_callers = partition_production_and_tests(callers)
        prod_callees, test_callees = partition_production_and_tests(callees)

        key_files: list[str] = []
        seen_files: set[str] = set()
        for group in (prod_callers, [node_to_dict(node)], prod_callees):
            for entry in group:
                fp = entry.get("file_path")
                if fp and fp not in seen_files:
                    seen_files.add(fp)
                    key_files.append(_relative_path(root, fp))

        summary_parts = [
            f"{node.kind} '{node.name}' ({node.language or 'unknown'}):",
            f"  {len(prod_callers)} production caller(s)",
            f"  {len(prod_callees)} production callee/trigger link(s)",
        ]
        if test_callers:
            summary_parts.append(f"  {len(test_callers)} test caller(s) omitted from primary view")

        response: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "symbol": compact_node(node_to_dict(node)),
            "production_callers": [compact_node(c) for c in prod_callers[:6]],
            "production_callees": [compact_node(c) for c in prod_callees[:6]],
            "key_files": key_files[:8],
            "next_tool_suggestions": [
                "Answer from this response unless a branch is missing",
            ],
        }

        if detail_level != "minimal":
            response["test_callers"] = [compact_node(c) for c in test_callers[:8]]
            response["test_callees"] = [compact_node(c) for c in test_callees[:8]]

        if include_source:
            snippet_nodes = (
                prod_callers[:2]
                + [node_to_dict(node)]
                + prod_callees[:1]
            )
            snippets = _snippet_for_nodes(
                root, snippet_nodes, max_source_files, max_lines_per_file,
            )
            response["source_snippets"] = snippets
            response.update(snippet_coverage_fields(snippets, key_files))
            response["next_tool_suggestions"] = [
                "Answer from source_snippets; do not Read key_files paths",
            ]

        return response
    finally:
        store.close()
