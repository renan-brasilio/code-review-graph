"""Cross-tool persistent memory for AI coding assistants.

Exposed over MCP so a memory saved from one client (Cursor, Claude Code, or
any other MCP client connected to this server) is recalled identically from
any other — unlike each tool's own built-in memory (Claude Code project
memory, Cursor rules files), which doesn't follow you across tools.

Not to be confused with ``memory.py``, which saves Q&A logs for
re-ingestion into the code graph — a different concept entirely from
"remember this fact" persistent agent memory.

Two independent stores, identical schema:

- **repo**: ``<repo>/.code-review-graph/memory.db`` — project-specific
  facts ("this codebase uses the repository pattern for data access").
- **global**: ``~/.code-review-graph/global_memory.db`` — personal
  preferences that apply everywhere ("I prefer tabs over spaces").

``recall_memories`` can query either store or both.

Embedding is opt-in (``embed=True``), never automatic — loading a local
model on first use, or calling a configured cloud provider, is exactly
the kind of silent cost/latency the rest of this project's embedding
code is deliberately careful to avoid outside an explicit `embed` step.
Without it, recall is a fast, free, always-available keyword match.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .embeddings import _cosine_similarity, _decode_vector, _encode_vector, get_provider

logger = logging.getLogger(__name__)

_VALID_SCOPES = ("repo", "global")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    vector BLOB,
    vector_provider TEXT
)
"""


def global_memory_path() -> Path:
    """Location of the global (cross-repo) memory store."""
    return Path.home() / ".code-review-graph" / "global_memory.db"


def repo_memory_path(repo_root: Path) -> Path:
    """Location of the per-repo memory store."""
    return Path(repo_root) / ".code-review-graph" / "memory.db"


def _db_path_for_scope(scope: str, repo_root: Optional[Path]) -> Path:
    if scope == "global":
        return global_memory_path()
    if repo_root is None:
        raise ValueError("repo_root is required when scope='repo'")
    return repo_memory_path(repo_root)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA_SQL)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row, scope: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content": row["content"],
        "category": row["category"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "scope": scope,
    }


def _embed_text(text: str, provider_name: Optional[str]) -> tuple[Optional[bytes], Optional[str]]:
    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        logger.warning("Memory embedding provider unavailable: %s", exc)
        return None, None
    if provider is None:
        return None, None
    try:
        vector = provider.embed([text])[0]
    except Exception as exc:  # noqa: BLE001 - embedding is best-effort
        logger.warning("Failed to embed memory content: %s", exc)
        return None, None
    return _encode_vector(vector), provider.name


def save_memory(
    content: str,
    scope: str = "repo",
    category: Optional[str] = None,
    repo_root: Optional[Path] = None,
    embed: bool = False,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Save a new memory.

    Args:
        content: The fact/preference to remember.
        scope: "repo" (this project only) or "global" (every project).
        category: Optional free-form label (e.g. "preference", "convention").
        repo_root: Required when scope="repo".
        embed: Compute a semantic embedding for this memory now, so
            ``recall_memories(..., embed=True)`` can rank it by similarity
            instead of keyword match. Off by default — see module docstring.
        provider: Embedding provider name, forwarded to ``get_provider()``.
            Only used when embed=True.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("Memory content cannot be empty")
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {_VALID_SCOPES}")

    db_path = _db_path_for_scope(scope, repo_root)
    conn = _connect(db_path)
    try:
        now = time.time()
        vector_blob, provider_name = (None, None)
        if embed:
            vector_blob, provider_name = _embed_text(text, provider)

        cursor = conn.execute(
            "INSERT INTO agent_memories "
            "(content, category, created_at, updated_at, vector, vector_provider) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (text, category, now, now, vector_blob, provider_name),
        )
        conn.commit()
        return {
            "id": cursor.lastrowid,
            "content": text,
            "category": category,
            "scope": scope,
            "created_at": now,
            "embedded": vector_blob is not None,
        }
    finally:
        conn.close()


def recall_memories(
    query: str = "",
    scope: str = "both",
    limit: int = 10,
    repo_root: Optional[Path] = None,
    embed: bool = False,
    provider: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Recall memories matching *query*.

    Args:
        query: Text to match. Empty returns the most recently updated
            memories in scope, unranked.
        scope: "repo", "global", or "both" (default — checks both stores
            and merges results).
        limit: Max memories to return.
        repo_root: Required when scope is "repo" or "both".
        embed: Rank by semantic similarity instead of keyword substring
            match. Loads an embedding provider (local model download on
            first use, or a configured cloud provider) — same cost as
            `code-review-graph embed`. Memories saved without embed=True
            fall back to a binary keyword-match score when ranked this way.
        provider: Embedding provider name. Only used when embed=True.
    """
    if scope not in ("repo", "global", "both"):
        raise ValueError('scope must be "repo", "global", or "both"')

    rows: list[tuple[dict[str, Any], Optional[bytes]]] = []
    scopes_to_check = ("repo", "global") if scope == "both" else (scope,)
    for one_scope in scopes_to_check:
        if one_scope == "repo" and repo_root is None:
            continue
        db_path = _db_path_for_scope(one_scope, repo_root)
        if not db_path.is_file():
            continue
        conn = _connect(db_path)
        try:
            for row in conn.execute("SELECT * FROM agent_memories").fetchall():
                rows.append((_row_to_dict(row, one_scope), row["vector"]))
        finally:
            conn.close()

    if not rows:
        return []

    query_text = query.strip()
    query_lower = query_text.lower()

    if embed and query_text:
        try:
            embed_provider = get_provider(provider)
        except ValueError as exc:
            logger.warning("Memory recall embedding provider unavailable: %s", exc)
            embed_provider = None
        if embed_provider is not None:
            query_vector = embed_provider.embed_query(query_text)
            scored = []
            for mem, vector_blob in rows:
                if vector_blob:
                    similarity = _cosine_similarity(query_vector, _decode_vector(vector_blob))
                else:
                    similarity = 0.5 if query_lower in mem["content"].lower() else 0.0
                scored.append((similarity, mem))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [mem for _, mem in scored[:limit]]

    memories = [mem for mem, _ in rows]
    if query_lower:
        memories = [
            mem for mem in memories
            if query_lower in mem["content"].lower()
            or (mem.get("category") and query_lower in mem["category"].lower())
        ]
    memories.sort(key=lambda mem: mem["updated_at"], reverse=True)
    return memories[:limit]


def list_agent_memories(
    scope: str = "both",
    repo_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """List every memory in scope, most recently updated first."""
    return recall_memories(query="", scope=scope, limit=10_000, repo_root=repo_root)


def forget_agent_memory(
    memory_id: int,
    scope: str,
    repo_root: Optional[Path] = None,
) -> bool:
    """Delete a memory by id. Returns True if a row was deleted."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {_VALID_SCOPES}")
    db_path = _db_path_for_scope(scope, repo_root)
    if not db_path.is_file():
        return False
    conn = _connect(db_path)
    try:
        cursor = conn.execute("DELETE FROM agent_memories WHERE id = ?", (memory_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
