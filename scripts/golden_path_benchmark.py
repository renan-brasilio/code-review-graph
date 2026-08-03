#!/usr/bin/env python3
"""Compare token cost: golden MCP path vs grep-and-read baseline.

Usage:
  uv run python scripts/golden_path_benchmark.py --repo /path/to/your-repo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_graph.token_benchmark import estimate_tokens
from code_review_graph.tools.context import get_minimal_context
from code_review_graph.tools.pipeline_trace import trace_pipeline
from code_review_graph.tools.review import get_review_context
from code_review_graph.tools.symbol_context import trace_symbol_context

TASK = (
    "How does sample record link generation work? What triggers it, "
    "which handlers are involved, and how do they connect to "
    "StSampleRecordUtility?"
)

HANDLER_SYMBOLS = [
    "StSampleRecordTriggerHandler",
    "StSampleFormTriggerHandler",
    "StSampleDocumentTriggerHandler",
    "StSampleRecordUtility",
]

READ_TARGETS = [
    "force-app/main/default/classes/StSampleRecordTriggerHandler.cls",
    "force-app/main/default/classes/StSampleFormTriggerHandler.cls",
    "force-app/main/default/classes/StSampleDocumentTriggerHandler.cls",
    "force-app/main/default/classes/StSampleRecordUtility.cls",
    "force-app/main/default/triggers/StSampleRecordTrigger.trigger",
]

_SOURCE_EXTS = (".cls", ".trigger", ".java", ".py")


def _json_tokens(obj: object) -> int:
    return estimate_tokens(json.dumps(obj, default=str))


def _grep_read_baseline(repo: Path, terms: list[str], top_k: int = 6) -> tuple[int, list[str]]:
    scores: dict[str, int] = {}
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _SOURCE_EXTS:
            continue
        rel = str(path.relative_to(repo))
        if any(skip in rel for skip in (".git", "node_modules", ".code-review-graph")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        score = sum(lower.count(t.lower()) for t in terms)
        if score:
            scores[rel] = score

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    total = 0
    files: list[str] = []
    for rel, _ in ranked:
        fp = repo / rel
        try:
            total += estimate_tokens(fp.read_text(encoding="utf-8", errors="replace"))
            files.append(rel)
        except OSError:
            pass
    return total, files


def _session_read_baseline(repo: Path) -> tuple[int, list[str]]:
    total = 0
    found: list[str] = []
    for rel in READ_TARGETS:
        fp = repo / rel
        if fp.is_file():
            total += estimate_tokens(fp.read_text(encoding="utf-8", errors="replace"))
            found.append(rel)
    return total, found


def _extract_terms(task: str) -> list[str]:
    ids = re.findall(r"\bSt[A-Z][A-Za-z0-9_]*\b", task)
    keywords = [
        w for w in re.findall(r"[A-Za-z_]{4,}", task.lower())
        if w not in {"does", "what", "which", "they", "work", "this", "that", "with", "from"}
    ]
    return list(dict.fromkeys(ids + keywords))


def run_golden_path(repo_root: str) -> dict:
    repo = Path(repo_root).resolve()
    minimal_steps = [
        ("get_minimal_context", lambda: get_minimal_context(task=TASK, repo_root=repo_root)),
        (
            "trace_pipeline",
            lambda: trace_pipeline(task=TASK, include_source=True, repo_root=repo_root),
        ),
    ]

    full_steps = minimal_steps + [
        (
            "trace_symbol_context (utility)",
            lambda: trace_symbol_context(
                target="StSampleRecordUtility",
                include_source=True,
                repo_root=repo_root,
            ),
        ),
        (
            "get_review_context (handlers)",
            lambda: get_review_context(
                target_symbols=HANDLER_SYMBOLS,
                include_source=True,
                max_lines_per_file=80,
                repo_root=repo_root,
            ),
        ),
    ]

    def _run_steps(steps: list) -> tuple[int, list[dict]]:
        total = 0
        calls: list[dict] = []
        for name, fn in steps:
            result = fn()
            tokens = _json_tokens(result)
            total += tokens
            calls.append({"tool": name, "status": result.get("status", "ok"), "tokens": tokens})
        return total, calls

    minimal_total, minimal_calls = _run_steps(minimal_steps)
    full_total, full_calls = _run_steps(full_steps)

    grep_terms = _extract_terms(TASK)
    grep_tokens, grep_files = _grep_read_baseline(repo, grep_terms)
    session_tokens, session_files = _session_read_baseline(repo)

    return {
        "repo": str(repo),
        "task": TASK,
        "minimal_golden_path": {
            "total_tokens": minimal_total,
            "calls": minimal_calls,
            "call_count": len(minimal_calls),
        },
        "full_golden_path": {
            "total_tokens": full_total,
            "calls": full_calls,
            "call_count": len(full_calls),
        },
        "grep_read_baseline": {
            "total_tokens": grep_tokens,
            "files": grep_files,
        },
        "session_style_reads": {
            "total_tokens": session_tokens,
            "files": session_files,
        },
        "savings_minimal_vs_session": (
            round(100 * (1 - minimal_total / session_tokens), 1) if session_tokens else None
        ),
        "savings_full_vs_session": (
            round(100 * (1 - full_total / session_tokens), 1) if session_tokens else None
        ),
        "savings_minimal_vs_grep": (
            round(100 * (1 - minimal_total / grep_tokens), 1) if grep_tokens else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden MCP path token benchmark")
    parser.add_argument(
        "--repo",
        required=True,
        help="Application repo root (must contain .code-review-graph/graph.db)",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON only")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".code-review-graph" / "graph.db").is_file():
        print(f"Missing graph: {repo / '.code-review-graph/graph.db'}", file=sys.stderr)
        return 1

    report = run_golden_path(str(repo))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    minimal = report["minimal_golden_path"]
    full = report["full_golden_path"]
    print(f"Repo: {report['repo']}\n")

    print("Minimal golden MCP path (recommended — 2 calls):")
    for call in minimal["calls"]:
        print(f"  - {call['tool']}: ~{call['tokens']} tokens ({call['status']})")
    print(f"  TOTAL: ~{minimal['total_tokens']} tokens\n")

    print("Full golden path (4 calls — redundant if trace_pipeline has snippets):")
    for call in full["calls"]:
        print(f"  - {call['tool']}: ~{call['tokens']} tokens ({call['status']})")
    print(f"  TOTAL: ~{full['total_tokens']} tokens\n")

    gr = report["grep_read_baseline"]
    print(f"Grep-and-read baseline (top {len(gr['files'])} files): ~{gr['total_tokens']} tokens")
    for f in gr["files"]:
        print(f"  - {f}")

    sr = report["session_style_reads"]
    print(f"\nExported-session style (6 handler/trigger files): ~{sr['total_tokens']} tokens")
    for f in sr["files"]:
        print(f"  - {f}")

    if report["savings_minimal_vs_session"] is not None:
        print(f"\nSavings (minimal path) vs session reads: {report['savings_minimal_vs_session']}%")
    if report["savings_full_vs_session"] is not None:
        print(f"Savings (full path) vs session reads: {report['savings_full_vs_session']}%")
    if report["savings_minimal_vs_grep"] is not None:
        print(f"Savings (minimal path) vs grep baseline: {report['savings_minimal_vs_grep']}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
