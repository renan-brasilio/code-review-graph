"""Post-build resolver: LWC/Aura ``@salesforce/apex`` and ``@salesforce/schema``
imports.

Tree-sitter's generic JS/TS import handling stores these as bare, unresolved
strings (``@salesforce/apex/ContactController.getContacts``) — there is no
local file for a bare specifier to resolve against, so
``callers_of``/``references_to`` on the Apex method or Field it names finds
nothing, even though the LWC component genuinely calls or wires it.

LWC import syntax for these two module families is a fixed, simple default
import — ``import x from '@salesforce/apex/Class.method'`` — Salesforce's
own framework requires this exact form (no destructuring, no renaming via
braces), so a regex is reliable here without a second tree-sitter parse.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_APEX_IMPORT_RE = re.compile(
    r"""import\s+(\w+)\s+from\s+['"]@salesforce/apex/([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)['"]"""
)
_SCHEMA_IMPORT_RE = re.compile(
    r"""import\s+(\w+)\s+from\s+['"]@salesforce/schema/([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)['"]"""
)


def _rewrite_edge_target(
    conn, edge_id: int, extra_raw: str, new_target: str, resolved_key: str,
) -> None:
    try:
        extra = json.loads(extra_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        extra = {}
    extra[resolved_key] = True
    conn.execute(
        "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
        (new_target, json.dumps(extra), edge_id),
    )


def _resolve_imports_from(
    conn, file_path: str, bare_target: str, resolved_qn: str | None,
) -> bool:
    """Rewrite IMPORTS_FROM edges matching *bare_target*. Returns True if any changed."""
    if not resolved_qn:
        return False
    rows = conn.execute(
        "SELECT id, extra FROM edges "
        "WHERE kind = 'IMPORTS_FROM' AND file_path = ? AND target_qualified = ?",
        (file_path, bare_target),
    ).fetchall()
    for row in rows:
        _rewrite_edge_target(conn, row["id"], row["extra"], resolved_qn, "lwc_resolved")
    return bool(rows)


def _resolve_local_usages(conn, file_path: str, local_to_qn: dict[str, str]) -> int:
    """Rewrite same-file CALLS/REFERENCES edges targeting an imported local name."""
    if not local_to_qn:
        return 0
    rows = conn.execute(
        "SELECT id, target_qualified, extra FROM edges "
        "WHERE kind IN ('CALLS', 'REFERENCES') AND file_path = ?",
        (file_path,),
    ).fetchall()
    resolved = 0
    for row in rows:
        target_qn = local_to_qn.get(row["target_qualified"])
        if not target_qn:
            continue
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        if extra.get("lwc_resolved"):
            continue
        _rewrite_edge_target(conn, row["id"], row["extra"], target_qn, "lwc_resolved")
        resolved += 1
    return resolved


def resolve_lwc_apex_imports(store: GraphStore) -> dict:
    """Resolve ``@salesforce/apex``/``@salesforce/schema`` imports in JS/TS files."""
    empty = {
        "files_indexed": 0,
        "apex_imports_resolved": 0, "apex_imports_unresolved": 0,
        "apex_calls_resolved": 0,
        "schema_imports_resolved": 0, "schema_imports_unresolved": 0,
    }
    conn = store._conn

    js_files: list[str] = [
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM edges "
            "WHERE kind = 'IMPORTS_FROM' AND ("
            "target_qualified LIKE '@salesforce/apex/%' OR "
            "target_qualified LIKE '@salesforce/schema/%'"
            ")"
        ).fetchall()
    ]
    if not js_files:
        return empty

    method_qual: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'apex' "
        "AND parent_name IS NOT NULL"
    ).fetchall():
        method_qual[(row["parent_name"], row["name"])] = row["qualified_name"]

    field_qual: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind = 'Field' AND language = 'salesforce_metadata'"
    ).fetchall():
        field_qual[(row["parent_name"], row["name"])] = row["qualified_name"]

    apex_imports_resolved = 0
    apex_imports_unresolved = 0
    apex_calls_resolved = 0
    schema_imports_resolved = 0
    schema_imports_unresolved = 0

    for file_path in js_files:
        path = Path(file_path)
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Cannot read LWC/Aura file %s: %s", file_path, exc)
            continue

        local_to_method: dict[str, str] = {}
        for local_name, cls, method in _APEX_IMPORT_RE.findall(source):
            resolved_qn = method_qual.get((cls, method))
            bare = f"@salesforce/apex/{cls}.{method}"
            if resolved_qn and _resolve_imports_from(conn, file_path, bare, resolved_qn):
                apex_imports_resolved += 1
                local_to_method[local_name] = resolved_qn
            else:
                apex_imports_unresolved += 1

        apex_calls_resolved += _resolve_local_usages(conn, file_path, local_to_method)

        for _local_name, obj, field in _SCHEMA_IMPORT_RE.findall(source):
            resolved_qn = field_qual.get((obj, field))
            bare = f"@salesforce/schema/{obj}.{field}"
            if _resolve_imports_from(conn, file_path, bare, resolved_qn):
                schema_imports_resolved += 1
            else:
                schema_imports_unresolved += 1

    if apex_imports_resolved or schema_imports_resolved or apex_calls_resolved:
        store.commit()

    logger.info(
        "LWC/Apex resolver: %d file(s), %d apex import(s) resolved (%d unresolved), "
        "%d call/reference edge(s) resolved, %d schema import(s) resolved (%d unresolved)",
        len(js_files), apex_imports_resolved, apex_imports_unresolved,
        apex_calls_resolved, schema_imports_resolved, schema_imports_unresolved,
    )
    return {
        "files_indexed": len(js_files),
        "apex_imports_resolved": apex_imports_resolved,
        "apex_imports_unresolved": apex_imports_unresolved,
        "apex_calls_resolved": apex_calls_resolved,
        "schema_imports_resolved": schema_imports_resolved,
        "schema_imports_unresolved": schema_imports_unresolved,
    }
