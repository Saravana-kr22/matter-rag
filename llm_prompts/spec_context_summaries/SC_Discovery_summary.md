# Matter Spec Summary: SC Discovery

**Source sections matched:** 28  
**Source chars sent to LLM:** 63,214  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 5,003  

---

## 1. Overview

Matter Service Advertising and Discovery is used in four contexts: Commissionable Node Discovery, Operational Discovery, Commissioner Discovery, and User Directed Commissioning. All four use IETF Standard DNS-Based Service Discovery (DNS-SD) per RFC 6763. Matter requires no modifications to IETF Standard DNS-SD.

DNS-SD enables discovery of both the unicast IPv6 address and port of a service dynamically, freeing Matter from requiring a preallocated fixed port and enabling multiple Matter software instances on a single device. On Wi-Fi and Ethernet, DNS-SD uses Multicast DNS (RFC 6762) for zero-configuration operation. On Thread mesh networks, where excessive multicast is detrimental, DNS-SD uses Unicast DNS via the Thread Service Registry on the Thread Border Router, leveraging Service Registration Protocol (SRP) and an Advertising Proxy.

Matter operates over IPv6 at minimum. IPv6 address discovery (AAAA records) is mandatory; IPv4 (A records) is optional. Optimizations to reduce multicast traffic apply, especially beneficial on Wi-Fi networks.

---

## 2. Protocol Flow & State Machine

### General Discovery Mechanics

Discovering a service involves: (1) enumeration of available instances ("browsing"), (2) lookup of port, host name, and additional info via SRV and TXT records ("resolving"), and (3) lookup of IPv6 addresses via AAAA records. At the protocol level, a single PTR query typically triggers the DNS Additional Record mechanism to return SRV, TXT, and address records together in one round trip, avoiding redundant queries.

### Commissionable Node Discovery

Applies to Commissionees already on the customer's IP network (Ethernet-connected or Wi-Fi). The Commissionee advertises service type `_matterc._udp`. The instance name is a dynamic pseudo-randomly selected 64-bit value expressed as a 16-character uppercase hex ASCII string (e.g., `DD200C20D25AE5F7`). A new instance name is selected on boot and whenever the node enters Commissioning Mode.

If a node receives `OpenCommissioningWindow` or `OpenBasicCommissioningWindow` on one of multiple connected IP networks, it MAY restrict advertisement to that network only; otherwise it advertises on all connected IP networks. When not in Commissioning Mode, a node MAY continue advertising under Extended Discovery (DNS-SD only, not BLE), but must provide a user-accessible way to disable or timeout this.

**Commissioning Mode states** (TXT key `CM`):
- `CM=0` (or absent): not in Commissioning Mode
- `CM=1`: in Commissioning Mode, Passcode provided by Commissionee (factory-new or `OpenBasicCommissioningWindow`)
- `CM=2`: in Commissioning Mode, dynamically generated Passcode from `OpenCommissioningWindow`
- `CM=3`: in Joint Fabric Commissioning Mode, dynamically generated Passcode from `OpenJointCommissioningWindow`

The `_CM` subtype is published only while `CM` ∈ {1, 2, 3}. The `_L<disc>`, `_S<disc>`, `_V<vid>`, and `_T<devtype>` subtypes enable Commissioners to filter results. Commissioners ignore unrecognized TXT keys to preserve forward compatibility.

### Operational Discovery

Used by already-commissioned Nodes to discover peers at runtime. Service type `_matter._tcp`. The DNS-SD instance name is `<CompressedFabricIdentifier>-<NodeIdentifier>`, each encoded as a 16-character uppercase hex string (e.g., `2906C908D115D362-8FC7772401CD0696`). Uniqueness is assumed from the combination of unique Fabric ID and unique Node ID within the Fabric.

Subtype `_I<CompressedFabricIdentifier>` enables Fabric-specific filtering. Subtype `_IC` (Incomplete Commissioning) is published by NFC-based Commissionees during commissioning steps 19–21 inclusive; withdrawn (via SRP update or TTL=0) outside that range.

**Incomplete Commissioning (IC) flow:** When a Commissionee publishes `_IC` and `IC=1`, the Commissioner identifies it and, if it knows how to commission the device, executes steps 20–21 to complete commissioning.

**Performance:** Nodes are advised to cache last-known IPv6 address and port for peers, use SRV queries (not PTR enumeration) when resolving a known operational service instance, not rely on DNS-SD for liveness determination, and limit enumeration queries to diagnostics and fabric membership discovery.

### Commissioner Discovery

Optional feature. Service type `_matterd._udp`. A Commissionee discovers available Commissioners, optionally presents a list to the user, and uses the User Directed Commissioning protocol ("door bell") to request commissioning from a selected Commissioner. Commissioner Discovery instance name follows the same generation rules as Commissionable Node Discovery (uniqueness, collision detection) but without the same triggers for when a new name must be selected.

### Thread-Specific Discovery

All Thread-connected Matter Nodes use SRP to register services with an available SRP server in the Thread Network Data. A Thread Border Router runs an Advertising Proxy (for Thread→Wi-Fi/Ethernet exposure) and a DNS-SD Discovery Proxy (for Wi-Fi/Ethernet→Thread exposure). Short-lived queries use unicast DNS over UDP; long-lived queries with change notification use DNS Push Notifications (RFC 8765) with DNS Stateful Operations (RFC 8490).

For long-lived requests where the requester's IPv6 address or port may change before the response is generated, the responder may need to perform discovery to find the requester's current address.

---

## 3. Normative Requirements

### 4.3 Discovery — General

> [4.25] "Matter software discovering other Matter instances **SHALL** process DNS AAAA (IPv6 address) records, but also **MAY** process DNS A (IPv4 address) records."

> [4.26] "Matter software advertising the availability of a service **SHOULD** indicate that announcements and answers for this service need include only IPv6 address records, not IPv4 address records."

> [4.27] "Matter software discovering other Matter instances **SHOULD NOT** expect any IPv4 addresses included in responses."

> [4.29] "Matter software using Multicast DNS to advertise the availability of a service **SHOULD** indicate that announcements and answers for this service need only be performed over IPv6."

> [4.30] "Matter application software using Multicast DNS to issue service discovery queries **SHOULD** indicate that these queries need only be performed over IPv6."

> [4.34] "All Thread-connected Matter Nodes **SHALL** implement Service Registration Protocol."

> [4.35] "Thread Border Routers advertise available SRP servers in the Thread Network Data. Thread devices **SHALL** register their services using an available SRP server."

> [4.37] "A Thread Border Router **SHALL** implement DNS-SD Discovery Proxy [RFC 8766] to enable clients on the Thread mesh (e.g., other Nodes) to discover services (e.g., Matter Nodes) advertised using Multicast DNS on an adjacent Ethernet or Wi-Fi link."

### 4.3.1 Commissionable Node Discovery — Instance Name

> [4.41] "the DNS-SD instance name **SHALL** be a dynamic, pseudo-randomly selected, 64-bit temporary unique identifier, expressed as a fixed-length sixteen-character hexadecimal string, encoded as ASCII (UTF-8) text using capital letters, e.g., DD200C20D25AE5F7."

> [4.41] "A new instance name **SHALL** be selected when the Node boots."

> [4.41] "A new instance name **SHALL** be selected whenever the Node enters Commissioning mode."

> [4.41] "A new instance name **MAY** be selected at other times, as long as the instance name does not change while the Node is in commissioning mode."

> [4.42] "A commissionable Node that is already connected to an IP-bearing network **SHALL** only make itself discoverable on the IP network and **SHALL** use the relevant DNS-SD service (_matterc._udp) described below."

> [4.43] "a commissionable Node that is connected to multiple IP-bearing networks **SHALL** make itself discoverable on all of its connected IP-bearing networks." [unless the OpenCommissioningWindow or OpenBasicCommissioningWindow was received on one network]

> [4.44] "The Matter Commissionable Node Discovery DNS-SD instance name **SHALL** be unique within the namespace of the local network."

> [4.45] "a new pseudo-randomly selected 64-bit temporary unique identifier **SHALL** be generated by the Matter Commissionee that is preparing for commissioning." [upon collision detection]

> [4.47] "For link-local Multicast DNS the service domain **SHALL** be local. For Unicast DNS such as used on Thread the service domain **SHALL** be as configured automatically by the Thread Border Router."

### 4.3.1.1 Host Name Construction

> [4.48] "The target host name **SHALL** be constructed using one of the available link-layer addresses, such as a 48-bit device MAC address (for Ethernet and Wi-Fi) or a 64-bit MAC Extended Address (for Thread) expressed as a fixed-length twelve-character (or sixteen-character) hexadecimal string, encoded as ASCII (UTF-8) text using capital letters, e.g., B75AFB458ECD."

> [4.48] "In the event that a device performs MAC address randomization for privacy, then the target host name **SHALL** use the privacy-preserving randomized version and the hostname **SHALL** be updated in the record every time the underlying link-layer address rotates."

### 4.3.1.2 Extended Discovery

> [4.50] "a Matter Commissionee **SHALL** provide a way for the customer to set a timeout on Extended Discovery, or otherwise disable Extended Discovery."

> [4.50] "The default behavior for Commissionable Node Discovery **SHOULD** default to limiting announcement as defined in Section 5.4.2.3, 'Announcement Duration' unless the Manufacturer wishes to enable longer periods for specific use cases."

### 4.3.1.3 Commissioning Subtypes

> [4.55] "A Commissionee that is not in commissioning mode (CM=0) **SHALL NOT** publish this subtype [_CM]."

### 4.3.1.4 TXT Records

> [4.59] "Nodes **SHALL** publish AAAA records for all available IPv6 addresses upon which they are willing to accept Matter commissioning messages."

> [4.61] "Commissioners **SHALL** silently ignore TXT record keys that they do not recognize."

### 4.3.1.5 TXT key for discriminator (D)

> [4.63] "The key D **SHALL** provide the full 12-bit discriminator for the Commissionable Node and **SHALL** be present in the DNS-SD TXT record."

> [4.64] "The discriminator value **SHALL** be encoded as a variable-length decimal number in ASCII text, with up to four digits, omitting any leading zeroes."

> [4.65] "Any key D with a value mismatching the aforementioned format **SHALL** be silently ignored."

### 4.3.1.6 TXT key for Vendor ID and Product ID (VP)

> [4.70] "The Vendor ID and Product ID **SHALL** both be expressed as variable-length decimal numbers, encoded as ASCII text, omitting any leading zeroes, and of maximum length of 5 characters each to fit their 16-bit numerical range."

> [4.71] "If the Product ID is present, it **SHALL** be separated from the Vendor ID using a '+' character."

> [4.72] "If the VP key is present without a Product ID, the value **SHALL** contain only the Vendor ID alone, with no '+' character."

> [4.73] "If the VP key is present, the value **SHALL** contain at least the Vendor ID."

> [4.74] "If the VP key is present, it **SHALL NOT** have a missing or empty value."

### 4.3.1.7 TXT key for commissioning mode (CM)

> [4.75] "The key CM (Commissioning Mode) **SHALL** indicate whether or not the publisher of the record is currently in Commissioning Mode and available for immediate commissioning."

> [4.76] "The absence of key CM **SHALL** imply a value of 0 (CM=0)."

> [4.76] "The key/value pair CM=0 **SHALL** indicate that the publisher is not currently in Commissioning Mode."

> [4.76] "The key/value pair CM=1 **SHALL** indicate that the publisher is currently in Commissioning Mode and requires use of a Passcode for commissioning provided by the Commissionee (e.g., embedded in Onboarding Material which is printed on device, on a label or displayed on screen), such as when the device is in a factory-new state or when the OpenBasicCommissioningWindow command has been used to enter commissioning mode."

> [4.76] "The key/value pair CM=2 **SHALL** indicate that the publisher is currently in Commissioning Mode and requires use of a dynamically generated Passcode for commissioning corresponding to the verifier that was passed to the device using the OpenCommissioningWindow command."

> [4.76] "The key/value pair CM=3 **SHALL** indicate that the publisher is currently in Joint Fabric Commissioning Mode and requires use of a dynamically generated Passcode for commissioning corresponding to the verifier that was passed to the device using the OpenJointCommissioningWindow command."

### 4.3.1.8 TXT key for device type (DT)

> [4.79] "If present, it **SHALL** be encoded as a variable-length decimal number in ASCII text, omitting any leading zeroes."

### 4.3.1.9 TXT key for device name (DN)

> [4.81] "If present, it **SHALL** be encoded as a valid UTF-8 string with a maximum length of 32 bytes."

> [4.82] "the source of this value **SHALL** be editable by the user with use clearly designated as being for on-network advertising."

> [4.83] "if a Commissionee supports this key/value pair, then the Commissionee **SHALL** provide a way for the customer to disable its inclusion."

> [4.84] "A Commissionee **SHOULD NOT** include this field unless doing so for specific use cases which call for it."

### 4.3.1.10 TXT key for rotating device identifier (RI)

> [4.87] "the value associated with the RI key **SHALL** contain the octets of the Rotating Device Identifier octet string encoded as the concatenation of each octet's value as a 2-digit uppercase hexadecimal number."

> [4.88] "The resulting ASCII string **SHALL NOT** be longer than 100 characters, which implies a Rotating Device Identifier of at most 50 octets."

### 4.3.1.11 TXT key for pairing hint (PH)

> [4.90] "it **SHALL** be encoded as a variable-length decimal number in ASCII text, omitting any leading zeroes."

> [4.96] "If the Commissionee has enabled Extended Discovery, then it **SHALL** include the key/value pair for PH in the DNS-SD TXT record when not in Commissioning Mode (CM=0)."

> [4.100] Bit 0 (Power Cycle): "When used with the target state of Commissioning Mode for a device that is in a factory reset state, this bit **SHALL** be set to 1 for devices using Standard Commissioning Flow, and set to 0 otherwise."

> [4.100] Bit 1 (Device Manufacturer URL/App): "When used with the target state of Commissioning Mode for a device that is in a factory reset state, this bit **SHALL** be set to 1 for devices requiring Custom Commissioning Flow before they can be available for Commissioning by any Commissioner."

> [4.100] Bit 4 (Custom Instruction): "The PI key/value pair **SHALL** describe a custom way to put the Device into the target state."

> [4.100] Bits 8, 10, 12, 15, 17, 19: "The exact value of N **SHALL** be made available via PI key."

> [4.100] Bit 20 (Power Cycle N times): "The format of the PI key's value **SHALL** be N,X,Y,Z where N,X,Y and Z are variable length decimal values (no leading zeros) and there are no spaces or other characters present in the value."

> [4.100] Bit 21 (Press Button for N seconds with indication): "The format of the PI key's value **SHALL** be N,Z or N,Z,A, where N and Z are variable length decimal values (no leading zeros), A is blank or a combination of CEC Key Codes separated by '&' or '|', and there are no spaces or other characters present in the value."

> [4.100] Bit 22 (Power Cycle Until Indication): "The format of the PI key's value **SHALL** be X,Y,Z,M,N where X, Y and Z are variable length decimal values (no leading zeros) and there are no spaces or other characters present in the value, while M is a single color name, and N, when present, is one of either ON or OFF."

> [4.101] "only basic primary and secondary colors that could unambiguously be decoded by a commissioner and understood by an end-user, but without worry of localization, **SHOULD** be used, e.g. white, red, green, blue, orange, yellow, purple, unless otherwise specified."

> [4.103] "only one such method can be specified which has a mandatory dependency on the PI key (PI Dependency= M) at a time."

> [4.105] "at least one bit in the above bitmap **SHALL** be set. That is, a PH value of 0 is undefined and illegal."

> [4.106] "the Commissioner **SHOULD** take its value into account when providing guidance to the user regarding steps required to put the Commissionee into Commissioning Mode."

### 4.3.1.12 TXT key for pairing instructions (PI)

> [4.108] "the value **SHALL** be encoded as a valid UTF-8 string with a maximum length of 128 bytes."

> [4.111] "the Commissionee **SHALL** be responsible for localization (translation to user's preferred language) as required using the Device's currently configured locale."

> [4.112] "It is RECOMMENDED to keep the length of PI field small and adhere to the guidance given in section 6.2 of [RFC 6763]."

> [4.113] "This key/value pair **SHALL** only be returned in the DNS-SD TXT record if the PH bitmap value has a bit set which has PI Dependency = M or has a bit set which has PI Dependency = O and the Commissionee wants to indicate related information as mentioned in Note 1 above."

> [4.113] "The PH key **SHALL NOT** have more than one bit set which has a mandatory dependency on the PI key (PI Dependency = M) to avoid ambiguity in PI key usage."

### 4.3.1.13 TXT key for Joint Fabric (JF)

> [4.115] "The JF key **SHALL** be present in the DNS-SD TXT record if and only if the Node is capable of being a Joint Fabric Administrator."

> [4.116] "The JF key **SHALL** be encoded as a variable-length decimal number in ASCII text, omitting any leading zeroes."

> [4.119] (Note 1): "bit 0 (Available) **SHALL** be unset for any of bits 1, 2 or 3 to be set. bit 0 (Available) **SHALL** be set, bits 1, 2 and 3 **SHALL** be unset as the default value prior to the Administrator Node being commissioned onto the Joint Fabric. Once an Administrator device is commissioned on the Joint Fabric, bit 0 (Available) **SHALL** be unset."

> [4.120] (Note 2): "bit 1 (Administrator) **SHALL** be set for bit 2 (Anchor) to be set. A device **SHALL** be a Joint Fabric Administrator to be a Joint Fabric Anchor Administrator. [...] at most one device **SHALL** have bit 3 (Datastore) set."

> [4.121] (Note 3): "bit 1 (Administrator), bit 2 (Anchor), and bit 3 (Datastore) **SHALL** all be set for the single device which is the Joint Fabric Datastore."

> [4.125] "The VP key **SHALL** be present in the DNS-SD TXT record if the JF key is present and **SHALL** provide the Vendor ID of the device."

### 4.3.2 Operational Discovery — Compressed Fabric Identifier

> [4.160] "a 64-bit compressed version of the full Fabric Reference **SHALL** be used. The computation of the Compressed Fabric Identifier **SHALL** be as follows: [CRYPTO_KDF using TargetOperationalRootPublicKey and TargetOperationalFabricID as inputs]"

### 4.3.2.4 Operational Service Domain and Host Name

> [4.167] "For link-local Multicast DNS the service domain **SHALL** be local. For Unicast DNS such as used on Thread the service domain **SHALL** be as configured automatically by the Thread Border Router."

> [4.168] "The target host name **SHALL** be constructed using one of the available link-layer addresses [...] the target host name **SHALL** use the privacy-preserving randomized version and the hostname **SHALL** be updated in the record every time the underlying link-layer address rotates."

### 4.3.2.5 Operational Discovery Subtypes (_IC)

> [4.169] "A Commissionee using NFC-based commissioning **SHALL** publish this subtype from the step 19 up to the step 21 (included), and **SHALL** withdraw it (using SRP update or DNS-SD with TTL=0) when leaving this step range."

> [4.169] "A node outside this step range of the commissioning flow **SHALL NOT** publish this subtype."

> [4.169] "A Commissionee not using NFC-based commissioning **SHALL NOT** use this subtype."

### 4.3.2.6 Operational Discovery Service Records

> [4.171] "Nodes **SHALL** publish AAAA records for all available IPv6 addresses upon which they are willing to accept operational messages."

> [4.174] "Nodes **SHALL** silently ignore TXT record keys that they do not recognize."

### 4.3.2.7 TXT key for Incomplete Commissioning (IC)

> [4.175] "A node publishing _IC subtype **SHALL** include IC=1 in TXT record of both the subtype and the base services."

> [4.176] "A Commissionee that is using NFC-based commissioning but is not waiting for the Commissioning Complete command **SHALL NOT** publish the _IC subtype."

> [4.178] "the Commissioner **SHALL** identify devices with the 'IC=1' subtype. If it knows how to commission this device, the Commissioner **SHALL** execute the steps from 20 to 21 to complete the commissioning of the device."

### 4.3.2.8 Operational Discovery Performance Recommendations

> [4.179] "Nodes **SHOULD** cache the last-known IPv6 address and port for each peer Node with which they interact from their SRV record resolved using DNS-SD."

> [4.179] "a Node **SHOULD** then perform a run-time discovery in parallel, to determine whether the desired Node has acquired a new IPv6 address and/or port [RFC 8305]." [when last-known address is stale or unknown]

> [4.179] "Nodes **SHOULD** respond to nonspecific service enumeration queries for the generic Matter Operational Discovery service type (_matter._tcp), but these queries **SHOULD NOT** be used in routine operation."

> [4.179] "Known Answer Suppression [RFC 6762] **SHOULD** be employed in such cases to further minimize the number of unnecessary responses to such a query."

> [4.179] "a Node **SHOULD** use an SRV query for the desired operational service instance rather than doing general enumeration of all nodes (e.g. PTR query) followed by filtering for the desired service instance."

> [4.179] "Nodes **SHOULD NOT** use DNS-SD as an operational liveness determination method."

### 4.3.3 Commissioner Discovery

> [4.191] "a Matter Commissioner **SHALL** provide a way for the customer to set a timeout on Commissioner Discovery, or otherwise disable Commissioner Discovery."

> [4.194] "The port advertised by a _matterd._udp service record **SHALL** be different than any port associated with other advertised _matterc._udp or _matter._tcp services, in order to ensure that the session-less messaging employed by the User Directed Commissioning protocol does not cause invalid message handling from fully operational Matter nodes at the same address."

---

## 4. Message Formats & Data Structures

### Service Types (DNS-SD)

| Discovery Type | Service Type |
|---|---|
| Commissionable Node Discovery | `_matterc._udp` |
| Operational Discovery | `_matter._tcp` |
| Commissioner Discovery | `_matterd._udp` |

### Commissionable Node Discovery Subtypes

| Subtype | Meaning |
|---|---|
| `_L<disc>` | Full 12-bit long discriminator (variable-length decimal, no leading zeros) |
| `_S<disc>` | Upper 4 bits of discriminator (variable-length decimal, no leading zeros) |
| `_V<vid>` | 16-bit Vendor ID (variable-length decimal, no leading zeros) |
| `_T<devtype>` | Device type identifier (variable-length decimal, no leading zeros) |
| `_CM` | Node is currently in Commissioning Mode (any CM value ∈ {1,2,3}) |

### Operational Discovery Subtypes

| Subtype | Meaning |
|---|---|
| `_I<CompressedFabricId>` | Fabric-specific filter; `<CompressedFabricId>` encoded as exactly 16 uppercase hex characters |
| `_IC` | Incomplete Commissioning; NFC-based commissioning steps 19–21 only |

### Commissionable Node Discovery TXT Record Keys

| Key | Mandatory | Encoding | Constraints |
|---|---|---|---|
| `D` | Yes | Variable-length decimal ASCII, up to 4 digits, no leading zeros | Full 12-bit discriminator |
| `VP` | No | `<VID>` or `<VID>+<PID>`; each decimal ASCII, max 5 chars, no leading zeros | If present: must contain at least VID; no empty value |
| `CM` | Yes (absence = 0) | Single decimal digit: 0, 1, 2, or 3 | Absence implies CM=0 |
| `DT` | No | Variable-length decimal ASCII, no leading zeros | Primary device type |
| `DN` | No | Valid UTF-8, max 32 bytes | Customer-disableable |
| `RI` | No | Uppercase hex pairs, max 100 ASCII chars | Rotating Device Identifier |
| `PH` | No (required if Extended Discovery) | Variable-length decimal ASCII, no leading zeros | Non-zero; bitmap of pairing methods |
| `PI` | Conditional on PH | Valid UTF-8, max 128 bytes | Only when PH has PI Dependency = M or relevant O bit |
| `JF` | Conditional | Variable-length decimal ASCII, no leading zeros | Present iff Node capable of Joint Fabric Administrator |

### Operational Discovery TXT Record Keys

| Key | Encoding | Meaning |
|---|---|---|
| `IC` | `1` or `0` | `IC=1`: waiting for Commissioning Complete; `IC=0`: not waiting |

### Instance Name Formats

**Commissionable Node Discovery:** 16-character uppercase hex string (64-bit pseudo-random temporary ID), e.g., `DD200C20D25AE5F7`.

**Operational Discovery:** `<CompressedFabricId>-<NodeId>`, each 16-character uppercase hex string, e.g., `2906C908D115D362-8FC7772401CD0696`.

**Commissioner Discovery:** Same format as Commissionable Node Discovery.

### Host Name Format

Constructed from link-layer address: 48-bit MAC → 12-character uppercase hex string; 64-bit MAC Extended Address (Thread) → 16-character uppercase hex string. Suffix with `.` (dot) as DNS label terminator, e.g., `B75AFB458ECD.`.

### Compressed Fabric Identifier

Computed via `CRYPTO_KDF` over `TargetOperationalRootPublicKey` (raw uncompressed EC public key of root cert, without leading format byte) and `TargetOperationalFabricID` (64-bit unsigned integer from Operational Certificate subject, big-endian octet string). Result: 64-bit value encoded as 16 uppercase hex characters for use in `_I<CompFabId>` subtype and in operational instance name.

### Pairing Hint (PH) Bitmap — Selected Entries with PI Dependency

| Bit | Name | PI Dependency |
|---|---|---|
| 0 | Power Cycle | no |
| 1 | Device Manufacturer URL/App | no |
| 2 | Administrator | no |
| 3 | Settings menu on the Node | no |
| 4 | Custom Instruction | M |
| 5 | Device Manual | no |
| 6 | Press Reset Button | no |
| 7 | Press Reset Button with application of power | no |
| 8 | Press Reset Button for N seconds | M |
| 9 | Press Reset Button until light blinks | O |
| 10 | Press Reset Button for N seconds with application of power | M |
| 11 | Press Reset Button until light blinks with application of power | O |
| 12 | Press Reset Button N times | M |
| 13 | Press Setup Button | no |
| 14 | Press Setup Button with application of power | no |
| 15 | Press Setup Button for N seconds | M |
| 16 | Press Setup Button until light blinks | O |
| 17 | Press Setup Button for N seconds with application of power | M |
| 18 | Press Setup Button until light blinks with application of power | O |
| 19 | Press Setup Button N times | M |
| 20 | Power Cycle N times | M (PI format: `N,X,Y,Z`) |
| 21 | Press Button for N seconds with indication | M (PI format: `N,Z` or `N,Z,A`) |
| 22 | Power Cycle Until Indication | M (PI format: `X,Y,Z,M,N`) |

At most one bit with PI Dependency = M may be set at a time.

### Joint Fabric (JF) Bitmap

| Bit | Capability |
|---|---|
| 0 | Available — capable of being Joint Fabric Administrator (unset once commissioned) |
| 1 | Administrator — currently acting as Joint Fabric Administrator |
| 2 | Anchor — currently acting as Joint Fabric Anchor Administrator |
| 3 | Datastore — currently acting as Joint Fabric Datastore |

---

## 5. Security Considerations

Multicast DNS, like IPv6 Neighbor Discovery, has no mechanism to distinguish genuine replies from malicious or fraudulent ones. The spec text addresses this as follows:

> [4.156] "Because of this, Multicast DNS, like IPv6 Neighbor Discovery, does not have any easy way to distinguish genuine replies from malicious or fraudulent replies. Consequently, application-layer end-to-end security is essential. Should a malicious device on the same local link give deliberately malicious or fraudulent replies, the misbehavior will be detected when the device is unable to establish a cryptographically secure application-layer communication channel. This reduces the threat to a Denial-of-Service attack, which can be remedied by physically removing the offending device."

Privacy protections required by spec text:

- A Commissionee supporting the `DN` (device name) key SHALL provide a way for the customer to disable its inclusion ([4.83]).
- A Commissionee SHALL provide a way for the customer to set a timeout on Extended Discovery, or otherwise disable it ([4.50]).
- A Matter Commissioner SHALL provide a way for the customer to set a timeout on Commissioner Discovery, or otherwise disable it ([4.191]).
- A vendor MAY choose not to include the `VP` key at all, for privacy reasons ([4.68]).
- When a device performs MAC address randomization for privacy, the host name SHALL use the randomized version and SHALL be updated on each rotation ([4.48], [4.168]).

The Compressed Fabric Identifier in operational instance names is derived from the root certificate public key and Fabric ID, providing cryptographic scoping of the operational namespace without exposing full fabric credentials in DNS-SD advertisements.

---

## 6. Error Handling & Timing

### Instance Name Collision Detection and Recovery

> [4.45] "In the rare event of a collision in the selection of the 64-bit temporary unique identifier, the existing DNS-SD name conflict detection mechanism will detect this collision, and a new pseudo-randomly selected 64-bit temporary unique identifier **SHALL** be generated by the Matter Commissionee that is preparing for commissioning."

Name conflict detection follows Section 9 ("Conflict Resolution") of RFC 6762 (Multicast DNS) and Section 2.4.3.1 ("Validation of Adds") of the SRP specification.

### Stale Record Handling

> [4.179] "Since proxied DNS-SD service discovery MAY be in use within a given network, and service record caching is expected of DNS-SD clients, Nodes **SHOULD NOT** use DNS-SD as an operational liveness determination method. This is because there may be stale records not yet expired after a Node becomes unreachable which may still be available."

### Cache Invalidation for Long-lived Requests

> [4.36] "When Matter Nodes issue long-lived requests to other Matter Nodes, by the time the response is generated the requester may have changed IPv6 address or port, so the responder may have to discover the current IPv6 address and port of the initiator in order to send the response."

### Retransmission Trigger for Cached Address Lookup

> [4.179] "When the IPv6 address and port for a peer Node is not known, or an attempt to communicate with a peer Node at its last-known IPv6 address and port does not appear to be succeeding within the expected network round-trip time (i.e., the retransmission timeout value for the first message packet) a Node **SHOULD** then perform a run-time discovery in parallel, to determine whether the desired Node has acquired a new IPv6 address and/or port [RFC 8305]."

### Incomplete Commissioning Withdrawal

> [4.169] "A Commissionee using NFC-based commissioning [...] **SHALL** withdraw it [_IC subtype] (using SRP update or DNS-SD with TTL=0) when leaving this step range [steps 19–21]."

### Unrecognized TXT Keys

> [4.61] "Commissioners **SHALL** silently ignore TXT record keys that they do not recognize."

> [4.174] "Nodes **SHALL** silently ignore TXT record keys that they do not recognize."

> [4.65] "Any key D with a value mismatching the aforementioned format **SHALL** be silently ignored."

### Unrecognized PH Bits

> [4.98] "If the Commissioner does not recognize this value [...] then the Commissioner **MAY** utilize the bits that it does understand and **MAY** utilize additional data sets available for assisting the user."

### IC Key Undefined Values

> [4.177] "The absence of the key IC or any undefined value does not provide information on the commissioning step."