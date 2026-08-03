"""Post-build resolver: Apex ``Label.X`` static field-access references.

``Label.My_Error_Message`` is Apex's built-in syntax for reading a Custom
Label — a plain field-access expression (``Label`` receiver, no
parentheses), not a method call. The generic parser only captures call-shaped
nodes (``method_invocation``), so this produces zero edges today even though
the Sitetracker Apex coding standard requires user-visible text go through
Custom Labels — meaning every compliant class does this.

Deliberately a regex over raw source rather than a parser.py change:
``Label.`` is Apex-reserved syntax, not a general expression pattern, so
matching it textually carries none of the ambiguity a general field-access
walker would (``System.LoggingLevel.DEBUG``, enum values, class constants,
etc. never start with the literal word ``Label``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_LABEL_REF_RE = re.compile(r"\bLabel\.([A-Za-z_][A-Za-z0-9_]*)\b")


def _enclosing_scope(methods: list, line_no: int, file_path: str) -> str:
    for row in methods:
        if row["line_start"] <= line_no <= row["line_end"]:
            return row["qualified_name"]
    return file_path


def resolve_apex_label_references(store: GraphStore) -> dict:
    """Resolve ``Label.X`` references in Apex source to ``Label`` nodes."""
    empty = {"files_indexed": 0, "references_resolved": 0, "references_unresolved": 0}
    conn = store._conn

    label_qual: dict[str, str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE kind = 'Label'"
    ).fetchall():
        label_qual.setdefault(row["name"], row["qualified_name"])
    if not label_qual:
        return empty

    apex_files: list[str] = [
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'apex'"
        ).fetchall()
    ]
    if not apex_files:
        return empty

    files_with_refs = 0
    resolved = 0
    unresolved = 0

    for file_path in apex_files:
        path = Path(file_path)
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Cannot read Apex file %s: %s", file_path, exc)
            continue

        matches = list(_LABEL_REF_RE.finditer(source))
        if not matches:
            continue
        files_with_refs += 1

        methods = conn.execute(
            "SELECT qualified_name, line_start, line_end FROM nodes "
            "WHERE file_path = ? AND kind IN ('Function', 'Test')",
            (file_path,),
        ).fetchall()

        for match in matches:
            label_name = match.group(1)
            line_no = source.count("\n", 0, match.start()) + 1
            source_qn = _enclosing_scope(methods, line_no, file_path)
            target_qn = label_qual.get(label_name)

            extra: dict = {"via": "Label"}
            if target_qn:
                target = target_qn
                resolved += 1
            else:
                target = f"Label.{label_name}"
                extra["unresolved_reference"] = True
                unresolved += 1

            store.upsert_edge(
                EdgeInfo(
                    kind="REFERENCES", source=source_qn, target=target,
                    file_path=file_path, line=line_no, extra=extra,
                )
            )

    if resolved or unresolved:
        store.commit()

    logger.info(
        "Apex label resolver: %d file(s), %d reference(s) resolved (%d unresolved)",
        files_with_refs, resolved, unresolved,
    )
    return {
        "files_indexed": files_with_refs,
        "references_resolved": resolved,
        "references_unresolved": unresolved,
    }
