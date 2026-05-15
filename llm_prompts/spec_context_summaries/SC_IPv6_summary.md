# Matter Spec Summary: SC IPv6

**Source sections matched:** 3  
**Source chars sent to LLM:** 3,923  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 756  

---

## 1. Overview

This section describes IPv6 network configuration requirements to enable IPv6 reachability between Nodes in a Matter network. A Matter network may be composed of one or more IPv6 networks. Two primary topologies are addressed:

- **Single network configuration**: All Matter Nodes are attached to the same IPv6 link (e.g., a single bridged Wi-Fi/Ethernet network within the same broadcast domain). Link-local IPv6 addressing is sufficient; no additional IPv6 network infrastructure is required.
- **Multiple network configuration**: A Matter network is composed of (typically one) infrastructure network and one or more stub networks. Stub networks do not serve as transit networks. Typically, the infrastructure network is a bridged Wi-Fi/Ethernet network and Thread networks are stub networks. A stub router connects a stub network to an infrastructure network and provides IPv6 reachability between the two networks.

---

## 2. Protocol Flow & State Machine

The section describes configuration-level behavior rather than a message handshake sequence. The key operational flows are:

**Stub Router Flow (multiple network configuration):**
1. Stub router advertises reachability to all routable prefixes on the adjacent network.
2. For a Thread-connected stub router: it sends Route Information Options (RFC 4191) in Router Advertisements (RFC 4861) to the adjacent infrastructure network, advertising all Thread network routable prefixes.
3. That same stub router also advertises all infrastructure network routable prefixes into the Thread Network Data.

**Matter Node Address Configuration Flow:**
1. Node configures a link-local IPv6 address.
2. Node receives on-link prefix advertisements (via ICMPv6 RA on Wi-Fi/Ethernet, or Thread Network Data on Thread).
3. If the received prefix allows autonomous configuration and the Node has fewer than three routable IPv6 addresses configured, the Node autonomously configures an IPv6 address from that prefix.
4. Node configures routes to adjacent networks (via Route Information Options on Wi-Fi/Ethernet, or Thread Network Data on Thread).

---

## 3. Normative Requirements

### 4.2.1. Stub Router Behavior

- "A stub router SHALL implement [draft-lemon-stub-networks]."
- "A routable IPv6 address SHALL have global scope (e.g. GUA or ULA) [RFC 4007] and SHALL be constructed out of a prefix advertised as on-link."
- "If there is no routable prefix on a given network, the stub router SHALL provide its own routable prefix."
- "Stub routers SHALL advertise reachability to all routable prefixes on the adjacent network."
- "A stub router connecting a Thread network SHALL advertise reachability to all of the Thread network's routable prefixes to the adjacent infrastructure network using Route Information Options [RFC 4191] contained in Router Advertisements [RFC 4861]."
- "That same stub router SHALL also advertise reachability to all of the infrastructure network's routable prefixes to the adjacent Thread network in the Thread Network Data [Thread specification]."

### 4.2.2. Matter Node Behavior

- "Matter Nodes SHALL configure a link-local IPv6 address."
- "Nodes SHALL support configuration of at least three routable IPv6 addresses (in addition to the link-local and, in the case of Thread, mesh-local addresses)."
- "If a Node receives an on-link prefix that allows autonomous configuration on a given interface and the Node has fewer than three routable IPv6 addresses configured, the Node SHALL autonomously configure an IPv6 address out of that prefix."
- "Matter Nodes SHALL also configure routes to adjacent networks."
- "On Wi-Fi / Ethernet networks, Nodes SHALL process Route Information Options [RFC 4191] and configure routes to IPv6 prefixes assigned to stub networks via stub routers."
- "Wi-Fi / Ethernet interfaces SHALL support maintaining at least 16 different routes configured using Route Information Options."
- "On Thread networks, Nodes SHALL route according to routing information provided in the Thread Network Data [Thread specification]."
- "Thread devices SHALL support as many routes as can be encoded in the Thread Network Data."
- "Matter Nodes SHALL support a number of IPv6 neighbor cache entries at least as large as the number of supported CASE sessions plus the number of supported routes."

---

## 4. Message Formats & Data Structures

The spec text references the following wire-level constructs but does not provide detailed format definitions in the provided sections:

- **Route Information Options** [RFC 4191] — carried in Router Advertisements on Wi-Fi/Ethernet interfaces.
- **Router Advertisements** [RFC 4861] — ICMPv6 RA messages used on Wi-Fi/Ethernet to advertise on-link prefixes.
- **Thread Network Data** [Thread specification] — used on Thread interfaces to carry on-link prefixes and routing information.

No TLV encodings, opcodes, or numeric field definitions are provided in the supplied spec text.

---

## 5. Security Considerations

(Not covered in provided spec sections.)

---

## 6. Error Handling & Timing

(Not covered in provided spec sections.)