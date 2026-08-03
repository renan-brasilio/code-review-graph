"""Shared node-resolution helpers for graph query tools."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import TYPE_CHECKING, Optional

from ..graph import GraphNode, _sanitize_name, edge_to_dict, node_to_dict

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..graph import GraphStore

_CLASSLIKE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
_HANDLER_SUFFIXES = ("Handler", "Utility", "Service", "Controller", "Trigger")
_SYMBOL_IN_TASK = re.compile(
    r"\b(St[A-Z][A-Za-z0-9_]*|[A-Z][a-z][A-Za-z0-9]*(?:"
    + "|".join(_HANDLER_SUFFIXES)
    + r"))\b"
)

# Sitetracker's own managed-package namespace aliases. Not customer data —
# this is the company's own product namespace, same as already named in
# docs/SALESFORCE.md.
MANAGED_PACKAGE_NAMESPACES = ("sitetracker", "strk")


def managed_package_namespace_hint(store: "GraphStore", target: str) -> Optional[str]:
    """Return a namespace alias if *target* looks like a Sitetracker
    managed-package symbol this repo cannot resolve locally by design.

    A customer repo only contains its own org's code — the package's own
    Apex/metadata source is never present, so a graph miss here is not a
    dead end to retry with Grep/Read; it's a signal to switch tools
    entirely. Two independent, unambiguous signals (no naming-convention
    guessing, since customer code can legitimately share the "St" prefix
    convention):

    1. The target string itself carries a namespace marker — metadata form
       (``sitetracker__Field__c``) or Apex code form (``sitetracker.Class``).
    2. An Apex ``CALLS`` edge in this repo has ``extra.receiver`` set to the
       namespace alias and matches the queried name — i.e. code in this
       repo genuinely calls into the package under that name.
    """
    bare = target.split("::")[-1].split(".")[-1]
    for ns in MANAGED_PACKAGE_NAMESPACES:
        if bare.startswith(f"{ns}__") or f"{ns}." in target:
            return ns

    try:
        rows = store._conn.execute(
            "SELECT extra FROM edges WHERE kind = 'CALLS' AND ("
            + " OR ".join("extra LIKE ?" for _ in MANAGED_PACKAGE_NAMESPACES)
            + ")",
            tuple(f'%"receiver": "{ns}"%' for ns in MANAGED_PACKAGE_NAMESPACES),
        ).fetchall()
    except sqlite3.OperationalError:
        logger.debug("managed_package_namespace_hint: edges table not queryable")
        return None

    for row in rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        receiver = extra.get("receiver")
        if receiver in MANAGED_PACKAGE_NAMESPACES and (
            extra.get("method") == bare or receiver == bare
        ):
            return receiver
    return None


def managed_package_not_found(store: "GraphStore", target: str) -> Optional[dict]:
    """A ``not_found``-shaped response redirecting to strk-mcp, or None."""
    ns = managed_package_namespace_hint(store, target)
    if not ns:
        return None
    return {
        "status": "not_found",
        "summary": (
            f"'{target}' is not in this repo's graph — it looks like a "
            f"Sitetracker managed-package symbol (namespace '{ns}'). This "
            "repo only indexes the org's own code; the package's own "
            "Apex/metadata source is never here, so further Grep/Read in "
            "this repo will not find it."
        ),
        "managed_package_namespace": ns,
        "next_tool_suggestions": [
            "strk-mcp (strk_resolve_tag, then strk_repo_structure/strk_grep/"
            "strk_read_file) for the package source — do not Grep/Read this "
            "repo for it",
        ],
    }


def extract_symbol_names(text: str) -> list[str]:
    """Pull likely Apex/Java class names from natural language."""
    seen: set[str] = set()
    names: list[str] = []
    for match in _SYMBOL_IN_TASK.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _is_likely_class_name(name: str) -> bool:
    return bool(_CLASSLIKE.match(name)) and "." not in name and "::" not in name


def _pick_best_candidate(
    candidates: list[GraphNode],
    target: str,
    pattern: str,
) -> GraphNode:
    """Resolve ambiguous name search to the most useful node."""
    bare = target.split("::")[-1]
    if "." in bare:
        cls_part, method_part = bare.rsplit(".", 1)
        method_matches = [
            c for c in candidates
            if c.kind == "Function" and c.name == method_part
            and (c.parent_name == cls_part or cls_part in (c.parent_name or ""))
        ]
        if len(method_matches) == 1:
            return method_matches[0]
        if method_matches:
            non_test = [c for c in method_matches if not c.is_test]
            return (non_test or method_matches)[0]

    exact = [c for c in candidates if c.name == bare]
    if len(exact) == 1:
        return exact[0]

    if pattern in ("callers_of", "callees_of", "tests_for", "inheritors_of"):
        if _is_likely_class_name(bare):
            classes = [c for c in candidates if c.kind == "Class" and not c.is_test]
            if classes:
                apex = [c for c in classes if c.language == "apex"]
                return (apex or classes)[0]
        if pattern == "callers_of":
            functions = [c for c in candidates if c.kind == "Function" and not c.is_test]
            if len(functions) == 1:
                return functions[0]

    non_test = [c for c in candidates if not c.is_test]
    if non_test:
        return non_test[0]
    return candidates[0]


def resolve_query_target(
    store: GraphStore,
    root,
    target: str,
    pattern: str,
) -> GraphNode | None:
    """Resolve a query target to a single graph node when possible."""
    node = store.get_node(target)
    if not node:
        node = store.get_node(str(root / target))
    bare = target.split("::")[-1]
    if not node and "." in bare and "::" not in target:
        cls_part, method_part = bare.rsplit(".", 1)
        method_candidates = [
            c for c in store.search_nodes(method_part, limit=20)
            if c.kind == "Function" and c.parent_name == cls_part
        ]
        if len(method_candidates) == 1:
            return method_candidates[0]
        if method_candidates:
            return _pick_best_candidate(method_candidates, target, pattern)
    if not node:
        candidates = store.search_nodes(bare, limit=10)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return _pick_best_candidate(candidates, target, pattern)
    return node


def _is_test_node(entry: dict) -> bool:
    """Heuristic: classify test nodes to deprioritize in discovery results."""
    if entry.get("is_test"):
        return True
    name = entry.get("name") or ""
    file_path = entry.get("file_path") or ""
    parent = entry.get("parent_name") or ""
    if name.endswith("Test") or parent.endswith("Test"):
        return True
    if name.startswith("test") and len(name) > 4:
        return True
    normalized = file_path.replace("\\", "/")
    if normalized.endswith("Test.cls") or "/test/" in normalized.lower():
        return True
    return False


def partition_production_and_tests(
    results: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Split query results into production vs test nodes."""
    production: list[dict] = []
    tests: list[dict] = []
    for entry in results:
        if _is_test_node(entry):
            tests.append(entry)
        else:
            production.append(entry)
    return production, tests


def compact_node(entry: dict) -> dict:
    """Token-efficient node projection for MCP responses."""
    return {
        k: entry[k]
        for k in (
            "name", "kind", "file_path", "qualified_name",
            "parent_name", "line_start", "line_end", "via_method", "match_tier",
            "indirect", "inferred_by", "target_resolution",
        )
        if k in entry and entry[k] is not None
    }


def collect_callers(
    store: GraphStore,
    node: GraphNode,
    seen_sources: set[str] | None = None,
) -> tuple[list[dict], list[dict], str | None]:
    """Collect callers for a function/class node with Apex fallbacks."""
    if seen_sources is None:
        seen_sources = set()

    results: list[dict] = []
    edges_out: list[dict] = []
    match_tier: str | None = None
    qn = node.qualified_name

    for edge in store.iter_edges_by_target(qn):
        if edge.kind not in ("CALLS", "INVOKES"):
            continue
        if edge.source_qualified in seen_sources:
            continue
        seen_sources.add(edge.source_qualified)
        caller = store.get_node(edge.source_qualified)
        if caller:
            results.append(node_to_dict(caller))
            edges_out.append(edge_to_dict(edge))

    # A C++ overload set deliberately keeps the target bare. Its candidates
    # support disambiguation, but do not prove that any one exact overload
    # was called, so skip the bare-name fallback entirely for those.
    cpp_overload_count = (
        store.count_nodes_by_name(node.name, language="cpp", kinds=("Function", "Test"))
        if node.language == "cpp"
        else 0
    )
    for edge in store.iter_edges_by_target_name(node.name, language=node.language or None):
        if edge.kind not in ("CALLS", "INVOKES"):
            continue
        if (
            "ambiguous_targets" in edge.extra
            or "unresolved_targets" in edge.extra
            or (node.language == "cpp" and edge.extra.get("receiver"))
        ):
            continue
        if cpp_overload_count > 1:
            continue
        if edge.source_qualified in seen_sources:
            continue
        seen_sources.add(edge.source_qualified)
        caller = store.get_node(edge.source_qualified)
        if caller:
            entry = node_to_dict(caller)
            entry["target_resolution"] = "unresolved"
            results.append(entry)
            edges_out.append(edge_to_dict(edge))

    if not results and node.kind == "Function" and node.parent_name:
        parent_qn = f"{node.file_path}::{node.parent_name}"
        for class_target in (parent_qn, node.parent_name):
            for edge in store.iter_edges_by_target(class_target):
                if edge.kind not in ("CALLS", "INVOKES"):
                    continue
                if edge.source_qualified in seen_sources:
                    continue
                seen_sources.add(edge.source_qualified)
                caller = store.get_node(edge.source_qualified)
                if caller:
                    entry = node_to_dict(caller)
                    entry["match_tier"] = "parent_class_fallback"
                    results.append(entry)
                    edges_out.append(edge_to_dict(edge))
        if results:
            match_tier = "parent_class_fallback"

    return results, edges_out, match_tier


def collect_class_callers(
    store: GraphStore,
    class_node: GraphNode,
) -> tuple[list[dict], list[dict]]:
    """Aggregate callers across all methods on a class."""
    seen_sources: set[str] = set()
    results: list[dict] = []
    edges_out: list[dict] = []

    direct, direct_edges, _ = collect_callers(store, class_node, seen_sources)
    results.extend(direct)
    edges_out.extend(direct_edges)

    if results:
        return results, edges_out

    methods = store.get_nodes_by_parent(class_node.name, class_node.file_path)
    lifecycle_first = (
        "andFinally", "andFinallyExtended", "afterInsert", "afterUpdate",
        "beforeInsert", "generateSampleRecordLinks", "generateSampleRecords",
        "updateSampleRecordLinks",
    )
    methods.sort(
        key=lambda m: (
            lifecycle_first.index(m.name) if m.name in lifecycle_first else 99,
            m.name,
        )
    )

    for method in methods:
        if method.kind not in ("Function", "Test"):
            continue
        method_results, method_edges, _ = collect_callers(store, method, seen_sources)
        for entry in method_results:
            tagged = dict(entry)
            tagged["via_method"] = method.name
            tagged.setdefault("match_tier", "class_method_aggregation")
            results.append(tagged)
        edges_out.extend(method_edges)

    return results, edges_out


def collect_callees(
    store: GraphStore,
    node: GraphNode,
) -> tuple[list[dict], list[dict]]:
    """Collect callees / invokees for a node."""
    results: list[dict] = []
    edges_out: list[dict] = []
    seen_targets: set[str] = set()
    qn = node.qualified_name

    for edge in store.iter_edges_by_source(qn):
        if edge.kind not in ("CALLS", "INVOKES"):
            continue
        if edge.target_qualified in seen_targets:
            continue
        seen_targets.add(edge.target_qualified)
        callee = store.get_node(edge.target_qualified)
        if callee:
            results.append(node_to_dict(callee))
            edges_out.append(edge_to_dict(edge))
        elif (
            isinstance(edge.extra.get("ambiguous_targets"), list)
            or isinstance(edge.extra.get("unresolved_targets"), list)
            or "::" not in edge.target_qualified
            or node.language == "cpp"
        ):
            unresolved = (
                edge.extra.get("ambiguous_targets")
                or edge.extra.get("unresolved_targets")
            )
            entry: dict = {
                "kind": "Function",
                "name": edge.target_qualified,
                "qualified_name": edge.target_qualified,
            }
            if isinstance(unresolved, list):
                resolution = (
                    "ambiguous" if edge.extra.get("ambiguous_targets") else "unresolved"
                )
                entry["resolution"] = resolution
                entry["candidates"] = [
                    _sanitize_name(candidate)
                    for candidate in unresolved[:20]
                    if isinstance(candidate, str)
                ]
                candidate_count = edge.extra.get(f"{resolution}_target_count")
                if not isinstance(candidate_count, int):
                    candidate_count = len(unresolved)
                entry["candidate_count"] = candidate_count
                entry["candidates_truncated"] = bool(
                    edge.extra.get(f"{resolution}_targets_truncated")
                    or candidate_count > len(entry["candidates"])
                )
            results.append(entry)
            edges_out.append(edge_to_dict(edge))

    return results, edges_out


def collect_class_callees(
    store: GraphStore,
    class_node: GraphNode,
) -> tuple[list[dict], list[dict]]:
    """Aggregate callees from class methods, direct outbound calls (e.g. a
    trigger's own INVOKES edge to its handler), and inbound INVOKES (triggers).
    """
    seen_targets: set[str] = set()
    results: list[dict] = []
    edges_out: list[dict] = []

    direct, direct_edges = collect_callees(store, class_node)
    for entry in direct:
        qn = entry.get("qualified_name")
        if qn:
            seen_targets.add(qn)
        results.append(entry)
    edges_out.extend(direct_edges)

    for edge in store.iter_edges_by_target(class_node.qualified_name):
        if edge.kind == "INVOKES" and edge.source_qualified not in seen_targets:
            seen_targets.add(edge.source_qualified)
            invoker = store.get_node(edge.source_qualified)
            if invoker:
                entry = node_to_dict(invoker)
                entry["relationship"] = "invoked_by"
                results.append(entry)
                edges_out.append(edge_to_dict(edge))

    if results:
        return results, edges_out

    methods = store.get_nodes_by_parent(class_node.name, class_node.file_path)
    for method in methods:
        if method.kind != "Function":
            continue
        method_results, method_edges = collect_callees(store, method)
        for entry in method_results:
            if entry.get("qualified_name") in seen_targets:
                continue
            qn = entry.get("qualified_name")
            if qn:
                seen_targets.add(qn)
            tagged = dict(entry)
            tagged["via_method"] = method.name
            results.append(tagged)
        edges_out.extend(method_edges)

    return results, edges_out
