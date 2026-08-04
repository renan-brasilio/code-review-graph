"""Post-build resolver: Aura component → Apex controller wiring.

Aura predates Lightning Web Components and wires to Apex completely
differently: an ``.cmp``/``.app`` bundle root tag names exactly one Apex
class as its ``controller``, and the bundle's client-side ``.js`` files
(controller/helper) dispatch server actions by *string* name —
``component.get("c.methodName")`` — rather than an ES6 import. Neither the
``.cmp``/``.app`` markup nor that string-dispatch pattern is visible to the
generic parser at all (no configured Tree-sitter grammar for `.cmp`/`.app`,
and a string literal method name isn't a call the JS/TS extractor
recognizes), so today ``callers_of`` on an Aura-invoked Apex method finds
nothing — same shape of gap as the LWC ``@salesforce/apex`` case, via a
different, string-based dispatch mechanism.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .metadata_indexer import DEFAULT_METADATA_PATHS, _load_metadata_config
from .parser import EdgeInfo, NodeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_AURA_CONTROLLER_RE = re.compile(
    r"""<aura:(?:component|application)\b[^>]*\bcontroller\s*=\s*["']([A-Za-z0-9_.]+)["']"""
)
_AURA_ACTION_RE = re.compile(r"""component\.get\(\s*["']c\.([A-Za-z0-9_]+)["']\s*\)""")


def _discover_aura_bundles(paths: list[str], repo_root: Path) -> list[Path]:
    seen: set[str] = set()
    found: list[Path] = []
    for rel in paths:
        base = repo_root / rel
        if not base.is_dir():
            continue
        for pattern in ("*.cmp", "*.app"):
            for bundle_path in base.rglob(pattern):
                key = str(bundle_path)
                if key in seen:
                    continue
                seen.add(key)
                found.append(bundle_path)
    return found


def _parse_aura_bundle(path: Path) -> Optional[dict]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Cannot read Aura bundle file %s: %s", path, exc)
        return None

    match = _AURA_CONTROLLER_RE.search(source)
    return {
        "name": path.stem,
        "file_path": str(path),
        "controller": match.group(1) if match else None,
        "bundle_dir": path.parent,
    }


def _aura_action_names(bundle_dir: Path) -> set[str]:
    names: set[str] = set()
    for js_file in bundle_dir.glob("*.js"):
        try:
            source = js_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Cannot read Aura controller/helper %s: %s", js_file, exc)
            continue
        names.update(_AURA_ACTION_RE.findall(source))
    return names


def _remove_stale_aura_bundles(store: GraphStore, current_paths: set[str]) -> int:
    """Remove AuraComponent nodes whose .cmp/.app file no longer exists.

    Same phantom-node risk as *.field-meta.xml/etc — .cmp/.app has no
    configured Tree-sitter language, so the general stale-file
    reconciliation in incremental.py never considers these files.
    """
    stored_paths = {
        row["file_path"]
        for row in store._conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE kind = 'AuraComponent'"
        ).fetchall()
    }
    stale = stored_paths - current_paths
    if not stale:
        return 0
    store.remove_files_permanently(list(stale))
    return len(stale)


def resolve_aura_apex_wiring(store: GraphStore, repo_root: Path) -> dict:
    """Index Aura components and resolve their controller's action calls."""
    empty = {
        "aura_components_indexed": 0, "aura_invokes_created": 0,
        "aura_invokes_unresolved": 0, "stale_aura_bundles_removed": 0,
    }
    config = _load_metadata_config(repo_root)
    if not config:
        return empty

    paths = config.get("paths") or list(DEFAULT_METADATA_PATHS)
    bundle_files = _discover_aura_bundles(paths, repo_root)

    current_paths = {str(p) for p in bundle_files}
    stale_removed = _remove_stale_aura_bundles(store, current_paths)

    parsed = [info for info in (_parse_aura_bundle(p) for p in bundle_files) if info]
    if not parsed:
        if stale_removed:
            return {**empty, "stale_aura_bundles_removed": stale_removed}
        return empty

    method_qual: dict[tuple[str, str], str] = {}
    for row in store._conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'apex' "
        "AND parent_name IS NOT NULL"
    ).fetchall():
        method_qual[(row["parent_name"], row["name"])] = row["qualified_name"]

    components_indexed = 0
    invokes_created = 0
    invokes_unresolved = 0

    for info in parsed:
        comp_qn = f"{info['file_path']}::{info['name']}"
        extra: dict = {"metadata_type": "AuraComponent"}
        if info.get("controller"):
            extra["controller"] = info["controller"]
        store.upsert_node(
            NodeInfo(
                kind="AuraComponent",
                name=info["name"],
                file_path=info["file_path"],
                line_start=1,
                line_end=1,
                language="salesforce_metadata",
                extra=extra,
            )
        )
        components_indexed += 1

        controller = info.get("controller")
        if not controller:
            continue

        for method_name in sorted(_aura_action_names(Path(info["bundle_dir"]))):
            target_qn = method_qual.get((controller, method_name))
            edge_extra: dict = {"via": "component.get"}
            if target_qn:
                target = target_qn
            else:
                target = f"{controller}.{method_name}"
                edge_extra["unresolved_reference"] = True
                invokes_unresolved += 1
            store.upsert_edge(
                EdgeInfo(
                    kind="INVOKES", source=comp_qn, target=target,
                    file_path=info["file_path"], line=1, extra=edge_extra,
                )
            )
            invokes_created += 1

    if components_indexed or invokes_created:
        store.commit()

    logger.info(
        "Aura/Apex resolver: %d component(s), %d invoke edge(s) (%d unresolved), "
        "%d stale bundle(s) removed",
        components_indexed, invokes_created, invokes_unresolved, stale_removed,
    )
    return {
        "aura_components_indexed": components_indexed,
        "aura_invokes_created": invokes_created,
        "aura_invokes_unresolved": invokes_unresolved,
        "stale_aura_bundles_removed": stale_removed,
    }
