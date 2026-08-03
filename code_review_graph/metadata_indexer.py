"""Salesforce metadata indexer — field formulas and object references.

Parses ``*.field-meta.xml`` under configured paths and creates ``Field`` nodes
with formula text in ``extra``, plus ``REFERENCES`` edges to related objects
and fields mentioned in formulas.
"""

from __future__ import annotations

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
        return section

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
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "fullName" and elem.text:
            full_name = elem.text.strip()
        elif tag == "formula" and elem.text:
            formula = elem.text.strip()

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
    }


def _field_refs(formula: str) -> set[str]:
    if not formula:
        return set()
    return set(_FIELD_REF_RE.findall(formula))


def index_salesforce_metadata(store: GraphStore, repo_root: Path) -> dict:
    """Index Salesforce field metadata into the graph store."""
    config = _load_metadata_config(repo_root)
    if not config:
        return {"fields_indexed": 0, "references_created": 0}

    paths = config.get("paths") or list(DEFAULT_METADATA_PATHS)
    include_formulas = config.get("include_formulas", True)
    fields_indexed = 0
    references_created = 0

    for rel in paths:
        base = repo_root / rel
        if not base.is_dir():
            continue
        for meta_path in base.rglob("*.field-meta.xml"):
            info = _parse_field_meta(meta_path)
            if not info:
                continue

            field_qn = f"{info['file_path']}::{info['name']}"
            extra: dict = {"metadata_type": "CustomField"}
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
                    parent_name=info.get("parent_name"),
                    extra=extra,
                )
            )
            fields_indexed += 1

            if info.get("formula"):
                for ref in _field_refs(info["formula"]):
                    store.upsert_edge(
                        EdgeInfo(
                            kind="REFERENCES",
                            source=field_qn,
                            target=ref,
                            file_path=info["file_path"],
                            line=1,
                            extra={"from_formula": True},
                        )
                    )
                    references_created += 1

    if fields_indexed:
        store.commit()

    logger.info(
        "Metadata indexer: %d field(s), %d reference edge(s)",
        fields_indexed,
        references_created,
    )
    return {
        "fields_indexed": fields_indexed,
        "references_created": references_created,
    }
