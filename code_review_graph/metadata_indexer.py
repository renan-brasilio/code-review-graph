"""Salesforce metadata indexer — field formulas and object references.

Parses ``*.field-meta.xml`` under configured paths and creates ``Field`` nodes
with formula text in ``extra``, plus ``REFERENCES`` edges to related objects
and fields mentioned in formulas.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .graph import GraphStore

from .parser import EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)

_SF_NS = "http://soap.sforce.com/2006/04/metadata"
_FIELD_REF_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*__(?:c|r))\b")

DEFAULT_METADATA_PATHS = ("force-app/main/default/objects",)


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


def _parse_field_meta(path: Path) -> Optional[dict]:
    try:
        tree = ET.parse(path)
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


def index_salesforce_metadata(store: GraphStore, repo_root: Path) -> dict:
    """Index Salesforce field metadata into the graph store."""
    config = _load_metadata_config(repo_root)
    if not config:
        return {"fields_indexed": 0, "references_created": 0, "references_unresolved": 0}

    paths = config.get("paths") or list(DEFAULT_METADATA_PATHS)
    include_formulas = config.get("include_formulas", True)

    # Pass 1: parse every field first so cross-field/cross-object lookups
    # (relationship traversal, sibling-field formula refs) can resolve
    # regardless of file discovery order.
    parsed: list[dict] = []
    seen_meta_paths: set[str] = set()
    for rel in paths:
        base = repo_root / rel
        if not base.is_dir():
            continue
        for meta_path in base.rglob("*.field-meta.xml"):
            key = str(meta_path)
            if key in seen_meta_paths:
                continue
            seen_meta_paths.add(key)
            info = _parse_field_meta(meta_path)
            if info:
                parsed.append(info)

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

    if fields_indexed:
        store.commit()

    logger.info(
        "Metadata indexer: %d field(s), %d reference edge(s) (%d unresolved)",
        fields_indexed,
        references_created,
        references_unresolved,
    )
    return {
        "fields_indexed": fields_indexed,
        "references_created": references_created,
        "references_unresolved": references_unresolved,
    }
