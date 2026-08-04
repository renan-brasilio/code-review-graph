"""Tools: save_memory, recall_memories, list_memories, forget_memory.

Cross-tool persistent memory — see ``agent_memory.py`` for the full design
rationale (why it's a separate store from the code graph, why embedding is
opt-in, and how repo vs. global scope works).
"""

from __future__ import annotations

from typing import Any

from ..agent_memory import (
    forget_agent_memory,
    list_agent_memories,
    recall_memories,
    save_memory,
)
from ._common import _resolve_root


def save_memory_func(
    content: str,
    scope: str = "repo",
    category: str | None = None,
    repo_root: str | None = None,
    embed: bool = False,
) -> dict[str, Any]:
    """Save a fact or preference for any MCP client to recall later.

    Args:
        content: The fact/preference to remember.
        scope: "repo" (this project only, default) or "global" (every
            project you work in with any MCP client connected to this
            server).
        category: Optional free-form label (e.g. "preference", "convention").
        repo_root: Repository root path. Auto-detected if omitted.
            Required (implicitly, via auto-detection) when scope="repo".
        embed: Compute a semantic embedding now so a later
            recall_memories(embed=True) call can rank this memory by
            similarity instead of keyword match. Off by default — loads
            an embedding provider (local model download on first use, or
            a configured cloud provider).
    """
    root = _resolve_root(repo_root) if scope == "repo" else None
    return save_memory(
        content=content, scope=scope, category=category, repo_root=root, embed=embed,
    )


def recall_memories_func(
    query: str = "",
    scope: str = "both",
    limit: int = 10,
    repo_root: str | None = None,
    embed: bool = False,
) -> dict[str, Any]:
    """Recall saved memories matching *query*.

    Args:
        query: Text to match. Empty returns the most recently updated
            memories in scope, unranked.
        scope: "repo", "global", or "both" (default — checks both stores).
        limit: Max memories to return.
        repo_root: Repository root path. Auto-detected if omitted.
        embed: Rank by semantic similarity instead of keyword substring
            match. Loads an embedding provider — same cost as
            `code-review-graph embed`.
    """
    root = _resolve_root(repo_root) if scope in ("repo", "both") else None
    memories = recall_memories(
        query=query, scope=scope, limit=limit, repo_root=root, embed=embed,
    )
    return {"memories": memories, "count": len(memories)}


def list_memories_func(
    scope: str = "both",
    repo_root: str | None = None,
) -> dict[str, Any]:
    """List every saved memory in scope, most recently updated first."""
    root = _resolve_root(repo_root) if scope in ("repo", "both") else None
    memories = list_agent_memories(scope=scope, repo_root=root)
    return {"memories": memories, "count": len(memories)}


def forget_memory_func(
    memory_id: int,
    scope: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Delete a saved memory by id (from list_memories/recall_memories output).

    Args:
        memory_id: The memory's id, from a prior list/recall call.
        scope: "repo" or "global" — must match the memory's actual scope.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    root = _resolve_root(repo_root) if scope == "repo" else None
    deleted = forget_agent_memory(memory_id=memory_id, scope=scope, repo_root=root)
    return {"deleted": deleted, "id": memory_id, "scope": scope}
