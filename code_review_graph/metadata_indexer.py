"""Salesforce metadata indexer — fields, flows, and object references.

Parses ``*.field-meta.xml`` and ``*.flow-meta.xml`` under configured paths.
Creates ``Field`` nodes with formula text in ``extra``, ``SalesforceFlow``
nodes with a compact step summary, and cross-boundary edges: field formula
references, Field→Object ``BELONGS_TO``, Flow→Apex/Flow ``INVOKES``, and
Flow→Object ``REFERENCES``.

Note: the node kind is ``SalesforceFlow``, not ``Flow`` — this codebase
already uses "flow" for a derived concept (execution paths through the call
graph; see ``flows.py`` / ``get_flow_tool``), unrelated to Salesforce's
declarative Flow automation metadata.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET  # nosec B405 - parses local repo metadata files, not untrusted network XML
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .graph import GraphStore

from .parser import EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)

_SF_NS = "http://soap.sforce.com/2006/04/metadata"
_FIELD_REF_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*__(?:c|r))\b")

# The whole package directory, not just "objects/" — *.flow-meta.xml lives
# under a sibling "flows/" folder, and rglob recurses either way.
DEFAULT_METADATA_PATHS = ("force-app",)

_OBJECT_STUB_FILE = "salesforce_metadata://Object"

# Flow elements that represent a step in the flow's control graph (each has
# a <name> and, per the one-node-per-file design, contribute to the compact
# extra["steps"] summary rather than becoming their own graph nodes).
_FLOW_STEP_TAGS = frozenset({
    "actionCalls", "assignments", "collectionProcessors", "decisions",
    "loops", "orchestratedStages", "recordCreates", "recordDeletes",
    "recordLookups", "recordRollbacks", "recordUpdates", "screens",
    "subflows", "waits", "apexPluginCalls",
})


def _package_directories(repo_root: Path) -> list[str]:
    """Read ``packageDirectories`` from ``sfdx-project.json``, if present.

    Real SFDX projects declare arbitrary package directory names (not
    always ``force-app``), so this is the authoritative source for where
    metadata lives — more reliable than guessing a fixed layout.
    """
    sfdx_path = repo_root / "sfdx-project.json"
    if not sfdx_path.is_file():
        return []
    try:
        data = json.loads(sfdx_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Malformed sfdx-project.json: %s", exc)
        return []

    dirs: list[str] = []
    for entry in data.get("packageDirectories") or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.strip():
            dirs.append(path.strip().rstrip("/"))
    return dirs


def _load_metadata_config(repo_root: Path) -> Optional[dict]:
    config_path = repo_root / ".code-review-graph" / "metadata.toml"
    if config_path.is_file():
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        section = data.get("metadata", {})
        if not section.get("enabled", True):
            return None
        section = dict(section)
        if not section.get("paths"):
            section["paths"] = _package_directories(repo_root) or list(DEFAULT_METADATA_PATHS)
        return section

    pkg_dirs = _package_directories(repo_root)
    if pkg_dirs:
        return {"enabled": True, "paths": pkg_dirs, "include_formulas": True}
    if (repo_root / "force-app").is_dir():
        return {"enabled": True, "paths": list(DEFAULT_METADATA_PATHS), "include_formulas": True}
    return None


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _child_text(elem: ET.Element, tag: str) -> Optional[str]:
    for child in elem:
        if _local(child.tag) == tag and child.text:
            return child.text.strip()
    return None


def _parse_field_meta(path: Path) -> Optional[dict]:
    try:
        tree = ET.parse(path)  # nosec B314 - local repo file, not untrusted network XML
    except ET.ParseError as exc:
        logger.warning("Malformed field metadata %s: %s", path, exc)
        return None

    root = tree.getroot()
    full_name = None
    formula = None
    field_type = None
    relationship_name = None
    reference_to: list[str] = []
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "fullName" and elem.text:
            full_name = elem.text.strip()
        elif tag == "formula" and elem.text:
            formula = elem.text.strip()
        elif tag == "type" and elem.text:
            field_type = elem.text.strip()
        elif tag == "relationshipName" and elem.text:
            relationship_name = elem.text.strip()
        elif tag == "referenceTo" and elem.text:
            reference_to.append(elem.text.strip())

    if not full_name:
        return None

    # Parent object from path: .../objects/ObjectName/fields/FieldName.field-meta.xml
    parts = path.parts
    parent_name = None
    if "objects" in parts:
        idx = parts.index("objects")
        if idx + 1 < len(parts):
            parent_name = parts[idx + 1]

    return {
        "name": full_name,
        "parent_name": parent_name,
        "formula": formula,
        "file_path": str(path),
        "type": field_type,
        "relationship_name": relationship_name,
        "reference_to": reference_to,
    }


def _field_qualified_name(info: dict) -> str:
    """Mirror ``GraphStore._make_qualified`` for a Field NodeInfo.

    Field nodes are created with ``parent_name`` set, so the store qualifies
    them as ``file::Object.FieldName`` — edge endpoints must match exactly or
    they silently point at a non-existent node.
    """
    parent_name = info.get("parent_name")
    if parent_name:
        return f"{info['file_path']}::{parent_name}.{info['name']}"
    return f"{info['file_path']}::{info['name']}"


def _field_refs(formula: str) -> set[str]:
    if not formula:
        return set()
    return set(_FIELD_REF_RE.findall(formula))


def _object_qualified_name(object_name: str) -> str:
    return f"{_OBJECT_STUB_FILE}::{object_name}"


def _upsert_object_stub(store: GraphStore, object_name: str, seen: set[str]) -> str:
    """Ensure a minimal ``Object`` node exists for *object_name* and return its qn.

    Only creates a *stub* — if ``_index_objects`` already indexed real
    ``.object-meta.xml`` for this object this run, *seen* already contains
    it and this is a no-op, so a stub never clobbers real metadata. This
    still matters for objects with no local ``.object-meta.xml`` at all:
    extending a managed package's object (adding a custom field or a flow
    that queries it) intentionally does not redeclare the package's own
    object metadata locally, since that would drift from and conflict with
    the installed package version.
    """
    qn = _object_qualified_name(object_name)
    if object_name not in seen:
        seen.add(object_name)
        store.upsert_node(
            NodeInfo(
                kind="Object",
                name=object_name,
                file_path=_OBJECT_STUB_FILE,
                line_start=1,
                line_end=1,
                language="salesforce_metadata",
                extra={"metadata_type": "Object", "synthesized": True},
            )
        )
    return qn


def _parse_object_meta(path: Path) -> Optional[dict]:
    """Parse ``objects/ObjectName/ObjectName.object-meta.xml``.

    Custom Metadata Types (``objects/X__mdt/X__mdt.object-meta.xml``) use
    the exact same format — the ``__mdt`` suffix on the object name is the
    only marker, so no separate parsing path is needed for CMT *type*
    definitions (CMT *records* under ``customMetadata/`` are data, not
    structure, and aren't indexed).
    """
    try:
        tree = ET.parse(path)  # nosec B314 - local repo file, not untrusted network XML
    except ET.ParseError as exc:
        logger.warning("Malformed object metadata %s: %s", path, exc)
        return None

    root = tree.getroot()
    object_name = path.parent.name
    if not object_name:
        return None

    label = None
    description = None
    sharing_model = None
    deployment_status = None
    plural_label = None
    for child in root:
        tag = _local(child.tag)
        if tag == "label" and child.text:
            label = child.text.strip()
        elif tag == "description" and child.text:
            description = child.text.strip()
        elif tag == "sharingModel" and child.text:
            sharing_model = child.text.strip()
        elif tag == "deploymentStatus" and child.text:
            deployment_status = child.text.strip()
        elif tag == "pluralLabel" and child.text:
            plural_label = child.text.strip()

    return {
        "name": object_name,
        "file_path": str(path),
        "label": label,
        "description": description,
        "sharing_model": sharing_model,
        "deployment_status": deployment_status,
        "plural_label": plural_label,
        "is_custom_metadata_type": object_name.endswith("__mdt"),
    }


def _index_objects(store: GraphStore, object_files: list[Path]) -> tuple[dict, set[str]]:
    """Index real ``.object-meta.xml`` files. Returns (stats, object names indexed)."""
    real_names: set[str] = set()
    objects_indexed = 0
    for path in object_files:
        info = _parse_object_meta(path)
        if not info:
            continue
        extra: dict = {
            "metadata_type": "Object", "synthesized": False, "source_file": info["file_path"],
        }
        if info.get("label"):
            extra["label"] = info["label"]
        if info.get("description"):
            extra["description"] = info["description"]
        if info.get("sharing_model"):
            extra["sharing_model"] = info["sharing_model"]
        if info.get("deployment_status"):
            extra["deployment_status"] = info["deployment_status"]
        if info.get("plural_label"):
            extra["plural_label"] = info["plural_label"]
        if info["is_custom_metadata_type"]:
            extra["is_custom_metadata_type"] = True

        store.upsert_node(
            NodeInfo(
                kind="Object",
                name=info["name"],
                file_path=_OBJECT_STUB_FILE,
                line_start=1,
                line_end=1,
                language="salesforce_metadata",
                extra=extra,
            )
        )
        objects_indexed += 1
        real_names.add(info["name"])

    return {"objects_indexed": objects_indexed}, real_names


def _downgrade_stale_real_objects(store: GraphStore, current_real_names: set[str]) -> int:
    """Reset Object nodes back to a bare stub when their .object-meta.xml disappears.

    Never deletes the node outright — other Field/Flow references to the
    same object by name may still be legitimate, so only the "real metadata"
    extra is cleared, not the node's existence.
    """
    conn = store._conn
    downgraded = 0
    for row in conn.execute(
        "SELECT name, extra FROM nodes WHERE kind = 'Object'"
    ).fetchall():
        if row["name"] in current_real_names:
            continue
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        if extra.get("synthesized", True):
            continue  # already a stub, nothing to downgrade
        store.upsert_node(
            NodeInfo(
                kind="Object",
                name=row["name"],
                file_path=_OBJECT_STUB_FILE,
                line_start=1,
                line_end=1,
                language="salesforce_metadata",
                extra={"metadata_type": "Object", "synthesized": True},
            )
        )
        downgraded += 1
    return downgraded


def _target_references(elem: ET.Element) -> list[str]:
    refs: list[str] = []
    for sub in elem.iter():
        if _local(sub.tag) == "targetReference" and sub.text:
            ref = sub.text.strip()
            if ref not in refs:
                refs.append(ref)
    return refs


def _parse_flow_meta(path: Path) -> Optional[dict]:
    if not path.name.endswith(".flow-meta.xml"):
        return None
    try:
        tree = ET.parse(path)  # nosec B314 - local repo file, not untrusted network XML
    except ET.ParseError as exc:
        logger.warning("Malformed flow metadata %s: %s", path, exc)
        return None

    root = tree.getroot()
    name = path.name[: -len(".flow-meta.xml")]

    process_type = None
    status = None
    trigger_object = None
    trigger_type = None
    record_trigger_type = None
    steps: list[dict] = []
    action_apex_refs: list[str] = []
    subflow_refs: list[str] = []
    object_refs: set[str] = set()

    for child in root:
        tag = _local(child.tag)
        if tag == "processType" and child.text:
            process_type = child.text.strip()
        elif tag == "status" and child.text:
            status = child.text.strip()
        elif tag == "start":
            trigger_object = _child_text(child, "object")
            trigger_type = _child_text(child, "triggerType")
            record_trigger_type = _child_text(child, "recordTriggerType")
            if trigger_object:
                object_refs.add(trigger_object)
            steps.append({
                "name": "start",
                "type": "start",
                "connects_to": _target_references(child),
            })
        elif tag in _FLOW_STEP_TAGS:
            step_name = _child_text(child, "name") or tag
            step: dict = {
                "name": step_name,
                "type": tag,
                "connects_to": _target_references(child),
            }
            obj = _child_text(child, "object")
            if obj:
                step["object"] = obj
                object_refs.add(obj)
            if tag == "actionCalls":
                action_type = _child_text(child, "actionType")
                action_name = _child_text(child, "actionName")
                if action_name and (action_type or "").lower() == "apex":
                    action_apex_refs.append(action_name)
                    step["action_name"] = action_name
                    step["action_type"] = action_type
            elif tag == "subflows":
                flow_name = _child_text(child, "flowName")
                if flow_name:
                    subflow_refs.append(flow_name)
                    step["flow_name"] = flow_name
            steps.append(step)

    return {
        "name": name,
        "file_path": str(path),
        "process_type": process_type,
        "status": status,
        "trigger_object": trigger_object,
        "trigger_type": trigger_type,
        "record_trigger_type": record_trigger_type,
        "steps": steps,
        "action_apex_refs": action_apex_refs,
        "subflow_refs": subflow_refs,
        "object_refs": sorted(object_refs),
    }


def _parse_custom_labels(path: Path) -> list[dict]:
    """Parse a ``CustomLabels.labels-meta.xml`` bundle into one dict per label.

    Unlike fields/flows, all of an org's Custom Labels live in a single
    file — Salesforce doesn't split these one-per-file.
    """
    try:
        tree = ET.parse(path)  # nosec B314 - local repo file, not untrusted network XML
    except ET.ParseError as exc:
        logger.warning("Malformed custom labels metadata %s: %s", path, exc)
        return []

    labels: list[dict] = []
    for elem in tree.getroot():
        if _local(elem.tag) != "labels":
            continue
        full_name = _child_text(elem, "fullName")
        if not full_name:
            continue
        labels.append({
            "name": full_name,
            "file_path": str(path),
            "value": _child_text(elem, "value"),
            "categories": _child_text(elem, "categories"),
            "short_description": _child_text(elem, "shortDescription"),
        })
    return labels


def _remove_stale_labels_in_file(store: GraphStore, file_path: str, current_names: set[str]) -> int:
    """Remove Label nodes whose entry is gone from *file_path*, which itself still exists.

    Unlike fields/flows (one file per entry), all of an org's Custom Labels
    live in one ``CustomLabels.labels-meta.xml`` bundle — removing a single
    ``<labels>`` entry doesn't change the set of *files* on disk, so the
    file-level stale check in ``index_salesforce_metadata`` never catches
    it. This diffs at the entry level instead.
    """
    conn = store._conn
    removed = 0
    for row in conn.execute(
        "SELECT qualified_name, name FROM nodes WHERE kind = 'Label' AND file_path = ?",
        (file_path,),
    ).fetchall():
        if row["name"] in current_names:
            continue
        qn = row["qualified_name"]
        conn.execute(
            "DELETE FROM edges WHERE source_qualified = ? OR target_qualified = ?", (qn, qn),
        )
        conn.execute("DELETE FROM nodes WHERE qualified_name = ?", (qn,))
        removed += 1
    if removed:
        store.commit()
    return removed


def _index_labels(store: GraphStore, label_files: list[Path]) -> dict:
    labels_indexed = 0
    labels_removed = 0
    for path in label_files:
        parsed = _parse_custom_labels(path)
        labels_removed += _remove_stale_labels_in_file(
            store, str(path), {info["name"] for info in parsed}
        )
        for info in parsed:
            extra: dict = {"metadata_type": "CustomLabel"}
            if info.get("value"):
                extra["value"] = info["value"]
            if info.get("categories"):
                extra["categories"] = info["categories"]
            if info.get("short_description"):
                extra["short_description"] = info["short_description"]
            store.upsert_node(
                NodeInfo(
                    kind="Label",
                    name=info["name"],
                    file_path=info["file_path"],
                    line_start=1,
                    line_end=1,
                    language="salesforce_metadata",
                    extra=extra,
                )
            )
            labels_indexed += 1
    return {"labels_indexed": labels_indexed, "labels_removed": labels_removed}


def _bool_text(elem: ET.Element, tag: str) -> bool:
    return (_child_text(elem, tag) or "false").strip().lower() == "true"


def _parse_permission_set(path: Path) -> Optional[dict]:
    """Parse a ``*.permissionset-meta.xml``.

    Unlike fields/labels, PermissionSet metadata has no ``<fullName>``
    element — the API name comes from the file name, same convention as
    Flow.
    """
    if not path.name.endswith(".permissionset-meta.xml"):
        return None
    try:
        tree = ET.parse(path)  # nosec B314 - local repo file, not untrusted network XML
    except ET.ParseError as exc:
        logger.warning("Malformed permission set metadata %s: %s", path, exc)
        return None

    root = tree.getroot()
    name = path.name[: -len(".permissionset-meta.xml")]
    label = None
    description = None
    class_names: list[str] = []
    field_refs: list[tuple[str, str]] = []
    object_refs: list[str] = []

    for child in root:
        tag = _local(child.tag)
        if tag == "label" and child.text:
            label = child.text.strip()
        elif tag == "description" and child.text:
            description = child.text.strip()
        elif tag == "classAccesses":
            apex_class = _child_text(child, "apexClass")
            if apex_class and _bool_text(child, "enabled"):
                class_names.append(apex_class)
        elif tag == "fieldPermissions":
            field_full = _child_text(child, "field")
            if (
                field_full and "." in field_full
                and (_bool_text(child, "readable") or _bool_text(child, "editable"))
            ):
                obj, _, fld = field_full.partition(".")
                field_refs.append((obj, fld))
        elif tag == "objectPermissions":
            obj = _child_text(child, "object")
            has_any_permission = any(
                _bool_text(child, perm)
                for perm in (
                    "allowCreate", "allowRead", "allowEdit", "allowDelete",
                    "modifyAllRecords", "viewAllRecords",
                )
            )
            if obj and has_any_permission:
                object_refs.append(obj)

    return {
        "name": name,
        "file_path": str(path),
        "label": label,
        "description": description,
        "class_names": class_names,
        "field_refs": field_refs,
        "object_refs": object_refs,
    }


def _index_permission_sets(
    store: GraphStore, ps_files: list[Path], object_stub_seen: set[str],
) -> dict:
    parsed = [info for info in (_parse_permission_set(p) for p in ps_files) if info]

    class_qual: dict[str, str] = {}
    for row in store._conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE kind = 'Class' AND language = 'apex'"
    ).fetchall():
        bare, qual = row["name"], row["qualified_name"]
        if bare not in class_qual or len(qual) < len(class_qual[bare]):
            class_qual[bare] = qual

    field_qual: dict[tuple[str, str], str] = {}
    for row in store._conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind = 'Field' AND language = 'salesforce_metadata'"
    ).fetchall():
        field_qual[(row["parent_name"], row["name"])] = row["qualified_name"]

    permission_sets_indexed = 0
    grants_created = 0
    grants_unresolved = 0

    for info in parsed:
        ps_qn = f"{info['file_path']}::{info['name']}"
        extra: dict = {"metadata_type": "PermissionSet"}
        if info.get("label"):
            extra["label"] = info["label"]
        if info.get("description"):
            extra["description"] = info["description"]
        store.upsert_node(
            NodeInfo(
                kind="PermissionSet",
                name=info["name"],
                file_path=info["file_path"],
                line_start=1,
                line_end=1,
                language="salesforce_metadata",
                extra=extra,
            )
        )
        permission_sets_indexed += 1

        for cls in info["class_names"]:
            target_qn = class_qual.get(cls)
            edge_extra: dict = {"via": "classAccesses"}
            if target_qn:
                target = target_qn
            else:
                target = cls
                edge_extra["unresolved_reference"] = True
                grants_unresolved += 1
            store.upsert_edge(
                EdgeInfo(
                    kind="GRANTS", source=ps_qn, target=target,
                    file_path=info["file_path"], line=1, extra=edge_extra,
                )
            )
            grants_created += 1

        for obj, fld in info["field_refs"]:
            target_qn = field_qual.get((obj, fld))
            edge_extra = {"via": "fieldPermissions"}
            if target_qn:
                target = target_qn
            else:
                target = f"{obj}.{fld}"
                edge_extra["unresolved_reference"] = True
                grants_unresolved += 1
            store.upsert_edge(
                EdgeInfo(
                    kind="GRANTS", source=ps_qn, target=target,
                    file_path=info["file_path"], line=1, extra=edge_extra,
                )
            )
            grants_created += 1

        for obj in info["object_refs"]:
            object_qn = _upsert_object_stub(store, obj, object_stub_seen)
            store.upsert_edge(
                EdgeInfo(
                    kind="GRANTS", source=ps_qn, target=object_qn,
                    file_path=info["file_path"], line=1, extra={"via": "objectPermissions"},
                )
            )
            grants_created += 1

    return {
        "permission_sets_indexed": permission_sets_indexed,
        "grants_created": grants_created,
        "grants_unresolved": grants_unresolved,
    }


def _discover(paths: list[str], repo_root: Path, pattern: str) -> list[Path]:
    seen: set[str] = set()
    found: list[Path] = []
    for rel in paths:
        base = repo_root / rel
        if not base.is_dir():
            continue
        for meta_path in base.rglob(pattern):
            key = str(meta_path)
            if key in seen:
                continue
            seen.add(key)
            found.append(meta_path)
    return found


def _remove_stale_metadata_files(store: GraphStore, current_paths: set[str]) -> int:
    """Remove Field/SalesforceFlow/Label nodes whose source file no longer exists.

    Metadata XML has no configured Tree-sitter language, so the general
    stale-file reconciliation in ``incremental.py`` (gated on
    ``parser.detect_language()``) never sees these files — a deleted
    ``*.field-meta.xml``/``*.flow-meta.xml``/``CustomLabels.labels-meta.xml``/
    ``*.permissionset-meta.xml`` would otherwise leave a permanent phantom
    node behind on every subsequent full ``build``.
    """
    stored_paths = {
        row["file_path"]
        for row in store._conn.execute(
            "SELECT DISTINCT file_path FROM nodes "
            "WHERE kind IN ('Field', 'SalesforceFlow', 'Label', 'PermissionSet')"
        ).fetchall()
    }
    stale = stored_paths - current_paths
    if not stale:
        return 0
    store.remove_files_permanently(list(stale))
    return len(stale)


def _index_fields(
    store: GraphStore,
    field_files: list[Path],
    include_formulas: bool,
    object_stub_seen: set[str],
) -> dict:
    parsed = [info for info in (_parse_field_meta(p) for p in field_files) if info]

    # name -> qualified name, preferring the same parent object on lookup.
    field_qn_by_object: dict[tuple[Optional[str], str], str] = {}
    field_qn_by_name: dict[str, str] = {}
    # (object, relationshipName) -> lookup/master-detail field's own qn.
    relationship_qn_by_object: dict[tuple[Optional[str], str], str] = {}

    for info in parsed:
        field_qn = _field_qualified_name(info)
        field_qn_by_object[(info.get("parent_name"), info["name"])] = field_qn
        field_qn_by_name.setdefault(info["name"], field_qn)
        if info.get("relationship_name"):
            rel_key = (info.get("parent_name"), info["relationship_name"])
            relationship_qn_by_object[rel_key] = field_qn

    fields_indexed = 0
    references_created = 0
    references_unresolved = 0

    for info in parsed:
        parent_name = info.get("parent_name")
        field_qn = _field_qualified_name(info)
        extra: dict = {"metadata_type": "CustomField"}
        if info.get("type"):
            extra["field_type"] = info["type"]
        if info.get("relationship_name"):
            extra["relationship_name"] = info["relationship_name"]
        if info.get("reference_to"):
            extra["reference_to"] = info["reference_to"]
        if include_formulas and info.get("formula"):
            extra["formula"] = info["formula"]

        store.upsert_node(
            NodeInfo(
                kind="Field",
                name=info["name"],
                file_path=info["file_path"],
                line_start=1,
                line_end=1,
                language="salesforce_metadata",
                parent_name=parent_name,
                extra=extra,
            )
        )
        fields_indexed += 1

        if parent_name:
            object_qn = _upsert_object_stub(store, parent_name, object_stub_seen)
            store.upsert_edge(
                EdgeInfo(
                    kind="BELONGS_TO",
                    source=field_qn,
                    target=object_qn,
                    file_path=info["file_path"],
                    line=1,
                    extra={},
                )
            )

        if not info.get("formula"):
            continue

        for ref in _field_refs(info["formula"]):
            if ref == info["name"]:
                continue
            if ref.endswith("__r"):
                # relationshipName in the XML has no __r suffix; the formula
                # token does.
                target_qn = relationship_qn_by_object.get((parent_name, ref[: -len("__r")]))
            else:
                target_qn = field_qn_by_object.get((parent_name, ref)) or field_qn_by_name.get(ref)

            edge_extra = {"from_formula": True}
            if target_qn:
                target = target_qn
            else:
                target = ref
                edge_extra["unresolved_reference"] = True
                references_unresolved += 1

            store.upsert_edge(
                EdgeInfo(
                    kind="REFERENCES",
                    source=field_qn,
                    target=target,
                    file_path=info["file_path"],
                    line=1,
                    extra=edge_extra,
                )
            )
            references_created += 1

    return {
        "fields_indexed": fields_indexed,
        "references_created": references_created,
        "references_unresolved": references_unresolved,
    }


def _index_flows(
    store: GraphStore,
    flow_files: list[Path],
    object_stub_seen: set[str],
) -> dict:
    parsed = [info for info in (_parse_flow_meta(p) for p in flow_files) if info]

    flow_qn_by_name: dict[str, str] = {
        info["name"]: f"{info['file_path']}::{info['name']}" for info in parsed
    }
    class_qn_by_name: dict[str, str] = {}
    for row in store._conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE kind = 'Class' AND language = 'apex'"
    ).fetchall():
        # Prefer the shortest qualified name on a name collision (matches
        # apex_static_resolver's convention for the same ambiguity).
        bare = row["name"]
        qual = row["qualified_name"]
        if bare not in class_qn_by_name or len(qual) < len(class_qn_by_name[bare]):
            class_qn_by_name[bare] = qual

    flows_indexed = 0
    invokes_created = 0
    invokes_unresolved = 0
    references_created = 0

    for info in parsed:
        flow_qn = flow_qn_by_name[info["name"]]
        extra: dict = {
            "metadata_type": "Flow",
            "steps": info["steps"],
            "step_count": len(info["steps"]),
        }
        if info.get("process_type"):
            extra["process_type"] = info["process_type"]
        if info.get("status"):
            extra["status"] = info["status"]
        if info.get("trigger_object"):
            extra["trigger_object"] = info["trigger_object"]
        if info.get("trigger_type"):
            extra["trigger_type"] = info["trigger_type"]
        if info.get("record_trigger_type"):
            extra["record_trigger_type"] = info["record_trigger_type"]

        store.upsert_node(
            NodeInfo(
                kind="SalesforceFlow",
                name=info["name"],
                file_path=info["file_path"],
                line_start=1,
                line_end=1,
                language="salesforce_metadata",
                extra=extra,
            )
        )
        flows_indexed += 1

        for action_name in info["action_apex_refs"]:
            target_qn = class_qn_by_name.get(action_name)
            edge_extra: dict = {"via": "actionCalls"}
            if target_qn:
                target = target_qn
            else:
                target = action_name
                edge_extra["unresolved_reference"] = True
                invokes_unresolved += 1
            store.upsert_edge(
                EdgeInfo(
                    kind="INVOKES", source=flow_qn, target=target,
                    file_path=info["file_path"], line=1, extra=edge_extra,
                )
            )
            invokes_created += 1

        for flow_name in info["subflow_refs"]:
            target_qn = flow_qn_by_name.get(flow_name)
            edge_extra = {"via": "subflows"}
            if target_qn:
                target = target_qn
            else:
                target = flow_name
                edge_extra["unresolved_reference"] = True
                invokes_unresolved += 1
            store.upsert_edge(
                EdgeInfo(
                    kind="INVOKES", source=flow_qn, target=target,
                    file_path=info["file_path"], line=1, extra=edge_extra,
                )
            )
            invokes_created += 1

        for object_name in info["object_refs"]:
            object_qn = _upsert_object_stub(store, object_name, object_stub_seen)
            store.upsert_edge(
                EdgeInfo(
                    kind="REFERENCES", source=flow_qn, target=object_qn,
                    file_path=info["file_path"], line=1, extra={},
                )
            )
            references_created += 1

    return {
        "flows_indexed": flows_indexed,
        "flow_invokes_created": invokes_created,
        "flow_invokes_unresolved": invokes_unresolved,
        "flow_references_created": references_created,
    }


def index_salesforce_metadata(store: GraphStore, repo_root: Path) -> dict:
    """Index Salesforce field and Flow metadata into the graph store."""
    empty = {
        "fields_indexed": 0, "references_created": 0, "references_unresolved": 0,
        "flows_indexed": 0, "flow_invokes_created": 0, "flow_invokes_unresolved": 0,
        "flow_references_created": 0, "objects_indexed": 0, "labels_indexed": 0,
        "labels_removed": 0, "stale_metadata_files_removed": 0,
        "real_objects_indexed": 0, "objects_downgraded": 0,
        "permission_sets_indexed": 0, "grants_created": 0, "grants_unresolved": 0,
    }
    config = _load_metadata_config(repo_root)
    if not config:
        return empty

    paths = config.get("paths") or list(DEFAULT_METADATA_PATHS)
    include_formulas = config.get("include_formulas", True)

    field_files = _discover(paths, repo_root, "*.field-meta.xml")
    flow_files = _discover(paths, repo_root, "*.flow-meta.xml")
    label_files = _discover(paths, repo_root, "CustomLabels.labels-meta.xml")
    object_files = _discover(paths, repo_root, "*.object-meta.xml")
    permission_set_files = _discover(paths, repo_root, "*.permissionset-meta.xml")

    current_paths = {
        str(p) for p in (*field_files, *flow_files, *label_files, *permission_set_files)
    }
    stale_removed = _remove_stale_metadata_files(store, current_paths)

    # Objects first: real .object-meta.xml (including CMT type definitions,
    # same format) must land before Field/Flow/PermissionSet indexing so
    # their stub fallback doesn't clobber real data — object_stub_seen
    # pre-seeded with real names is what makes _upsert_object_stub a no-op
    # for them.
    real_object_stats, real_object_names = _index_objects(store, object_files)
    objects_downgraded = _downgrade_stale_real_objects(store, real_object_names)
    object_stub_seen: set[str] = set(real_object_names)

    field_stats = _index_fields(store, field_files, include_formulas, object_stub_seen)
    flow_stats = _index_flows(store, flow_files, object_stub_seen)
    label_stats = _index_labels(store, label_files)
    # After fields: permission set fieldPermissions resolve against Field
    # nodes, which only exist once _index_fields has run.
    ps_stats = _index_permission_sets(store, permission_set_files, object_stub_seen)

    any_indexed = (
        field_stats["fields_indexed"] or flow_stats["flows_indexed"]
        or label_stats["labels_indexed"] or real_object_stats["objects_indexed"]
        or objects_downgraded or ps_stats["permission_sets_indexed"]
    )
    if any_indexed:
        store.commit()

    logger.info(
        "Metadata indexer: %d field(s), %d flow(s), %d label(s), %d object(s) "
        "(%d real, %d downgraded), %d permission set(s) (%d grant(s), %d unresolved), "
        "%d stale file(s) removed, %d stale label(s) removed, "
        "%d field reference edge(s) (%d unresolved), "
        "%d flow invoke edge(s) (%d unresolved), %d flow reference edge(s)",
        field_stats["fields_indexed"], flow_stats["flows_indexed"], label_stats["labels_indexed"],
        len(object_stub_seen), real_object_stats["objects_indexed"], objects_downgraded,
        ps_stats["permission_sets_indexed"], ps_stats["grants_created"],
        ps_stats["grants_unresolved"], stale_removed, label_stats["labels_removed"],
        field_stats["references_created"], field_stats["references_unresolved"],
        flow_stats["flow_invokes_created"], flow_stats["flow_invokes_unresolved"],
        flow_stats["flow_references_created"],
    )
    return {
        **field_stats,
        **flow_stats,
        **label_stats,
        **ps_stats,
        "objects_indexed": len(object_stub_seen),
        "real_objects_indexed": real_object_stats["objects_indexed"],
        "objects_downgraded": objects_downgraded,
        "stale_metadata_files_removed": stale_removed,
    }
