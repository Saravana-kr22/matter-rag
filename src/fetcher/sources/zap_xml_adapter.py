"""ZAP XML format adapter — converts <configurator> XML to standard DM XML.

ZAP-style XML files use a ``<configurator>`` root element with either:
- ``<clusterExtension>`` children that extend an existing cluster by its hex ID
- ``<cluster>`` children that define standalone new clusters (with <name> and <code> sub-elements)

This adapter converts both formats into the standard CSA DevX DM XML format that
``MatterXMLFetcher`` can parse. Device type XMLs (containing ``<deviceType>``) are
detected and skipped via ``is_device_type_xml()``.

ZAP clusterExtension format::

    <configurator>
      <clusterExtension code="0x0201">
        <attribute side="server" code="0xFFF10002" type="boolean"
                   writable="true" optional="true">AttrName</attribute>
        <command source="client" code="0xFFF100F0" name="CmdName">
          <arg name="X" type="Y"/>
        </command>
        <event side="server" code="0xFFF10000" name="EventName" priority="info">
          <field id="0" name="X" type="Y"/>
        </event>
      </clusterExtension>
    </configurator>

ZAP standalone cluster format::

    <configurator>
      <cluster>
        <name>MyCluster</name>
        <code>0xFFF1FC07</code>
        <attribute code="0x0000" side="server" name="Attr" type="int16u" .../>
        <command source="client" code="0x00" name="Cmd">...</command>
        <event side="server" code="0x00" name="Evt" priority="info">...</event>
      </cluster>
    </configurator>

Output: standard DM XML ``<cluster>`` element(s) for the KG.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def convert_zap_to_dm_xml(
    zap_xml_path: Path,
    cluster_name_lookup: Optional[Dict[int, str]] = None,
    pics_code_overrides: Optional[Dict[str, str]] = None,
) -> str:
    """Convert a ZAP-format XML to standard DM XML format.

    Handles both standalone <cluster> definitions and <clusterExtension> elements.

    Args:
        zap_xml_path: Path to the ZAP XML file
        cluster_name_lookup: Optional {cluster_id_int: cluster_name} for resolving
            extension cluster names (standalone clusters have their own <name>)

    Returns:
        String containing the equivalent DM XML content (one <cluster> per definition)
    """
    if cluster_name_lookup is None:
        cluster_name_lookup = {}

    tree = ET.parse(zap_xml_path)
    root = tree.getroot()

    clusters_xml: List[str] = []

    # Collect enums, bitmaps, and structs by cluster code
    enums_by_cluster: Dict[str, List[ET.Element]] = {}
    bitmaps_by_cluster: Dict[str, List[ET.Element]] = {}
    structs_by_cluster: Dict[str, List[ET.Element]] = {}

    for child in root:
        tag = _strip_ns(child.tag)
        if tag == "enum":
            cluster_code = _get_child_cluster_code(child)
            if cluster_code:
                enums_by_cluster.setdefault(cluster_code, []).append(child)
        elif tag == "bitmap":
            cluster_code = _get_child_cluster_code(child)
            if cluster_code:
                bitmaps_by_cluster.setdefault(cluster_code, []).append(child)
        elif tag == "struct":
            cluster_code = _get_child_cluster_code(child)
            if cluster_code:
                structs_by_cluster.setdefault(cluster_code, []).append(child)

    # Process standalone <cluster> definitions (new clusters, not extensions)
    for cluster_el in root:
        if _strip_ns(cluster_el.tag) != "cluster":
            continue

        cluster_name, cluster_id_hex, pics_code, attributes, commands, events = _parse_zap_standalone_cluster(cluster_el)
        if not cluster_name or not cluster_id_hex:
            continue

        # Apply PICS code override if provided
        if pics_code_overrides:
            for _override_key, _override_val in pics_code_overrides.items():
                if _override_key.lower() in cluster_name.lower() or cluster_name.lower() in _override_key.lower():
                    pics_code = _override_val
                    break

        lines: List[str] = []
        lines.append(
            f'<cluster xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            f' name="{_escape_xml(cluster_name)}" id="{cluster_id_hex}" revision="1">'
        )

        if pics_code:
            lines.append(
                f'  <classification hierarchy="base" role="application" picsCode="{pics_code}" scope="Endpoint"/>'
            )

        if attributes:
            lines.append("  <attributes>")
            for attr in attributes:
                attr_id = attr["id"]
                attr_name = _escape_xml(attr["name"])
                attr_type = attr.get("type", "")
                writable = attr.get("writable", False)
                optional = attr.get("optional", False)

                lines.append(
                    f'    <attribute id="{attr_id}" name="{attr_name}" type="{attr_type}">'
                )
                if writable:
                    lines.append('      <access read="true" write="true" writePrivilege="manage"/>')
                else:
                    lines.append('      <access read="true" write="false"/>')
                if optional:
                    lines.append("      <optionalConform/>")
                else:
                    lines.append("      <mandatoryConform/>")
                lines.append("    </attribute>")
            lines.append("  </attributes>")

        if commands:
            lines.append("  <commands>")
            for cmd in commands:
                cmd_id = cmd["id"]
                cmd_name = _escape_xml(cmd["name"])
                direction = cmd.get("direction", "commandToServer")
                lines.append(
                    f'    <command id="{cmd_id}" name="{cmd_name}" direction="{direction}">'
                )
                lines.append('      <access invokePrivilege="manage"/>')
                lines.append("    </command>")
            lines.append("  </commands>")

        if events:
            lines.append("  <events>")
            for evt in events:
                evt_id = evt["id"]
                evt_name = _escape_xml(evt["name"])
                priority = evt.get("priority", "info")
                lines.append(
                    f'    <event id="{evt_id}" name="{evt_name}" priority="{priority}"/>'
                )
            lines.append("  </events>")

        lines.append("</cluster>")
        clusters_xml.append("\n".join(lines))

    # Process each clusterExtension
    for ext in root:
        if _strip_ns(ext.tag) != "clusterExtension":
            continue

        code_str = ext.get("code", "").strip()
        if not code_str:
            continue

        cluster_id_int = int(code_str, 16) if code_str.startswith("0x") else int(code_str)
        cluster_id_hex = f"0x{cluster_id_int:04X}"

        # Resolve cluster name
        cluster_name = cluster_name_lookup.get(cluster_id_int, f"Cluster_{cluster_id_hex}")
        if not cluster_name.endswith(" Cluster") and "Cluster" not in cluster_name:
            cluster_name = f"{cluster_name} Cluster"

        # Parse attributes, commands, events from the extension
        attributes = _parse_zap_attributes(ext)
        commands = _parse_zap_commands(ext)
        events = _parse_zap_events(ext)

        # Build DM XML string
        lines: List[str] = []
        lines.append(
            f'<cluster xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            f' name="{_escape_xml(cluster_name)}" id="{cluster_id_hex}" revision="1">'
        )

        # Attributes
        if attributes:
            lines.append("  <attributes>")
            for attr in attributes:
                attr_id = attr["id"]
                attr_name = _escape_xml(attr["name"])
                attr_type = attr.get("type", "")
                writable = attr.get("writable", False)
                optional = attr.get("optional", False)

                lines.append(
                    f'    <attribute id="{attr_id}" name="{attr_name}" type="{attr_type}">'
                )
                if writable:
                    lines.append('      <access read="true" write="true" writePrivilege="manage"/>')
                else:
                    lines.append('      <access read="true" write="false"/>')
                if optional:
                    lines.append("      <optionalConform/>")
                else:
                    lines.append("      <mandatoryConform/>")
                lines.append("    </attribute>")
            lines.append("  </attributes>")

        # Commands
        if commands:
            lines.append("  <commands>")
            for cmd in commands:
                cmd_id = cmd["id"]
                cmd_name = _escape_xml(cmd["name"])
                direction = cmd.get("direction", "commandToServer")
                lines.append(
                    f'    <command id="{cmd_id}" name="{cmd_name}" direction="{direction}">'
                )
                lines.append('      <access invokePrivilege="manage"/>')
                lines.append("    </command>")
            lines.append("  </commands>")

        # Events
        if events:
            lines.append("  <events>")
            for evt in events:
                evt_id = evt["id"]
                evt_name = _escape_xml(evt["name"])
                priority = evt.get("priority", "info")
                lines.append(
                    f'    <event id="{evt_id}" name="{evt_name}" priority="{priority}"/>'
                )
            lines.append("  </events>")

        lines.append("</cluster>")
        clusters_xml.append("\n".join(lines))

    # If no cluster/clusterExtension found but we have type definitions (enums/bitmaps/structs),
    # produce a minimal cluster DM XML so these types are preserved in the output.
    if not clusters_xml and (enums_by_cluster or bitmaps_by_cluster or structs_by_cluster):
        all_codes = set(enums_by_cluster.keys()) | set(bitmaps_by_cluster.keys()) | set(structs_by_cluster.keys())
        for code_str in sorted(all_codes):
            cluster_id_int = int(code_str, 16) if code_str.startswith("0x") else int(code_str)
            cluster_id_hex = f"0x{cluster_id_int:04X}"
            cluster_name = cluster_name_lookup.get(cluster_id_int, f"Cluster_{cluster_id_hex}")

            lines: List[str] = []
            lines.append(
                f'<cluster xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
                f' name="{_escape_xml(cluster_name)}" id="{cluster_id_hex}" revision="1">'
            )
            lines.append("  <!-- Type definitions only (enums/bitmaps/structs) -->")

            # Emit enums as attributes with their type name so the KG knows these types exist
            enums = enums_by_cluster.get(code_str, [])
            bitmaps = bitmaps_by_cluster.get(code_str, [])
            structs = structs_by_cluster.get(code_str, [])

            if enums or bitmaps or structs:
                lines.append("  <dataTypes>")
                for enum_el in enums:
                    ename = enum_el.get("name", "")
                    etype = enum_el.get("type", "enum8")
                    items = []
                    for item in enum_el:
                        if _strip_ns(item.tag) == "item":
                            items.append(f'{item.get("name", "")}={item.get("value", "")}')
                    lines.append(f'    <enum name="{_escape_xml(ename)}" type="{etype}">')
                    for item_str in items:
                        lines.append(f"      <item>{_escape_xml(item_str)}</item>")
                    lines.append("    </enum>")
                for bm_el in bitmaps:
                    bname = bm_el.get("name", "")
                    btype = bm_el.get("type", "bitmap8")
                    fields = []
                    for field in bm_el:
                        if _strip_ns(field.tag) == "field":
                            fields.append(f'{field.get("name", "")}={field.get("mask", "")}')
                    lines.append(f'    <bitmap name="{_escape_xml(bname)}" type="{btype}">')
                    for f_str in fields:
                        lines.append(f"      <field>{_escape_xml(f_str)}</field>")
                    lines.append("    </bitmap>")
                for st_el in structs:
                    sname = st_el.get("name", "")
                    lines.append(f'    <struct name="{_escape_xml(sname)}"/>')
                lines.append("  </dataTypes>")

            lines.append("</cluster>")
            clusters_xml.append("\n".join(lines))

    if not clusters_xml:
        logger.warning("[zap_xml_adapter] No cluster or clusterExtension elements found in %s", zap_xml_path)
        return ""

    return "\n\n".join(clusters_xml)


def is_zap_format(xml_path: Path) -> bool:
    """Check if an XML file uses ZAP format (has <configurator> root element).

    Returns False on parse errors or non-ZAP format files.
    """
    try:
        for event, elem in ET.iterparse(str(xml_path), events=("start",)):
            root_tag = _strip_ns(elem.tag)
            return root_tag == "configurator"
    except (ET.ParseError, OSError):
        return False
    return False


def is_device_type_xml(xml_path: Path) -> bool:
    """Check if a ZAP XML file is a device type definition (not a cluster).

    Device type XMLs have <deviceType> children under <configurator>.
    Returns False on parse errors or non-device-type files.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        if _strip_ns(root.tag) != "configurator":
            return False
        for child in root:
            if _strip_ns(child.tag) == "deviceType":
                return True
        return False
    except (ET.ParseError, OSError):
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_zap_attributes(ext_el: ET.Element) -> List[Dict[str, object]]:
    """Parse <attribute> elements from a clusterExtension."""
    attrs: List[Dict[str, object]] = []
    for child in ext_el:
        if _strip_ns(child.tag) != "attribute":
            continue

        code_str = child.get("code", "").strip()
        if not code_str:
            continue
        attr_id = code_str if code_str.startswith("0x") else f"0x{int(code_str):04X}"

        # Attribute name: "name" XML attr, text content, or <description> child
        name = child.get("name", "").strip()
        if not name:
            desc_el = child.find("description")
            if desc_el is not None and desc_el.text:
                name = desc_el.text.strip()
        if not name and child.text:
            name = child.text.strip()
        if not name:
            name = f"Attr_{attr_id}"

        attr_type = _map_zap_type(child.get("type", ""))
        writable = child.get("writable", "").lower() == "true"
        optional = child.get("optional", "").lower() == "true"

        attrs.append({
            "id": attr_id,
            "name": name,
            "type": attr_type,
            "writable": writable,
            "optional": optional,
        })
    return attrs


def _parse_zap_commands(ext_el: ET.Element) -> List[Dict[str, str]]:
    """Parse <command> elements from a clusterExtension."""
    cmds: List[Dict[str, str]] = []
    for child in ext_el:
        if _strip_ns(child.tag) != "command":
            continue

        code_str = child.get("code", "").strip()
        if not code_str:
            continue
        cmd_id = code_str if code_str.startswith("0x") else f"0x{int(code_str):04X}"

        name = child.get("name", "").strip()
        if not name:
            name = f"Cmd_{cmd_id}"

        source = child.get("source", "client").lower()
        direction = "commandToServer" if source == "client" else "commandToClient"

        cmds.append({
            "id": cmd_id,
            "name": name,
            "direction": direction,
        })
    return cmds


def _parse_zap_events(ext_el: ET.Element) -> List[Dict[str, str]]:
    """Parse <event> elements from a clusterExtension."""
    events: List[Dict[str, str]] = []
    for child in ext_el:
        if _strip_ns(child.tag) != "event":
            continue

        code_str = child.get("code", "").strip()
        if not code_str:
            continue
        evt_id = code_str if code_str.startswith("0x") else f"0x{int(code_str):04X}"

        name = child.get("name", "").strip()
        if not name:
            name = f"Event_{evt_id}"

        priority = child.get("priority", "info").strip()

        events.append({
            "id": evt_id,
            "name": name,
            "priority": priority,
        })
    return events


def _parse_zap_standalone_cluster(
    cluster_el: ET.Element,
) -> tuple:
    """Parse a standalone <cluster> element from ZAP format.

    Returns:
        (cluster_name, cluster_id_hex, pics_code, attributes, commands, events)
    """
    cluster_name = ""
    cluster_id_hex = ""
    pics_code = ""

    # Get name from <name> child element
    name_el = cluster_el.find("name")
    if name_el is None:
        # Try with namespace stripping
        for child in cluster_el:
            if _strip_ns(child.tag) == "name" and child.text:
                cluster_name = child.text.strip()
                break
    elif name_el.text:
        cluster_name = name_el.text.strip()

    # Get code from <code> child element
    code_el = cluster_el.find("code")
    if code_el is None:
        for child in cluster_el:
            if _strip_ns(child.tag) == "code" and child.text:
                code_str = child.text.strip()
                try:
                    cid = int(code_str, 16) if code_str.startswith("0x") else int(code_str)
                    cluster_id_hex = f"0x{cid:04X}"
                except ValueError:
                    pass
                break
    elif code_el.text:
        code_str = code_el.text.strip()
        try:
            cid = int(code_str, 16) if code_str.startswith("0x") else int(code_str)
            cluster_id_hex = f"0x{cid:04X}"
        except ValueError:
            pass

    # Get PICS code from <define> element (e.g., VENDOR_MY_CLUSTER → VMC)
    for child in cluster_el:
        if _strip_ns(child.tag) == "define" and child.text:
            pics_code = _derive_pics_code(child.text.strip(), cluster_name)
            break

    if not cluster_name or not cluster_id_hex:
        return ("", "", "", [], [], [])

    # Parse attributes, commands, events (same format as clusterExtension)
    attributes = _parse_zap_attributes(cluster_el)
    commands = _parse_zap_commands(cluster_el)
    events = _parse_zap_events(cluster_el)

    return (cluster_name, cluster_id_hex, pics_code, attributes, commands, events)


def _get_child_cluster_code(el: ET.Element) -> str:
    """Get the cluster code from a <cluster code="..."/> child element."""
    for child in el:
        if _strip_ns(child.tag) == "cluster":
            return child.get("code", "").strip()
    return ""


def _derive_pics_code(define_str: str, cluster_name: str) -> str:
    """Derive a PICS code from a <define> string or cluster name.

    Examples:
        VENDOR_CLUSTER_ONE → VCO
        VENDOR_CLUSTER_TWO → VCT
        MY_CUSTOM_CLUSTER → MCC
    """
    s = define_str.upper()
    for suffix in ("_CLUSTER", "_SERVER", "_CLIENT", "_EXTENSION"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break

    parts = [p for p in s.split("_") if p]
    if len(parts) >= 2:
        # Keep first letter of each word
        pics = "".join(p[0] for p in parts if p)
        if len(pics) >= 2:
            return pics

    # Fallback: derive from cluster name
    words = [w for w in cluster_name.split() if w and w.lower() != "cluster"]
    if words:
        pics = "".join(w[0].upper() for w in words)
        if len(pics) >= 2:
            return pics

    return ""


def _map_zap_type(zap_type: str) -> str:
    """Map ZAP type names to DM XML type names."""
    _TYPE_MAP = {
        "boolean": "bool",
        "int8u": "uint8",
        "int8s": "int8",
        "int16u": "uint16",
        "int16s": "int16",
        "int32u": "uint32",
        "int32s": "int32",
        "int64u": "uint64",
        "int64s": "int64",
        "enum8": "enum8",
        "enum16": "enum16",
        "bitmap8": "map8",
        "bitmap16": "map16",
        "bitmap32": "map32",
        "char_string": "string",
        "octet_string": "octstr",
        "epoch_s": "epoch-s",
        "elapsed_s": "elapsed-s",
        "temperature": "temperature",
    }
    return _TYPE_MAP.get(zap_type.lower(), zap_type)


def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _escape_xml(text: str) -> str:
    """Escape XML special characters in attribute values."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
