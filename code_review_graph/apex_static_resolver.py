"""Post-build Apex static call resolver.

After tree-sitter parsing, Apex CALLS edges for ``ClassName.methodName()``
often target the bare class name while ``extra.receiver`` and ``extra.method``
carry the real callee.  This module rewrites those edges to qualified method
nodes (``file::ClassName.methodName``) so ``callers_of`` on a method works.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_apex_static_calls(store: GraphStore) -> dict:
    """Resolve Apex static CALLS edges to qualified method targets.

    Safe to call multiple times — edges with ``apex_resolved`` in extra are
    skipped.

    Returns resolution counts for telemetry.
    """
    conn = store._conn

    apex_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'apex'"
        ).fetchall()
    }
    if not apex_files:
        return {"files_indexed": 0, "calls_resolved": 0, "calls_unresolved": 0}

    name_to_qual: dict[str, str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name FROM nodes "
        "WHERE kind = 'Class' AND language = 'apex'"
    ).fetchall():
        bare = row["name"]
        qual = row["qualified_name"]
        if bare not in name_to_qual or len(qual) < len(name_to_qual[bare]):
            name_to_qual[bare] = qual

    method_to_qual: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'apex' "
        "AND parent_name IS NOT NULL"
    ).fetchall():
        method_to_qual[(row["parent_name"], row["name"])] = row["qualified_name"]

    calls_rows = conn.execute(
        "SELECT id, source_qualified, target_qualified, extra, file_path "
        "FROM edges WHERE kind = 'CALLS'"
    ).fetchall()

    resolved = 0
    unresolved = 0

    for row in calls_rows:
        if row["file_path"] not in apex_files:
            continue

        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}

        if extra.get("apex_resolved"):
            continue

        receiver = extra.get("receiver")
        method_name = extra.get("method")
        if not receiver or not method_name:
            continue

        new_target = method_to_qual.get((receiver, method_name))
        if not new_target and receiver in name_to_qual:
            class_bare = name_to_qual[receiver].split("::")[-1]
            new_target = method_to_qual.get((class_bare, method_name))

        if not new_target:
            unresolved += 1
            continue

        extra["apex_resolved"] = True
        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
            (new_target, json.dumps(extra), row["id"]),
        )
        resolved += 1
        logger.debug(
            "Apex static resolved: %s → %s (receiver=%s)",
            row["source_qualified"],
            new_target,
            receiver,
        )

    if resolved:
        conn.commit()

    logger.info(
        "Apex static resolver: resolved %d CALLS edges (%d unresolved) in %d files",
        resolved,
        unresolved,
        len(apex_files),
    )
    return {
        "files_indexed": len(apex_files),
        "calls_resolved": resolved,
        "calls_unresolved": unresolved,
    }
