"""Multi-hop pipeline tracing for architecture questions (token-efficient)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..flows import get_flow_by_id, get_flows
from ..graph import GraphNode, node_to_dict
from ._common import _get_store, snippet_coverage_fields
from ._resolve import (
    collect_callees,
    collect_callers,
    collect_class_callees,
    collect_class_callers,
    compact_node,
    extract_symbol_names,
    partition_production_and_tests,
    resolve_query_target,
)
from .symbol_context import _relative_path, _snippet_for_nodes

logger = logging.getLogger(__name__)

_LIFECYCLE_ORDER = {
    "generateSampleRecords": 10,
    "generateSampleRecordLinks": 20,
    "updateSampleRecordLinks": 30,
    "andFinally": 40,
    "andFinallyExtended": 41,
    "afterInsert": 50,
    "afterUpdate": 51,
    "beforeInsert": 52,
}

_PIPELINE_METHODS = frozenset(_LIFECYCLE_ORDER)

_PIPELINE_KEYWORDS = (
    "trigger", "handler", "how", "work", "flow", "pipeline", "connect",
    "generation", "record", "link",
)

_NOISE_METHODS = frozenset({
    "add", "containsKey", "generateKey", "get",
    "getName", "getUserId", "insert", "dispatch",
})


def _is_trigger_node(entry: dict) -> bool:
    fp = (entry.get("file_path") or "").replace("\\", "/")
    return fp.endswith(".trigger") or entry.get("kind") == "Trigger"


def _step_sort_key(entry: dict) -> tuple:
    name = entry.get("name") or ""
    if _is_trigger_node(entry):
        return (0, name)
    if name in _LIFECYCLE_ORDER:
        return (1, _LIFECYCLE_ORDER[name], entry.get("parent_name") or "")
    if entry.get("relationship") == "invoked_by":
        return (2, name)
    if entry.get("kind") == "Class":
        return (3, name)
    return (4, entry.get("parent_name") or "", name)


def _task_derived_terms(task: str) -> list[str]:
    """Method names implied by natural-language architecture questions."""
    terms: list[str] = []
    tl = task.lower()
    if "link" in tl and "record" in tl:
        terms.extend([
            "generateSampleRecordLinks",
            "updateSampleRecordLinks",
        ])
    if "record" in tl:
        terms.append("generateSampleRecords")
    return terms


def _task_is_pipeline_question(task: str) -> bool:
    tl = task.lower()
    return any(kw in tl for kw in _PIPELINE_KEYWORDS)


def _resolve_anchor(store, root: Path, task: str, anchor: str) -> GraphNode | None:
    if anchor.strip():
        return resolve_query_target(store, root, anchor.strip(), "callers_of")
    for symbol in extract_symbol_names(task):
        node = resolve_query_target(store, root, symbol, "callers_of")
        if node:
            return node
    if _task_is_pipeline_question(task):
        for term in _task_derived_terms(task):
            node = resolve_query_target(store, root, term, "callers_of")
            if node:
                return node
    return None


def _discover_seed_nodes(
    store,
    root: Path,
    task: str,
    anchor: str,
) -> list[GraphNode]:
    """Collect pipeline seeds from anchor, class names, and task keywords."""
    seeds: list[GraphNode] = []
    seen_ids: set[int] = set()

    def add(node: GraphNode | None) -> None:
        if not node or node.is_test:
            return
        if node.id is not None and node.id in seen_ids:
            return
        if node.id is not None:
            seen_ids.add(node.id)
        seeds.append(node)

    if anchor.strip():
        add(resolve_query_target(store, root, anchor.strip(), "callers_of"))

    for symbol in extract_symbol_names(task):
        add(resolve_query_target(store, root, symbol, "callers_of"))

    if _task_is_pipeline_question(task):
        for term in _task_derived_terms(task):
            node = resolve_query_target(store, root, term, "callers_of")
            add(node)
            if node and node.parent_name:
                add(resolve_query_target(store, root, node.parent_name, "callers_of"))

    if not seeds:
        add(_resolve_anchor(store, root, task, anchor))

    return seeds


def _trigger_for_handler(store, handler: GraphNode) -> GraphNode | None:
    """Find a trigger that invokes the handler class."""
    targets = (handler.qualified_name, handler.name)
    for target in targets:
        for edge in store.get_edges_by_target(target):
            if edge.kind != "INVOKES":
                continue
            trigger = store.get_node(edge.source_qualified)
            if trigger and not trigger.is_test:
                fp = (trigger.file_path or "").replace("\\", "/")
                if fp.endswith(".trigger") or trigger.kind == "Trigger":
                    return trigger
        for edge in store.search_edges_by_target_name(handler.name, kind="INVOKES"):
            trigger = store.get_node(edge.source_qualified)
            if trigger and not trigger.is_test:
                return trigger
    return None


def _expand_from_seed(
    store,
    root: Path,
    seed: GraphNode,
    seen_qn: set[str],
) -> list[dict]:
    """Expand one seed into handlers, triggers, and lifecycle methods."""
    collected: list[dict] = []

    def add_node(node: GraphNode | None, **extra: Any) -> None:
        if not node or node.is_test:
            return
        entry = node_to_dict(node)
        qn = entry.get("qualified_name")
        if not qn or qn in seen_qn:
            return
        seen_qn.add(qn)
        entry.update(extra)
        collected.append(entry)

    def add_handler_chain(handler: GraphNode | None) -> None:
        if not handler or handler.is_test:
            return
        add_node(handler)
        add_node(_trigger_for_handler(store, handler))

    add_node(seed)
    if seed.parent_name:
        add_handler_chain(resolve_query_target(store, root, seed.parent_name, "callers_of"))

    if seed.kind == "Class":
        callers, _ = collect_class_callers(store, seed)
    else:
        callers, _, _ = collect_callers(store, seed)

    prod_callers, _ = partition_production_and_tests(callers)
    for entry in prod_callers[:4]:
        qn = entry.get("qualified_name")
        if qn and qn not in seen_qn:
            seen_qn.add(qn)
            collected.append({**entry, "relationship": "invoked_by"})
        parent_name = entry.get("parent_name")
        if parent_name:
            add_handler_chain(resolve_query_target(store, root, parent_name, "callers_of"))

    if seed.kind == "Class":
        _, callees = collect_class_callees(store, seed)
    elif seed.kind == "Function":
        _, callees = collect_callees(store, seed)
    else:
        callees = []

    prod_callees, _ = partition_production_and_tests(callees)
    for entry in prod_callees:
        if entry.get("name") in _PIPELINE_METHODS:
            qn = entry.get("qualified_name")
            if qn and qn not in seen_qn:
                seen_qn.add(qn)
                collected.append(entry)

    return collected


def _expand_multi_phase(
    store,
    root: Path,
    seeds: list[GraphNode],
) -> list[dict]:
    seen_qn: set[str] = set()
    collected: list[dict] = []
    for seed in seeds:
        collected.extend(_expand_from_seed(store, root, seed, seen_qn))
    return collected


def _is_noise_step(entry: dict) -> bool:
    name = entry.get("name") or ""
    return name in _NOISE_METHODS


def _is_pipeline_step(entry: dict, task: str) -> bool:
    if _is_noise_step(entry):
        return False
    if _is_trigger_node(entry):
        return True
    name = entry.get("name") or ""
    parent = entry.get("parent_name") or ""
    kind = entry.get("kind") or ""
    if name in _PIPELINE_METHODS:
        return True
    if kind == "Class" and ("Handler" in name or "Utility" in name):
        if parent.endswith("TriggerHandler"):
            return True
    if name in _LIFECYCLE_ORDER and parent.endswith("TriggerHandler"):
        return True
    if parent.endswith("TriggerHandler") and name in ("andFinally", "andFinallyExtended"):
        tl = task.lower()
        if any(k in tl for k in ("handler", "trigger", "connect", "how", "work", "pipeline")):
            return True
    return False


def _filter_pipeline_steps(steps: list[dict], task: str) -> list[dict]:
    filtered = [s for s in steps if _is_pipeline_step(s, task)]
    if len(filtered) >= 3:
        return filtered
    return [s for s in steps if not _is_noise_step(s)]


def _find_best_flow(store, symbols: list[str], node_ids: set[int]) -> dict | None:
    symbol_lower = {s.lower() for s in symbols}
    best: dict | None = None
    best_score = 0

    for flow_meta in get_flows(store, limit=80):
        detail = get_flow_by_id(store, flow_meta["id"])
        if not detail:
            continue
        steps = detail.get("steps") or []
        score = 0
        for step in steps:
            if step.get("node_id") in node_ids:
                score += 2
            name = (step.get("name") or "").lower()
            parent = (step.get("qualified_name") or "").lower()
            if any(sym in name or sym in parent for sym in symbol_lower):
                score += 1
        if score > best_score:
            best_score = score
            best = detail

    return best if best_score >= 2 else None


def _flow_covers_pipeline(flow_detail: dict, task: str) -> bool:
    """True when a detected flow spans multiple pipeline stages, not a single callee chain."""
    step_names = {s.get("name") or "" for s in flow_detail.get("steps") or []}
    pipeline_hits = step_names & _PIPELINE_METHODS
    if len(pipeline_hits) >= 2:
        return True
    flow_name = (flow_detail.get("name") or "").lower()
    if "record" in flow_name and pipeline_hits:
        return True
    return False


def _choose_pipeline_steps(
    flow_detail: dict | None,
    related: list[dict],
    max_steps: int,
    task: str,
) -> tuple[list[dict], str, str | None]:
    """Prefer graph expansion when it covers more handlers than a partial flow."""
    synthetic = _steps_synthetic(related, max_steps, task)
    if not flow_detail or not _flow_covers_pipeline(flow_detail, task):
        return synthetic, "graph_expansion", None

    from_flow = _steps_from_flow(flow_detail, max_steps, task)

    def _handler_keys(steps: list[dict]) -> set[str]:
        keys: set[str] = set()
        for step in steps:
            parent = step.get("parent_name") or ""
            name = step.get("name") or ""
            if parent.endswith("Handler") or parent.endswith("Utility"):
                keys.add(parent)
            elif step.get("kind") == "Class":
                keys.add(name)
            elif _is_trigger_node(step):
                keys.add(name)
        return keys

    if len(_handler_keys(synthetic)) > len(_handler_keys(from_flow)):
        return synthetic, "graph_expansion", None
    if len(synthetic) > len(from_flow):
        return synthetic, "graph_expansion", None
    return from_flow, "detected_flow", flow_detail.get("name")


def _steps_from_flow(detail: dict, max_steps: int, task: str) -> list[dict]:
    steps: list[dict] = []
    for step in detail.get("steps") or []:
        entry = {
            "name": step.get("name"),
            "kind": step.get("kind"),
            "file_path": step.get("file"),
            "qualified_name": step.get("qualified_name"),
            "line_start": step.get("line_start"),
            "line_end": step.get("line_end"),
            "source": "flow",
        }
        if _is_trigger_node(entry):
            steps.append(entry)
            continue
        prod, _ = partition_production_and_tests([entry])
        if prod:
            steps.append(entry)
        if len(steps) >= max_steps:
            break
    return _filter_pipeline_steps(steps, task)[:max_steps]


def _steps_synthetic(nodes: list[dict], max_steps: int, task: str) -> list[dict]:
    deduped: dict[str, dict] = {}
    for entry in nodes:
        qn = entry.get("qualified_name") or entry.get("name") or ""
        if qn not in deduped:
            tagged = dict(entry)
            tagged.setdefault("source", "graph_expansion")
            deduped[qn] = tagged
    ordered = sorted(deduped.values(), key=_step_sort_key)
    filtered = _filter_pipeline_steps(ordered, task)
    return filtered[:max_steps]


def trace_pipeline(
    task: str = "",
    anchor: str = "",
    include_source: bool = False,
    max_steps: int = 8,
    max_source_files: int = 4,
    max_lines_per_file: int = 40,
    repo_root: str | None = None,
    detail_level: str = "minimal",
) -> dict[str, Any]:
    """Trace an ordered handler/trigger pipeline for multi-hop architecture questions.

    Combines flow detection, caller/callee expansion, and optional snippets so
    agents can answer "how does X work end-to-end?" without reading many files.
    """
    store, root = _get_store(repo_root)
    try:
        symbols = extract_symbol_names(f"{task} {anchor}".strip())
        seeds = _discover_seed_nodes(store, root, task, anchor)
        if not seeds:
            return {
                "status": "not_found",
                "summary": (
                    "No anchor symbol found. Pass anchor=ClassName or "
                    "include class names in task."
                ),
                "next_tool_suggestions": [
                    "semantic_search_nodes",
                    "trace_symbol_context",
                ],
            }

        anchor_node = seeds[0]
        related = _expand_multi_phase(store, root, seeds)
        node_ids = {n.get("id") for n in related if n.get("id") is not None}
        flow_detail = _find_best_flow(store, symbols or [anchor_node.name], node_ids)
        pipeline_steps, pipeline_source, flow_name = _choose_pipeline_steps(
            flow_detail, related, max_steps, task,
        )

        key_files: list[str] = []
        seen_files: set[str] = set()
        for step in pipeline_steps:
            fp = step.get("file_path")
            if fp and fp not in seen_files:
                seen_files.add(fp)
                key_files.append(_relative_path(root, fp))

        handler_names = [
            s["name"] for s in pipeline_steps
            if s.get("kind") in ("Class", "Function", "Trigger") and s.get("name")
        ]
        summary_parts = [
            f"Pipeline ({pipeline_source}) — {len(seeds)} seed(s):",
            f"  {len(pipeline_steps)} step(s) across {len(key_files)} file(s)",
        ]
        if flow_name:
            summary_parts.append(f"  flow: {flow_name}")
        if handler_names:
            summary_parts.append(f"  chain: {' → '.join(handler_names[:10])}")

        response: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "anchor": compact_node(node_to_dict(anchor_node)),
            "pipeline_source": pipeline_source,
            "pipeline_steps": [compact_node(s) for s in pipeline_steps],
            "key_files": key_files[:10],
            "extracted_symbols": symbols[:8] or None,
            "seed_count": len(seeds),
            "next_tool_suggestions": [
                "Answer from this response; do not Grep/Read handler .cls files covered below",
            ],
        }

        if flow_name:
            response["flow_name"] = flow_name

        if detail_level != "minimal":
            response["related_nodes"] = [compact_node(n) for n in related[:12]]

        if include_source and pipeline_steps:
            snippet_cap = max_source_files
            if _task_is_pipeline_question(task) and len(key_files) > max_source_files:
                snippet_cap = min(len(key_files), max(max_source_files, 6))
            snippets = _snippet_for_nodes(
                root,
                pipeline_steps,
                snippet_cap,
                max_lines_per_file,
            )
            response["source_snippets"] = snippets
            response.update(snippet_coverage_fields(snippets, key_files))
            response["next_tool_suggestions"] = [
                "Answer from source_snippets; Grep only Flow/CMT/formula metadata gaps",
            ]

        return response
    finally:
        store.close()
