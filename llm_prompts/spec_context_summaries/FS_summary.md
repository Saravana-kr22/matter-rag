# Matter Spec Summary: FS Protocol

**Source sections matched:** 25  
**Source chars sent to LLM:** 20,212  
**Generated:** 2026-04-28 17:26:22  
**Summary words:** 2,921  

---

## 1. Overview

Fabric Synchronization is a feature that enables commissioning of devices from one fabric to another without requiring user intervention for every device. It defines mechanisms that can be used by multiple ecosystems/controllers to communicate with one another to simplify the experience for users.

**Key roles and concepts:**

- **Fabric Synchronizing Administrator**: A component within an ecosystem which supports Fabric Synchronization. This includes the Aggregator and Fabric Administrator node(s).
- **Synchronized Device**: A device which is being synchronized between ecosystems.
- **Commissionee**: The Fabric Synchronizing Administrator that advertises its presence over DNS-SD.
- **Commissioner**: The Fabric Synchronizing Administrator that performs service discovery.

Fabric Synchronization is present when an Aggregator satisfies the `FabricSynchronization` condition or one or more Bridged Nodes satisfy the `FabricSynchronizedNode` condition.

---

## 2. Protocol Details

### Composition

An Aggregator supporting Fabric Synchronization must be composed of:

1. **Matter Fabric Administrator Node**: The device providing the Aggregator must be able to commission nodes on its fabric.
2. **Aggregator Node**: Must have specific endpoints in its Descriptor cluster `PartsList`.
3. **Bridged Node Endpoint(s)**: Each endpoint conforming to the `FabricSynchronizedNode` condition represents a Synchronized Device.
4. **Commissioner Control Cluster**: Enables another device supporting Fabric Synchronization to set up a bidirectional synchronization relationship without QR code or Manual Pairing Code entry (as used in ECM flow).

### Fabric Synchronized Relationships

Each client ecosystem commissions the Aggregator using the client ecosystem's own fabric. Client ecosystems each have their own dedicated, isolated fabric separate from the fabric used by the Aggregator to interact with Matter devices. One ecosystem can directly access all devices and the Aggregator of another ecosystem from its own fabric without requiring the other ecosystem to have access granted on the first ecosystem's fabric.

### Setup Flow

The setup flow covers the following phases:

1. **Mutual Authentication (optional precondition)**: Ecosystems MAY wait until complete bi-directional commissioning has been completed before exposing any Bridged Nodes, so both ecosystems have received device attestation from the other.
2. **User Action / Initiating Discovery**: The user enables administrator-assisted commissioning through an appropriate interface. The Commissionee provides a Manual Pairing Code (and optionally a QR code).
3. **Discovery**: The Commissionee advertises over DNS-SD; the Commissioner MAY discover and notify the user.
4. **Forward Commissioning**: The user initiates commissioning of the Commissionee Fabric Synchronizing Administrator. The Commissioner commissions the Commissionee using the Concurrent connection commissioning flow.
5. **Reverse Commissioning** (recommended): After forward commissioning, the Commissioner SHOULD initiate Reverse Commissioning using the Commissioner Control Cluster. A Commissionee Fabric Synchronizing Administrator MAY choose to require reverse commissioning before enabling Fabric Synchronization.
6. **Fabric Synchronization Configuration**: The user is asked for consent to synchronize devices between fabrics.
7. **Fabric Synchronization**: The Fabric Synchronizing Administrator commissions synchronized devices as configured by the user.

### Commissioning of Synchronized Devices

Commissioning is performed by:
1. Discovering available Synchronized Devices by identifying endpoints specified in the `PartsList` of the Aggregator within the Fabric Synchronizing Administrator.
2. Identifying if the Synchronized Device supports the `BridgedICDSupport` feature in the Bridged Device Basic Information Cluster.
3. Initiating commissioning by sending an `OpenCommissioningWindow` command to the Administrator Commissioning Cluster exposed on the endpoint with the Bridged Node device type.
4. Completing commissioning using the Enhanced Commissioning Method.

### Device Deduplication

To prevent commissioning a device onto a fabric it is already on, Fabric Synchronizing Administrators examine the `UniqueID`, `VendorID`, and `ProductID` in the Bridged Device Basic Information Cluster. A UniqueID caching and tie-breaker policy is required to avoid looping. The cache SHOULD have space for at least 5 entries per NodeID.

### Changes to Synchronized Devices

Devices can be added to or removed from the set of Synchronized Devices through Administrator-specific means (e.g., via a Manufacturer-provided app). Dynamic Endpoint allocation is used for endpoint management.

### Device Names and Locations

Fabric Synchronizing Administrators SHOULD expose device names and grouping/location information via the Ecosystem Information Cluster. Exposure via the Basic Information Cluster is optional, but if done, the same data MUST also appear in the Ecosystem Information Cluster.

---

## 3. Normative Requirements

### 12.6.3 — Fabric Synchronization Composition

**[12.52]**
> "An Aggregator supporting Fabric Synchronization SHALL be composed of the following components."

**[12.53]**
> "The device providing the Aggregator SHALL be able to commission nodes on its fabric."

**[12.54]**
> "When Fabric Synchronization is supported, the Aggregator with FabricSynchronization condition (see Device Library, Aggregator) SHALL be met on an endpoint with the following endpoints in the Descriptor cluster PartsList."

**[12.56]**
> "Fabric Synchronization SHALL be supported when the SupportedDeviceCategories attribute in the Commissioner Control Cluster has the FabricSynchronization bit set."

**[12.58] — Bridged Node requirements:**

> "The Bridged Node SHALL include the Ecosystem Information Cluster. (Enables discovery and directory synchronization.)"

> "The Bridged Node SHALL include the Administrator Commissioning Cluster when the user consents to share a device. (Enables synchronization of devices between fabrics.)"

> "The Bridged Node SHOULD support the BridgedICDSupport feature in the Bridged Device Basic Information Cluster if the Synchronized Device is an Intermittently Connected Device (ICD). (Enables communication with ICDs.)"

### 12.6.4 — Preventing Device Duplication

**[12.59]**
> "A Fabric Synchronizing Administrator SHOULD NOT commission devices onto the same fabric that they are already on. To avoid this, the Fabric Synchronizing Administrator SHOULD examine the UniqueID of a potential Synchronized Device's Bridged Device Basic Information Cluster and SHOULD examine the VendorID and ProductID fields if they are present in the Bridged Device Basic Information Cluster. If all of the provided values for UniqueID, ProductID, and VendorID match a known device that is already on the Fabric Synchronizing Administrator's fabric, then it SHOULD NOT attempt to commission the device."

**[12.61]**
> "When a Fabric Synchronizing Administrator commissions a Synchronized Device, it SHALL persist and maintain an association with the UniqueID in the Bridged Device Basic Information Cluster exposed by another Fabric Synchronizing Administrator."

**[12.62]**
> "If a Fabric Synchronizing Administrator exposes a Synchronized Device which does not have a UniqueID in its Basic Information Cluster, then the Fabric Synchronizing Administrator SHALL generate and persist a new UniqueID to be used in the Bridged Device Basic Information Cluster."

**[12.63] — Unifying Generated UniqueID:**

> "When a Fabric Synchronizing Administrator establishes a PASE session to a Synchronized Device for the purposes of commissioning, the Fabric Synchronizing Administrator SHOULD verify that the device is not already present on the intended fabric as follows:"

> "The Fabric Synchronizing Administrator MAY check if the UniqueID is present in the Basic Information Cluster. If the UniqueID is present, the Fabric Synchronizing Administrator can skip the below check."

> "If the UniqueID is not present or not checked, the Fabric Synchronizing Administrator SHOULD check if the intended fabric is already present in the Fabric Table."

> "If it is present, the Fabric Synchronizing Administrator SHOULD NOT complete commissioning and SHOULD avoid attempting to commission the device (or establish PASE sessions) in the future by persisting the UniqueID exposed by the other Fabric Synchronizing Administrator's Bridged Device Basic Information Cluster. (If the Fabric Synchronizing Administrator exposes the device through a Bridged Node endpoint, then the Fabric Synchronizing Administrator SHOULD expose the UniqueID through its Bridged Device Basic Information Cluster.)"

> "If it is not present, the Fabric Synchronizing Administrator MAY continue the commissioning process."

**[12.64] — Caching and tie-breaker policy:**

> "The Fabric Synchronizing Administrator SHOULD create a cache of prior known UniqueIDs scoped to the NodeID of the Synchronized Device. The cache SHOULD have space for at least 5 entries per NodeID."

> "If a Fabric Synchronizing Administrator (hereafter denoted A) receives a UniqueID from another Fabric Synchronizing Administrator's (hereafter denoted B) Bridged Device Basic Information Cluster that matches an entry in the cache, but is not the entry currently presented in the Bridged Device Basic Information Cluster of the client Fabric Synchronizing Administrator (A), then the Fabric Synchronizing Administrator (A) SHOULD set the UniqueID in its Bridged Device Basic Information Cluster to the value stored in the cache which is lexicographically smaller than all other entries."

### 12.6.5 — Changes to Device Names and Locations

**[12.68]**
> "A Fabric Synchronizing Administrator SHOULD expose such names in the Ecosystem Information Cluster on the associated endpoint. A Fabric Synchronizing Administrator MAY expose such names in the Basic Information Cluster on the associated endpoint."

**[12.69]**
> "If a Fabric Synchronizing Administrator exposes such names in the Basic Information Cluster for a Synchronized Device, then the same associated names SHALL be exposed in the Ecosystem Information Cluster."

**[12.71]**
> "A Fabric Synchronizing Administrator SHOULD expose such grouping using the Ecosystem Information Cluster as described above. A Fabric Synchronizing Administrator MAY expose such grouping in the Basic Information Cluster on the associated endpoint."

**[12.72]**
> "If a Fabric Synchronizing Administrator exposes such grouping in the Basic Information Cluster for a Synchronized Device, then the same associated grouping SHALL be exposed in the Ecosystem Information Cluster."

**[12.73]**
> "Nodes that wish to be notified of a change in such a name or location SHOULD monitor changes of the Basic Information Cluster and of the Ecosystem Information Cluster."

**[12.74]**
> "A Fabric Synchronizing Administrator MAY make it possible (e.g. through a Manufacturer's app) for its users to restrict whether all or some of the Ecosystem Information Cluster is exposed to the Fabric."

### 12.6.6 — Changes to the Set of Synchronized Devices

**[12.75]**
> "When an update to the set of synchronized Devices occurs, the Fabric Synchronizing Administrator SHALL:"

> "Update the PartsList attribute on the Descriptor clusters of the Root Node Endpoint and of the endpoint which holds the Aggregator device type."

> "Update the exposed endpoints and their descriptors according to the new set of Synchronized Devices"

**[12.76]**
> "Nodes that wish to be notified of added/removed devices SHOULD monitor changes of the PartsList attribute in the Descriptor cluster on the Root Node Endpoint and the endpoint which holds the Aggregator device type."

**[12.77]**
> "Allocation of endpoints for Synchronized Devices SHALL be performed as described in Dynamic Endpoint allocation."

### 12.6.8 — Setup Flow

**[12.82]**
> "Before or after commissioning a Fabric Synchronizing Administrator, the user SHALL be asked for consent to enable Fabric Synchronization functionality between ecosystems. This matches the existing single-device Administrator-Assisted Commissioning consent model that requires user consent when Matter devices are shared."

**[12.83]**
> "Each ecosystem SHALL independently ask the user for consent. This can be done before or after commissioning the device."

**[12.88]**
> "The user SHALL be able to enable the administrator-assisted commissioning of an ecosystem's Fabric Synchronization feature through an appropriate interface of the devices on that ecosystem. For example, a mobile application, a web configuration, or an on-device interface."

**[12.90]**
> "The Commissionee SHALL provide the user with Manual Pairing Code and MAY provide the user with a QR code to initiate commissioning of the Commissionee by the Commissioner."

**[12.91]**
> "The Commissionee SHALL advertise its presence over DNS-SD (see Section 5.4.2.7, "Using Existing IP-bearing Network" and Section 4.3.1, "Commissionable Node Discovery".)"

**[12.92]**
> "A Commissioner MAY discover the Commissionee device and provide the user with a notification prior to additional user action."

**[12.93]**
> "The user SHALL then be able to initiate commissioning on another administrator with the Fabric Synchronization feature using the provided QR code or manual pairing code from the Commissioner."

**[12.94]**
> "The Commissioner SHALL commission the Commissionee using the steps outlined in the Concurrent connection commissioning flow."

**[12.96]**
> "After the Commissioner has finished commissioning the Commissionee, the Commissioner SHOULD initiate Reverse Commissioning using the Commissioner Control Cluster."

**[12.97]**
> "After or before commissioning (and optionally Reverse Commissioning,) the device-appropriate interfaces for the Fabric Synchronization feature SHALL ask the user for consent to synchronize devices between fabrics according to Scope Of User Consent."

**[12.100]**
> "The Fabric Synchronizing Administrator SHALL commission the synchronized devices as configured by the user."

**[12.101] — Commissioning of Matter devices SHALL be performed by:**

> "Discover available Synchronized Devices to commission by identifying endpoints specified in the PartsList of the Aggregator within the Fabric Synchronizing Administrator."

> "Identify if the Synchronized Device supports the BridgedICDSupport feature in the Bridged Device Basic Information Cluster by presence of the BridgedICDSupport feature."

> "If the BridgedICDSupport feature is present, the client MAY use the BridgedICDSupport feature to ensure the device is active."

> "Initiate commissioning by sending an OpenCommissioningWindow command to the Administrator Commissioning Cluster exposed on the endpoint with the Bridged Node device type specified in the PartsList and the Administrator Commissioning Cluster present."

> "Complete commissioning using the Enhanced Commissioning Method."

**[12.86] — Informational setup steps (SHOULD):**

> "To initiate the setup of Fabric Synchronization, the manufacturer-specific setup SHOULD include the following steps: Scan a QR code or enter the Manual Pairing Code of the Commissionee Fabric Synchronizing Administrator. This is similar to the current Administrator-Assisted Commissioning of Matter devices. Consent and configure relationships on both ecosystems. (The Commissionee ecosystem MAY provide a configuration step prior to providing the QR code or Manual Pairing Code.)"

**Configurable consent options (MAY):**

> "The Fabric Synchronization feature MAY ask the user for consent for all Synchronized Devices or consent for smaller subsets independently."

> "The Fabric Synchronization feature MAY ask the user for consent to perform this operation automatically when new Synchronized Devices are commissioned."

> "The Fabric Synchronization feature MAY ask the user for consent to share Synchronized Device metadata such as device names and locations."

---

## 4. Data Structures

### Clusters Referenced

| Cluster | Usage |
|---|---|
| Commissioner Control Cluster | Bidirectional Fabric Synchronization setup; contains `SupportedDeviceCategories` attribute with `FabricSynchronization` bit |
| Ecosystem Information Cluster | Required on each Bridged Node endpoint (FabricSynchronizedNode); exposes names and grouping of Synchronized Devices |
| Administrator Commissioning Cluster | Required on Bridged Node endpoint when user consents to share; target for `OpenCommissioningWindow` command |
| Bridged Device Basic Information Cluster | Contains `UniqueID`, `VendorID`, `ProductID` fields; optionally supports `BridgedICDSupport` feature |
| Basic Information Cluster | May contain device names and grouping; if used, same data must be mirrored in Ecosystem Information Cluster |
| Descriptor Cluster | Contains `PartsList` attribute on Root Node Endpoint and Aggregator endpoint; updated when Synchronized Devices change |

### Attributes Referenced

| Attribute | Cluster | Purpose |
|---|---|---|
| `SupportedDeviceCategories` | Commissioner Control Cluster | Must have `FabricSynchronization` bit set to indicate Fabric Synchronization support |
| `PartsList` | Descriptor Cluster | Lists endpoints of Synchronized Devices; monitored for add/remove events |
| `UniqueID` | Basic Information Cluster / Bridged Device Basic Information Cluster | Used for deduplication; generated and persisted if absent |
| `VendorID` | Bridged Device Basic Information Cluster | Used in conjunction with UniqueID for deduplication |
| `ProductID` | Bridged Device Basic Information Cluster | Used in conjunction with UniqueID for deduplication |

### Commands Referenced

| Command | Cluster | Purpose |
|---|---|---|
| `OpenCommissioningWindow` | Administrator Commissioning Cluster | Initiates commissioning of a Synchronized Device |

### UniqueID Cache Structure

- Scoped to NodeID of each Synchronized Device
- SHOULD have space for at least 5 entries per NodeID
- Tie-breaker: lexicographically smallest UniqueID wins when a conflict is detected

---

## 5. Security Considerations

**User Consent:**
- The user SHALL be asked for consent to enable Fabric Synchronization functionality before or after commissioning a Fabric Synchronizing Administrator ([12.82]).
- Each ecosystem SHALL independently ask the user for consent ([12.83]).
- The consent model matches the existing single-device Administrator-Assisted Commissioning consent model that requires user consent when Matter devices are shared ([12.82]).

**Mutual Authentication:**
- As a precondition to enabling Fabric Synchronization, ecosystems MAY wait until complete bi-directional commissioning of both ecosystems has been completed before exposing any Bridged Nodes. At this point, both ecosystems have received device attestation from the other ([12.81]).
- A Commissionee Fabric Synchronizing Administrator MAY choose to require reverse commissioning before enabling Fabric Synchronization ([12.96 Note]).

**Fabric Isolation:**
- Client ecosystems each have their own dedicated, isolated fabric separate from the fabric used by the Aggregator to interact with Matter devices. Ecosystem E1 can directly access devices and the Aggregator of Ecosystem E2 from E1's fabric without requiring E2 to have any access granted on E1's fabric ([12.78], [12.79]).

**Device Discovery:**
- The Commissionee SHALL advertise its presence over DNS-SD ([12.91]).
- The Commissionee SHALL provide the user with a Manual Pairing Code ([12.90]).

**Metadata Exposure Control:**
- A Fabric Synchronizing Administrator MAY make it possible (e.g., through a Manufacturer's app) for its users to restrict whether all or some of the Ecosystem Information Cluster is exposed to the Fabric ([12.74]).

---

## 6. Error Handling

### Device Duplication Prevention

The spec defines a multi-layered deduplication strategy with specific failure/avoidance behavior:

1. **Match detected via UniqueID + VendorID + ProductID**: The Fabric Synchronizing Administrator SHOULD NOT attempt to commission the device ([12.59]).

2. **Missing UniqueID on Synchronized Device**: The Fabric Synchronizing Administrator SHALL generate and persist a new UniqueID to be used in the Bridged Device Basic Information Cluster ([12.62]).

3. **PASE session check — fabric already present in Fabric Table**: The Fabric Synchronizing Administrator SHOULD NOT complete commissioning and SHOULD avoid attempting to commission the device (or establish PASE sessions) in the future by persisting the UniqueID exposed by the other Fabric Synchronizing Administrator's Bridged Device Basic Information Cluster ([12.63]).

4. **UniqueID loop prevention (caching + tie-breaker)**:
   - Required when a Fabric Synchronizing Administrator updated its Bridged Device Basic Information Cluster as a result of a duplication detected during a PASE session ([12.64]).
   - If a received UniqueID matches a cache entry but is not the currently presented value, the Fabric Synchronizing Administrator SHOULD set the UniqueID to the lexicographically smallest entry in the cache ([12.64]).