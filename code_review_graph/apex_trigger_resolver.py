"""Post-build Apex trigger → handler resolver.

Detects ``StTriggerFactory.createAndExecuteHandler(HandlerClass.class)`` in
``.trigger`` files and emits ``INVOKES`` edges from the trigger to the handler
class so traversal can follow trigger → handler chains.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def _get_apex_parser():
    import tree_sitter_language_pack as tslp

    return tslp.get_parser("apex")


def _trigger_name_from_tree(root) -> Optional[str]:
    for child in root.children:
        if child.type == "trigger_declaration":
            for ch in child.children:
                if ch.type == "identifier":
                    return ch.text.decode("utf-8", errors="replace")
    return None


def _is_create_and_execute_handler(node) -> bool:
    method_name: Optional[str] = None
    for ch in reversed(node.children):
        if ch.type == "argument_list":
            continue
        if ch.type == "identifier":
            method_name = ch.text.decode("utf-8", errors="replace")
            break
        if ch.type == ".":
            continue
        break
    return method_name == "createAndExecuteHandler"


def _extract_handler_class(node) -> Optional[str]:
    for child in node.children:
        if child.type != "argument_list":
            continue
        for arg in child.children:
            if arg.type in ("field_access", "scoped_identifier"):
                for ch in arg.children:
                    if ch.type == "identifier":
                        name = ch.text.decode("utf-8", errors="replace")
                        if name != "class":
                            return name
            elif arg.type == "identifier":
                return arg.text.decode("utf-8", errors="replace")
    return None


def _find_handler_invocations(node, handlers: list[str]) -> None:
    if node.type == "method_invocation" and _is_create_and_execute_handler(node):
        handler = _extract_handler_class(node)
        if handler:
            handlers.append(handler)
    for child in node.children:
        _find_handler_invocations(child, handlers)


def resolve_apex_trigger_handlers(store: GraphStore) -> dict:
    """Emit INVOKES edges from Apex triggers to their handler classes."""
    conn = store._conn

    trigger_files: list[str] = [
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes "
            "WHERE language = 'apex' AND file_path LIKE '%.trigger'"
        ).fetchall()
    ]
    if not trigger_files:
        return {"triggers_indexed": 0, "handlers_linked": 0}

    name_to_qual: dict[str, str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name FROM nodes "
        "WHERE kind = 'Class' AND language = 'apex'"
    ).fetchall():
        bare = row["name"]
        qual = row["qualified_name"]
        if bare not in name_to_qual or len(qual) < len(name_to_qual[bare]):
            name_to_qual[bare] = qual

    existing: set[tuple[str, str]] = {
        (row["source_qualified"], row["target_qualified"])
        for row in conn.execute(
            "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'INVOKES'"
        ).fetchall()
    }

    parser = _get_apex_parser()
    linked = 0

    for file_path in trigger_files:
        path = Path(file_path)
        if not path.is_file():
            continue
        try:
            source = path.read_bytes()
        except OSError as exc:
            logger.warning("Cannot read trigger file %s: %s", file_path, exc)
            continue

        tree = parser.parse(source)
        trigger_name = _trigger_name_from_tree(tree.root_node)
        if not trigger_name:
            continue

        trigger_qn = f"{file_path}::{trigger_name}"
        handlers: list[str] = []
        _find_handler_invocations(tree.root_node, handlers)

        for handler_bare in handlers:
            handler_qn = name_to_qual.get(handler_bare)
            if not handler_qn:
                logger.debug(
                    "Trigger resolver: handler %s not found for %s",
                    handler_bare,
                    trigger_qn,
                )
                continue
            key = (trigger_qn, handler_qn)
            if key in existing:
                continue
            store.upsert_edge(
                EdgeInfo(
                    kind="INVOKES",
                    source=trigger_qn,
                    target=handler_qn,
                    file_path=file_path,
                    line=0,
                    extra={"handler_class": handler_bare, "apex_trigger_resolved": True},
                )
            )
            existing.add(key)
            linked += 1

    if linked:
        store.commit()

    logger.info(
        "Apex trigger resolver: linked %d handler(s) across %d trigger file(s)",
        linked,
        len(trigger_files),
    )
    return {"triggers_indexed": len(trigger_files), "handlers_linked": linked}
