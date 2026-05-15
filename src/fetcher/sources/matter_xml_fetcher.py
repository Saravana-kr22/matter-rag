"""Matter Data Model XML fetcher — parses CSA Matter DM XML files.

Reads ``*.xml`` files from a local directory and produces one ``FetchedDocument``
per cluster definition found in the XML.  The document content is a human-readable
text summary of the cluster (used for text embeddings), while the structured schema
is stored in ``doc.metadata['schema']`` for direct ingestion into the knowledge graph
without regex guessing.

Supported XML formats:

  **DevX / CSA format** (current — one cluster per file, or multiple clusters in one file):

      <cluster id="0x0006" name="On/Off" revision="6">
        <features>
          <feature bit="0" code="LT" name="Lighting" summary="..." conformance="O"/>
        </features>
        <attributes>
          <attribute id="0x0000" name="OnOff" type="boolean" default="FALSE"
                     access="R V" conformance="M" quality="N"/>
        </attributes>
        <commands>
          <command id="0x00" name="Off" direction="commandToServer" response="Y"/>
        </commands>
        <events>
          <event id="0x00" name="SwitchLatched" priority="info" conformance="O"/>
        </events>
      </cluster>

Usage (via sources.json)::

    {
      "id": "data_model",
      "type": "matter_xml",
      "role": "data_model",
      "path": "data/data_model"
    }

Each returned ``FetchedDocument`` has:

  - ``path``    — relative path of the XML file (with ``::ClusterName`` suffix)
  - ``content`` — plain-text description of the cluster (for embeddings)
  - ``metadata["schema"]`` — structured dict::

        {
          "cluster_id": "0x0006",
          "cluster_name": "On/Off",
          "revision": "6",
          "attributes": [
            {"id": "0x0000", "name": "OnOff", "type": "boolean",
             "access": "R V", "conformance": "M", "quality": "N", "default": "FALSE"}
          ],
          "commands": [
            {"id": "0x00", "name": "Off", "direction": "commandToServer", "response": "Y"}
          ],
          "events": [
            {"id": "0x00", "name": "SwitchLatched", "priority": "info", "conformance": "O"}
          ],
          "features": [
            {"bit": "0", "code": "LT", "name": "Lighting", "conformance": "O"}
          ]
        }
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config.config_loader import AppConfig
from src.fetcher.base_fetcher import BaseFetcher, FetchedDocument, resolve_config_vars

logger = logging.getLogger(__name__)


class MatterXMLFetcher(BaseFetcher):
    """Parse Matter data-model XML files and return one FetchedDocument per cluster."""

    def __init__(self, path: str, extra_metadata: Optional[Dict] = None) -> None:
        self._path = Path(path)
        self._extra_metadata = extra_metadata or {}

    @classmethod
    def source_type(cls) -> str:
        return "matter_xml"

    @classmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "MatterXMLFetcher":
        cfg = resolve_config_vars(source_cfg)
        return cls(
            path=cfg.get("path", "data/data_model"),
            extra_metadata=cfg.get("metadata", {}),
        )

    def fetch(self) -> List[FetchedDocument]:
        if not self._path.exists():
            raise FileNotFoundError(
                f"[MatterXMLFetcher] Data model directory not found: {self._path.resolve()}"
            )

        docs: List[FetchedDocument] = []
        xml_files = list(self._path.rglob("*.xml"))
        logger.info("[MatterXMLFetcher] Found %d XML file(s) in %s", len(xml_files), self._path)

        for xml_file in xml_files:
            try:
                clusters = self._parse_xml(xml_file)
                for cluster in clusters:
                    doc = self._cluster_to_document(xml_file, cluster)
                    docs.append(doc)
            except Exception as exc:
                logger.warning("[MatterXMLFetcher] Failed to parse %s: %s", xml_file, exc)

        logger.info(
            "[MatterXMLFetcher] Loaded %d cluster definition(s) from %d XML file(s)",
            len(docs), len(xml_files),
        )
        return docs

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    def _parse_xml(self, xml_file: Path) -> List[Dict[str, Any]]:
        """Parse one XML file and return a list of cluster schema dicts."""
        import xml.etree.ElementTree as ET

        tree = ET.parse(xml_file)
        root = tree.getroot()

        # Strip namespace prefix for consistent tag matching
        tag = self._strip_ns(root.tag)

        clusters: List[Dict[str, Any]] = []

        if tag == "cluster":
            # Single-cluster file
            c = self._parse_cluster_element(root)
            if c:
                clusters.append(c)
        elif tag in ("clusters", "configurator", "matter", "zigbee"):
            # Multi-cluster file
            for el in root.iter():
                if self._strip_ns(el.tag) == "cluster":
                    c = self._parse_cluster_element(el)
                    if c:
                        clusters.append(c)
        else:
            # Try to find cluster elements anywhere in the tree
            for el in root.iter():
                if self._strip_ns(el.tag) == "cluster":
                    c = self._parse_cluster_element(el)
                    if c:
                        clusters.append(c)

        # Fill in missing conformance for derived clusters by inheriting from base cluster XML.
        # E.g. Mode-derived clusters (WaterHeaterMode, OvenMode …) list SupportedModes and
        # CurrentMode without a conformance child — they inherit "M" from Mode Base.
        for cluster in clusters:
            self._apply_base_cluster_inheritance(xml_file, cluster)

        return clusters

    def _apply_base_cluster_inheritance(self, xml_file: Path, schema: Dict[str, Any]) -> None:
        """Fill in missing entities and fields from the base cluster XML (if derived)."""
        import xml.etree.ElementTree as ET

        # Detect base cluster name from <classification hierarchy="derived" baseCluster="..."/>
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
        except Exception:
            return

        base_cluster_name = ""
        for el in root.iter():
            if self._strip_ns(el.tag) == "classification":
                if el.get("hierarchy", "") == "derived":
                    base_cluster_name = el.get("baseCluster", "").strip()
                break

        if not base_cluster_name:
            return  # not a derived cluster

        # Locate base cluster XML in the same directory
        base_schema = self._load_base_cluster_schema(xml_file.parent, base_cluster_name)
        if base_schema is None:
            return

        # Fields to inherit per entity type (only fill in when the derived entity has an empty value)
        _INHERIT_FIELDS = {
            "attributes": ["conformance", "access", "type", "default", "quality"],
            "commands":   ["conformance", "access", "direction", "response"],
            "events":     ["conformance", "access", "priority"],
            "features":   ["conformance", "summary"],
        }

        for entity_type, fields in _INHERIT_FIELDS.items():
            base_map = {e["name"]: e for e in base_schema.get(entity_type, []) if e.get("name")}
            for entity in schema.get(entity_type, []):
                base_entity = base_map.get(entity.get("name", ""))
                if not base_entity:
                    continue
                for field in fields:
                    if not entity.get(field) and base_entity.get(field):
                        entity[field] = base_entity[field]

        # Promote base entities that are completely absent from the derived cluster.
        # Dishwasher Alarm / Refrigerator Alarm (for example) inherit ALL entities
        # from AlarmBase because their XML only lists classification metadata.
        for entity_type in ("attributes", "commands", "events", "features"):
            derived_names = {e.get("name", "") for e in schema.get(entity_type, [])}
            for base_entity in base_schema.get(entity_type, []):
                if base_entity.get("name") and base_entity["name"] not in derived_names:
                    schema.setdefault(entity_type, []).append(dict(base_entity))

    def _load_base_cluster_schema(self, directory: Path, base_cluster_name: str) -> Optional[Dict[str, Any]]:
        """Find and parse the base cluster XML, returning its schema dict (or None)."""
        import xml.etree.ElementTree as ET

        base_lower = base_cluster_name.lower()
        for xml_file in directory.glob("*.xml"):
            try:
                tree = ET.parse(xml_file)
                root = tree.getroot()
                # Match by cluster name attribute (may be "X Cluster" or just "X")
                cname = root.get("name", "").lower()
                if cname == base_lower or cname == f"{base_lower} cluster":
                    clusters = []
                    if self._strip_ns(root.tag) == "cluster":
                        c = self._parse_cluster_element(root)
                        if c:
                            return c
                    else:
                        for el in root.iter():
                            if self._strip_ns(el.tag) == "cluster":
                                c = self._parse_cluster_element(el)
                                if c:
                                    return c
            except Exception:
                continue
        return None

    def _parse_cluster_element(self, el) -> Optional[Dict[str, Any]]:
        """Extract schema dict from a <cluster> element."""
        import xml.etree.ElementTree as ET

        cluster_id   = el.get("id", "")
        cluster_name = el.get("name", "")

        if not cluster_name:
            # Try child <name> element
            name_el = el.find("name") or el.find("{*}name")
            if name_el is not None and name_el.text:
                cluster_name = name_el.text.strip()

        if not cluster_name:
            return None

        # Read cluster aliases from <clusterIds><clusterId id="0x0405" name="..." picsCode="..."/>
        # Base-cluster XML files (WaterContentMeasurement, ConcentrationMeasurement, etc.)
        # define concrete cluster IDs / names in a child <clusterIds> block.
        cluster_aliases: List[Dict[str, str]] = []
        if not cluster_id:
            for child in el.iter():
                if self._strip_ns(child.tag) == "clusterId":
                    cid  = child.get("id", "").strip()
                    cname = child.get("name", "").strip()
                    cpics = child.get("picsCode", "").strip()
                    if cid:
                        cluster_aliases.append({"id": cid, "name": cname, "picsCode": cpics})
            if cluster_aliases:
                cluster_id = cluster_aliases[0]["id"] if len(cluster_aliases) == 1 \
                    else ", ".join(a["id"] for a in cluster_aliases)

        revision = el.get("revision", el.get("rev", ""))

        schema: Dict[str, Any] = {
            "cluster_id":      cluster_id,
            "cluster_name":    cluster_name,
            "revision":        revision,
            "pics_code":       "",
            "hierarchy":       "",
            "base_cluster":    "",
            "cluster_aliases": cluster_aliases,  # [{id, name, picsCode}] for base clusters
            "attributes":      [],
            "commands":        [],
            "events":          [],
            "features":        [],
        }

        # ---- PICS code + hierarchy from <classification ...> ----
        for classif in el.iter():
            if self._strip_ns(classif.tag) == "classification":
                pics_code = classif.get("picsCode", "").strip()
                if pics_code:
                    schema["pics_code"] = pics_code
                schema["hierarchy"] = classif.get("hierarchy", "").strip()
                schema["base_cluster"] = classif.get("baseCluster", "").strip()
                break

        # ---- Attributes ----
        for attr_parent in [el] + list(el.iter()):
            if self._strip_ns(attr_parent.tag) in ("attributes", "attributeList"):
                for attr in attr_parent:
                    a = self._parse_entity(attr, ["id", "name", "type", "access",
                                                   "conformance", "quality", "default",
                                                   "nullable", "privilege"])
                    if a.get("name"):
                        schema["attributes"].append(a)
                break

        # Fallback: attributes directly under cluster
        for attr in el:
            if self._strip_ns(attr.tag) == "attribute":
                a = self._parse_entity(attr, ["id", "name", "type", "access",
                                               "conformance", "quality", "default"])
                if a.get("name") and a not in schema["attributes"]:
                    schema["attributes"].append(a)

        # ---- Commands ----
        for cmd_parent in [el] + list(el.iter()):
            if self._strip_ns(cmd_parent.tag) in ("commands", "commandList"):
                for cmd in cmd_parent:
                    c = self._parse_entity(cmd, ["id", "name", "direction",
                                                   "response", "conformance", "access"])
                    if c.get("name"):
                        schema["commands"].append(c)
                break

        for cmd in el:
            if self._strip_ns(cmd.tag) == "command":
                c = self._parse_entity(cmd, ["id", "name", "direction",
                                              "response", "conformance"])
                if c.get("name") and c not in schema["commands"]:
                    schema["commands"].append(c)

        # ---- Events ----
        for evt_parent in [el] + list(el.iter()):
            if self._strip_ns(evt_parent.tag) in ("events", "eventList"):
                for evt in evt_parent:
                    e = self._parse_entity(evt, ["id", "name", "priority",
                                                   "conformance", "access"])
                    if e.get("name"):
                        schema["events"].append(e)
                break

        for evt in el:
            if self._strip_ns(evt.tag) == "event":
                e = self._parse_entity(evt, ["id", "name", "priority", "conformance"])
                if e.get("name") and e not in schema["events"]:
                    schema["events"].append(e)

        # ---- Features ----
        for feat_parent in [el] + list(el.iter()):
            if self._strip_ns(feat_parent.tag) in ("features", "feature"):
                if self._strip_ns(feat_parent.tag) == "feature":
                    # Direct feature element
                    f = self._parse_entity(feat_parent, ["bit", "code", "name",
                                                          "summary", "conformance"])
                    if f.get("name"):
                        schema["features"].append(f)
                else:
                    for feat in feat_parent:
                        f = self._parse_entity(feat, ["bit", "code", "name",
                                                       "summary", "conformance"])
                        if f.get("name"):
                            schema["features"].append(f)
                break

        # Deduplicate by name
        for key in ("attributes", "commands", "events", "features"):
            seen_names: set = set()
            deduped = []
            for item in schema[key]:
                n = item.get("name", "")
                if n and n not in seen_names:
                    seen_names.add(n)
                    deduped.append(item)
            schema[key] = deduped

        return schema

    @staticmethod
    def _parse_entity(el, attrs: List[str]) -> Dict[str, str]:
        """Extract a subset of XML attributes from an element.

        Handles both the legacy inline-attribute format (``access="R V" conformance="M"``)
        and the newer CSA DevX child-element format where ``<access read="true"…/>`` and
        ``<mandatoryConform/>``, ``<optionalConform/>``, etc. are child elements.
        """
        result: Dict[str, str] = {}
        for a in attrs:
            v = el.get(a, "")
            if v:
                result[a] = v

        # Also try child <name> text if name attr missing
        if not result.get("name"):
            from xml.etree.ElementTree import Element
            name_el = el.find("name")
            if name_el is not None and name_el.text:
                result["name"] = name_el.text.strip()

        # ── Child-element format (CSA DevX / newer Matter XML) ────────────────
        # access: <access read="true" write="true" readPrivilege="view" writePrivilege="operate"/>
        #         <access invokePrivilege="operate"/>  (commands only)
        if "access" in attrs and not result.get("access"):
            acc_el = el.find("access")
            if acc_el is not None:
                parts = []
                if acc_el.get("read", "").lower() in ("true", "1"):
                    parts.append("R")
                if acc_el.get("write", "").lower() in ("true", "1"):
                    parts.append("W")
                invoke_priv = acc_el.get("invokePrivilege", "")
                read_priv   = acc_el.get("readPrivilege", "")
                write_priv  = acc_el.get("writePrivilege", "")
                if invoke_priv:
                    parts.append(f"invoke:{invoke_priv}")
                if read_priv or write_priv:
                    priv_parts = []
                    if read_priv:
                        priv_parts.append(f"r:{read_priv[0].upper()}")
                    if write_priv:
                        priv_parts.append(f"w:{write_priv[0].upper()}")
                    parts.append(f"[{' '.join(priv_parts)}]")
                if parts:
                    result["access"] = " ".join(parts)

        # conformance: <mandatoryConform/> <optionalConform/> <provisionalConform/>
        #              <disabledConform/>  <deprecateConform/>  <otherwiseConform/>
        # Feature conditions are captured as "[FeatureName]" suffix, e.g. "M [LT]" or "O [FQ | LT]".
        if "conformance" in attrs and not result.get("conformance"):
            _CONFORM_MAP = {
                "mandatoryConform":   "M",
                "optionalConform":    "O",
                "provisionalConform": "P",
                "disabledConform":    "D",
                "deprecateConform":   "deprecated",
                "otherwiseConform":   "O",
                "disallowConform":    "disallowed",
                "describedConform":   "see text",
                "notConform":         "N/A",
            }
            for child in el:
                tag = child.tag
                if "}" in tag:
                    tag = tag.split("}", 1)[1]
                code = _CONFORM_MAP.get(tag)
                if code:
                    feature_conds = MatterXMLFetcher._extract_feature_conds(child)
                    if feature_conds:
                        result["conformance"] = f"{code} [{' | '.join(feature_conds)}]"
                    else:
                        result["conformance"] = code
                    break

        return result

    @staticmethod
    def _extract_feature_conds(conform_el) -> List[str]:
        """Collect feature names from inside a conformance element (BFS).

        Handles direct ``<feature name="X"/>`` children and nested
        ``<orTerm>``, ``<andTerm>``, ``<notTerm>`` containers.
        """
        found: List[str] = []
        stack = list(conform_el)
        while stack:
            el = stack.pop()
            tag = el.tag
            if "}" in tag:
                tag = tag.split("}", 1)[1]
            if tag == "feature":
                fname = el.get("name", "")
                if fname and fname not in found:
                    found.append(fname)
            elif tag in ("orTerm", "andTerm", "notTerm", "otherwiseConform"):
                stack.extend(list(el))
        return found

    @staticmethod
    def _strip_ns(tag: str) -> str:
        """Remove XML namespace prefix: ``{http://...}cluster`` → ``cluster``."""
        if tag.startswith("{"):
            return tag.split("}", 1)[1]
        return tag

    # ------------------------------------------------------------------
    # Document construction
    # ------------------------------------------------------------------

    def _cluster_to_document(self, xml_file: Path, schema: Dict[str, Any]) -> FetchedDocument:
        """Convert a cluster schema dict into a FetchedDocument."""
        cluster_name = schema["cluster_name"]
        cluster_id   = schema.get("cluster_id", "")
        revision     = schema.get("revision", "")

        # Build human-readable text summary (for embeddings / text search)
        lines: List[str] = [
            f"Cluster: {cluster_name}" + (f" (id={cluster_id})" if cluster_id else ""),
        ]
        if revision:
            lines.append(f"Revision: {revision}")

        if schema["features"]:
            lines.append("\nFeatures:")
            for f in schema["features"]:
                lines.append(
                    f"  [{f.get('code','')}] bit={f.get('bit','')} {f.get('name','')} "
                    f"({f.get('conformance','')})"
                )

        if schema["attributes"]:
            lines.append("\nAttributes:")
            for a in schema["attributes"]:
                lines.append(
                    f"  id={a.get('id','')} {a.get('name','')} type={a.get('type','')} "
                    f"access={a.get('access','')} conformance={a.get('conformance','')} "
                    f"quality={a.get('quality','')} default={a.get('default','')}"
                )

        if schema["commands"]:
            lines.append("\nCommands:")
            for c in schema["commands"]:
                lines.append(
                    f"  id={c.get('id','')} {c.get('name','')} "
                    f"direction={c.get('direction','')} response={c.get('response','')} "
                    f"conformance={c.get('conformance','')}"
                )

        if schema["events"]:
            lines.append("\nEvents:")
            for e in schema["events"]:
                lines.append(
                    f"  id={e.get('id','')} {e.get('name','')} "
                    f"priority={e.get('priority','')} conformance={e.get('conformance','')}"
                )

        content = "\n".join(lines)

        # Relative path: file.xml::ClusterName
        rel_path = str(xml_file.relative_to(self._path) if xml_file.is_absolute() else xml_file)
        slug = cluster_name.replace(" ", "_").replace("/", "_")
        doc_path = f"{rel_path}::{slug}"

        metadata: Dict[str, Any] = {
            "source":         "data_model",
            "source_id":      "matter_xml",
            "doc_type":       "data_model",
            "cluster_name":   cluster_name,
            "cluster_id":     cluster_id,
            "revision":       revision,
            "absolute_path":  str(xml_file.resolve()),
            "_process_rules": [],
            "schema":         schema,   # full structured schema for KG ingestion
            **self._extra_metadata,
        }

        return FetchedDocument(path=doc_path, content=content, metadata=metadata)
