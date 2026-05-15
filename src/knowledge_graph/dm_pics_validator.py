"""DM XML → PICS validation map.

Parses all Matter Data Model XML files and builds a map from PICS prefix
(e.g. "OO") to ``ClusterPicsSchema`` — the complete set of server/client
attributes, commands, and features for that cluster.

Used by the PICS analysis pipeline to validate PICS codes in test cases.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# PICS code pattern: prefix.side.type + hex_id  e.g. OO.S.A0000
# Strips optional .Rsp suffix and (name) annotation before matching.
PICS_CODE_RE = re.compile(
    r'^([A-Z]{1,8})\.(S|C|M)\.(A|C|E|F)([0-9A-Fa-f]{1,8})$'
)

# Regex to extract raw PICS codes from free text (table cells, step text, etc.)
PICS_EXTRACT_RE = re.compile(
    r'\b([A-Z]{1,8}\.[SCM]\.[ACEF][0-9A-Fa-f]{1,8})'
    r'(?:\.Rsp)?(?:\([A-Za-z][A-Za-z0-9_.]*\))?'
)


@dataclass
class ClusterPicsSchema:
    """PICS metadata for one cluster derived from DM XML."""

    pics_code: str                                              # e.g. "OO"
    cluster_name: str                                          # e.g. "On/Off Cluster"
    server_attrs: Dict[int, str] = field(default_factory=dict)  # {0x0000: "OnOff"}
    client_attrs: Dict[int, str] = field(default_factory=dict)
    server_cmds:  Dict[int, str] = field(default_factory=dict)  # {0x00: "Off"}
    client_cmds:  Dict[int, str] = field(default_factory=dict)
    features: Dict[int, Tuple[str, str]] = field(default_factory=dict)  # {0: ("LT","Lighting")}
    # Mandatory entity sets — IDs whose conformance is mandatoryConform in the XML.
    # Mandatory entities don't require PICS declarations in test plans.
    mandatory_server_attrs: Set[int] = field(default_factory=set)
    mandatory_client_attrs: Set[int] = field(default_factory=set)
    mandatory_server_cmds:  Set[int] = field(default_factory=set)
    mandatory_client_cmds:  Set[int] = field(default_factory=set)
    # Disallowed entity sets — IDs whose conformance is disallowConform or deprecateConform
    # in the DM XML.  These entities are intentionally not available in derived clusters
    # and should NOT be flagged as missing PICS in test plans.
    disallowed_server_attrs: Set[int] = field(default_factory=set)
    disallowed_client_attrs: Set[int] = field(default_factory=set)
    disallowed_server_cmds:  Set[int] = field(default_factory=set)
    disallowed_client_cmds:  Set[int] = field(default_factory=set)
    # Base cluster name from DM XML hierarchy="derived" baseCluster="..." attribute.
    # Used internally during build_pics_map to inherit entities from base cluster schemas.
    base_cluster_name: str = field(default="")

    def format_schema_text(self) -> str:
        """Format DM schema as a human-readable block for LLM prompts.

        Mandatory entities are annotated with (M) — test cases do NOT need
        to declare PICS for these, as all devices must implement them.
        """
        lines: List[str] = []
        if self.server_attrs:
            lines.append("  Server Attributes:")
            for hid, name in sorted(self.server_attrs.items()):
                if hid in self.disallowed_server_attrs:
                    tag = " (X)"
                elif hid in self.mandatory_server_attrs:
                    tag = " (M)"
                else:
                    tag = " (O)"
                lines.append(f"    0x{hid:04X} {name}{tag}")
        if self.client_attrs:
            lines.append("  Client Attributes:")
            for hid, name in sorted(self.client_attrs.items()):
                if hid in self.disallowed_client_attrs:
                    tag = " (X)"
                elif hid in self.mandatory_client_attrs:
                    tag = " (M)"
                else:
                    tag = " (O)"
                lines.append(f"    0x{hid:04X} {name}{tag}")
        if self.server_cmds:
            lines.append("  Server Commands (commandToServer):")
            for hid, name in sorted(self.server_cmds.items()):
                if hid in self.disallowed_server_cmds:
                    tag = " (X)"
                elif hid in self.mandatory_server_cmds:
                    tag = " (M)"
                else:
                    tag = " (O)"
                lines.append(f"    0x{hid:02X} {name}{tag}")
        if self.client_cmds:
            lines.append("  Client Commands (commandToClient):")
            for hid, name in sorted(self.client_cmds.items()):
                if hid in self.disallowed_client_cmds:
                    tag = " (X)"
                elif hid in self.mandatory_client_cmds:
                    tag = " (M)"
                else:
                    tag = " (O)"
                lines.append(f"    0x{hid:02X} {name}{tag}")
        if self.features:
            lines.append("  Features:")
            for bit, (code, name) in sorted(self.features.items()):
                lines.append(f"    bit{bit} {code} {name}")
        return "\n".join(lines) if lines else "  (no attributes/commands/features in DM XML)"


@dataclass
class PicsValidationResult:
    """Result of validating a single PICS code against the DM schema."""
    code: str
    valid: bool
    error_type: Optional[str] = None   # "unknown_cluster" | "unknown_id" | "invalid_format"
    description: Optional[str] = None


def build_pics_map(dm_dir: Path) -> Dict[str, ClusterPicsSchema]:
    """Parse all DM XML files in *dm_dir* and return a map keyed by PICS prefix.

    Example::

        pics_map = build_pics_map(Path("data/data_model"))
        oo = pics_map["OO"]   # ClusterPicsSchema for On/Off Cluster
        print(oo.server_attrs)   # {0: "OnOff", 16384: "GlobalSceneControl", ...}
    """
    pics_map: Dict[str, ClusterPicsSchema] = {}
    for xml_path in sorted(dm_dir.glob("*.xml")):
        try:
            for schema in _parse_cluster_xml(xml_path):
                pics_map[schema.pics_code] = schema
        except Exception:
            pass  # skip malformed XML silently

    # Second pass: resolve hierarchy="derived" baseCluster inheritance.
    # Derived clusters (e.g. DishwasherAlarm → Alarm Base) carry no attributes/
    # commands/features of their own in the XML — they all live in the base cluster.
    # Build a name→code lookup so we can find the base schema.
    name_to_code: Dict[str, str] = {
        s.cluster_name.lower(): code for code, s in pics_map.items()
    }
    # Also index without trailing " cluster" suffix for fuzzy matching.
    name_to_code_no_suffix: Dict[str, str] = {
        s.cluster_name.lower().removesuffix(" cluster").strip(): code
        for code, s in pics_map.items()
    }

    for code, schema in pics_map.items():
        if not schema.base_cluster_name:
            continue
        base_name_lower = schema.base_cluster_name.lower()
        base_code = (
            name_to_code.get(base_name_lower)
            or name_to_code.get(base_name_lower + " cluster")
            or name_to_code_no_suffix.get(base_name_lower)
            or name_to_code_no_suffix.get(base_name_lower.removesuffix(" base").strip())
        )
        if not base_code or base_code not in pics_map:
            continue
        base = pics_map[base_code]
        # Merge base entities into derived — derived-specific entries take priority.
        for aid, name in base.server_attrs.items():
            schema.server_attrs.setdefault(aid, name)
        for aid, name in base.client_attrs.items():
            schema.client_attrs.setdefault(aid, name)
        for cid, name in base.server_cmds.items():
            schema.server_cmds.setdefault(cid, name)
        for cid, name in base.client_cmds.items():
            schema.client_cmds.setdefault(cid, name)
        for bit, feat in base.features.items():
            schema.features.setdefault(bit, feat)
        schema.mandatory_server_attrs |= base.mandatory_server_attrs
        schema.mandatory_client_attrs |= base.mandatory_client_attrs
        schema.mandatory_server_cmds  |= base.mandatory_server_cmds
        schema.mandatory_client_cmds  |= base.mandatory_client_cmds
        schema.disallowed_server_attrs |= base.disallowed_server_attrs
        schema.disallowed_client_attrs |= base.disallowed_client_attrs
        schema.disallowed_server_cmds  |= base.disallowed_server_cmds
        schema.disallowed_client_cmds  |= base.disallowed_client_cmds

    return pics_map


def extract_pics_codes_from_text(text: str) -> List[str]:
    """Extract all PICS-like codes from free text.

    Returns deduplicated list of raw codes without name annotations,
    e.g. ["OO.S.A0000", "OO.S.F00"] from "OO.S.A0000(OnOff) OO.S.F00(LT)".
    """
    raw_codes = PICS_EXTRACT_RE.findall(text)
    return list(dict.fromkeys(raw_codes))  # deduplicate, preserve order


def validate_pics_code(
    code: str,
    pics_map: Dict[str, ClusterPicsSchema],
) -> PicsValidationResult:
    """Validate a PICS code string against the DM schema map.

    Returns a :class:`PicsValidationResult`.  Notes:

    * Protocol-level PICS (BLE, Thread, WiFi, TCP, QR, commissioning) are not
      in the DM XML — they return ``error_type="unknown_cluster"`` with
      ``valid=True``.  Callers should treat these as *unvalidatable* rather
      than invalid.
    * Codes failing the format regex return ``valid=False``.
    """
    # Strip annotation e.g. "OO.S.A0000(OnOff)" → "OO.S.A0000"
    raw = re.sub(r'\(.*?\)', '', code).strip()
    # Strip .Rsp suffix
    raw = re.sub(r'\.Rsp$', '', raw, flags=re.I)

    m = PICS_CODE_RE.match(raw)
    if not m:
        return PicsValidationResult(
            code=code, valid=False, error_type="invalid_format",
            description=f"'{code}' does not match PICS code format X.S.AXXX",
        )

    prefix, side, entity_type, hex_id_str = m.group(1), m.group(2), m.group(3), m.group(4)

    if prefix not in pics_map:
        # Could be a protocol-level PICS (BLE, Thread, TCP, etc.) — not invalid
        return PicsValidationResult(
            code=code, valid=True, error_type="unknown_cluster",
            description=(
                f"Cluster prefix '{prefix}' not in DM XML "
                "(likely a protocol-level or commissioning PICS)"
            ),
        )

    schema = pics_map[prefix]
    try:
        entity_id = int(hex_id_str, 16)
    except ValueError:
        return PicsValidationResult(
            code=code, valid=False, error_type="invalid_format",
            description=f"Cannot parse hex entity ID '{hex_id_str}'",
        )

    if entity_type == "A":   # Attribute
        pool = schema.server_attrs if side == "S" else schema.client_attrs
        if entity_id not in pool and entity_id not in schema.server_attrs:
            return PicsValidationResult(
                code=code, valid=False, error_type="unknown_id",
                description=f"Attribute 0x{entity_id:04X} not in {prefix} {side} schema",
            )
    elif entity_type == "C":  # Command
        if side == "S" and entity_id not in schema.server_cmds:
            return PicsValidationResult(
                code=code, valid=False, error_type="unknown_id",
                description=f"0x{entity_id:02X} is not a server-side command in {prefix}",
            )
        if side == "C" and entity_id not in schema.client_cmds:
            # Some responses are in server_cmds; allow if found there
            if entity_id not in schema.server_cmds:
                return PicsValidationResult(
                    code=code, valid=False, error_type="unknown_id",
                    description=f"0x{entity_id:02X} not found as a command in {prefix}",
                )
    elif entity_type == "F":  # Feature
        if entity_id not in schema.features:
            return PicsValidationResult(
                code=code, valid=False, error_type="unknown_id",
                description=f"Feature bit {entity_id} not in {prefix} schema",
            )

    return PicsValidationResult(code=code, valid=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_cluster_xml(xml_path: Path) -> List[ClusterPicsSchema]:
    """Parse one DM XML cluster file into a list of :class:`ClusterPicsSchema`.

    Returns a list because a single XML file can define multiple concrete cluster
    aliases via a ``<clusterIds>`` block (e.g. ConcentrationMeasurement.xml
    defines CMOCONC, CDOCONC, … and ResourceMonitoring.xml defines HEPAFREMON,
    ACFREMON, WTLREPMON).  The base schema (keyed by the ``<classification
    picsCode=…>`` attribute) is always first; alias schemas follow.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError:
        return []

    classif = root.find("classification")
    if classif is None:
        return []

    pics_code = classif.get("picsCode", "").strip()
    if not pics_code:
        return []

    cluster_name = root.get("name", xml_path.stem)
    schema = ClusterPicsSchema(pics_code=pics_code, cluster_name=cluster_name)

    # Capture base cluster name for hierarchy="derived" clusters (second-pass merge).
    base_cluster = classif.get("baseCluster", "").strip()
    if base_cluster:
        schema.base_cluster_name = base_cluster

    # Attributes — always server-side in the DM XML model
    for attr in root.iter("attribute"):
        attr_id_str = attr.get("id", "")
        attr_name   = attr.get("name", "")
        if not attr_id_str or not attr_name:
            continue
        try:
            aid = int(attr_id_str, 16) if attr_id_str.startswith("0x") else int(attr_id_str)
        except ValueError:
            continue
        schema.server_attrs[aid] = attr_name
        if any(c.tag == "mandatoryConform" for c in attr):
            schema.mandatory_server_attrs.add(aid)
        if any(c.tag in ("disallowConform", "deprecateConform") for c in attr):
            schema.disallowed_server_attrs.add(aid)

    # Commands — direction determines server vs client
    for cmd in root.iter("command"):
        cmd_id_str = cmd.get("id", "")
        cmd_name   = cmd.get("name", "")
        direction  = cmd.get("direction", "commandToServer")
        if not cmd_id_str or not cmd_name:
            continue
        try:
            cid = int(cmd_id_str, 16) if cmd_id_str.startswith("0x") else int(cmd_id_str)
        except ValueError:
            continue
        is_mandatory = any(c.tag == "mandatoryConform" for c in cmd)
        is_disallowed = any(c.tag in ("disallowConform", "deprecateConform") for c in cmd)
        if direction == "commandToClient":
            schema.client_cmds[cid] = cmd_name
            if is_mandatory:
                schema.mandatory_client_cmds.add(cid)
            if is_disallowed:
                schema.disallowed_client_cmds.add(cid)
        else:
            schema.server_cmds[cid] = cmd_name
            if is_mandatory:
                schema.mandatory_server_cmds.add(cid)
            if is_disallowed:
                schema.disallowed_server_cmds.add(cid)

    # Features — bit + code + name
    for feat in root.iter("feature"):
        bit_str = feat.get("bit", "")
        code    = feat.get("code", "")
        name    = feat.get("name", "")
        if not bit_str or not code:
            continue
        try:
            bit = int(bit_str)
        except ValueError:
            continue
        schema.features[bit] = (code, name)

    results: List[ClusterPicsSchema] = [schema]

    # Build alias schemas from <clusterIds><clusterId picsCode="X" name="Y"/>.
    # Each alias shares all attributes/commands/features of the base cluster.
    cluster_ids_el = root.find("clusterIds")
    if cluster_ids_el is not None:
        for cid_el in cluster_ids_el.findall("clusterId"):
            alias_pics = cid_el.get("picsCode", "").strip()
            alias_name = cid_el.get("name", "").strip()
            if not alias_pics or not alias_name or alias_pics == pics_code:
                continue
            alias = ClusterPicsSchema(
                pics_code=alias_pics,
                cluster_name=alias_name,
                server_attrs=dict(schema.server_attrs),
                client_attrs=dict(schema.client_attrs),
                server_cmds=dict(schema.server_cmds),
                client_cmds=dict(schema.client_cmds),
                features=dict(schema.features),
                mandatory_server_attrs=set(schema.mandatory_server_attrs),
                mandatory_client_attrs=set(schema.mandatory_client_attrs),
                mandatory_server_cmds=set(schema.mandatory_server_cmds),
                mandatory_client_cmds=set(schema.mandatory_client_cmds),
                disallowed_server_attrs=set(schema.disallowed_server_attrs),
                disallowed_client_attrs=set(schema.disallowed_client_attrs),
                disallowed_server_cmds=set(schema.disallowed_server_cmds),
                disallowed_client_cmds=set(schema.disallowed_client_cmds),
            )
            results.append(alias)

    return results
