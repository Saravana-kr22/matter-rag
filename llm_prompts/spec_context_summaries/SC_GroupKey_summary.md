# Matter Spec Summary: SC GroupKey

**Source sections matched:** 64  
**Source chars sent to LLM:** 67,583  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 5,555  

---

## 1. Overview

Section 4.17 describes **Operational Group Keys** — a mechanism for generating, disseminating, and managing shared symmetric keys across a group of Nodes in a Fabric. Operational group keys allow Nodes to: prove mutual group membership, exchange messages confidentially without fear of manipulation by non-members, and encrypt data decryptable only by other group members.

An **operational group** is a logical collection of Nodes running one or more common application clusters sharing a common security domain via a shared symmetric group key. Individual Nodes may belong to multiple operational groups simultaneously; group membership can change over time. Subgroups can be formed by defining distinct Group Identifiers within the same key domain.

Section 4.18 describes the **Message Counter Synchronization Protocol (MCSP)**, which handles replay-protection counter synchronization for group messages.

Section 4.19 describes the **Bluetooth Transport Protocol (BTP)**, providing a packetized stream interface over GATT for transporting Matter messages over BLE.

Key roles:
- **Administrator** (key distribution Administrator): generates, manages, and distributes epoch keys; assigns Group IDs; pushes updates to authorized Nodes.
- **Node** (group member): derives operational group keys from epoch key material; participates in group communication.
- **BTP Client / BTP Server**: GATT client/server roles for BLE session establishment and data transfer.

---

## 2. Protocol Flow & State Machine

### 2.1 Operational Group Key Derivation

An operational group key is produced by applying a key derivation function (Crypto_KDF) with an epoch key and salt (CompressedFabricIdentifier) as inputs. The Info portion is the hard-coded group security info `"GroupKey v1.0"` (bytes `0x47 0x72 0x6f 0x75 0x70 0x4b 0x65 0x79 0x20 0x76 0x31 0x2e 0x30`). Group membership is enforced by restricting access to epoch keys; only Nodes possessing the input epoch key can derive the operational key.

### 2.2 Installing a Group on a Newly Commissioned Node

The sequence to install a group onto a newly commissioned Node is:

1. **Admin**: Generate a fabric-unique Group ID (`GID`) and random key `key0` for group key set ID `K`.
2. **Admin**: Write group key set `K` to the remote Node (`GroupMember`) using the `KeySetWrite` command.
3. **Admin**: Associate Group ID `GID` with key set `K` by writing an entry to the `GroupKeyMap` list attribute.
4. **GroupMember**: Node subscribes to the IPv6 multicast address generated from the Fabric ID and Group ID.
5. **Admin**: Associate endpoint with Group ID `GID` by sending the Groups cluster's `AddGroup` command to the endpoint.

### 2.3 Epoch Key Lifecycle / Rotation State Machine

Each epoch key slot transitions through states: **New** (start time in future), **Current** (active key with newest start time), **Previous** (active key with second newest start time), **Old** (active key with third newest start time).

Two types of state transitions:
- **Admin Refresh**: An entire group key set is freshly written to a Node during commissioning or administration.
- **Epoch Activate**: System time progresses past a key's start time, activating the key and aging out other slots.
- **Admin Update**: Administrator updates an old epoch key with a new epoch key.

When in steady state, the Admin Refresh state MAY be entered in place of an Admin Update state transition.

### 2.4 Epoch Key Rotation Without Time Synchronization

A Node without synchronized time determines the current epoch key by comparing relative start times of received epoch keys and using the key with the **second newest** start time as the current epoch key. It then uses that key for all locally initiated security interactions until next contact with the distribution Administrator.

### 2.5 Group Session ID Derivation

The Group Session ID is derived by applying Crypto_KDF against the Operational Group Key and treating the output as a big-endian 16-bit integer. It is used by receiving nodes to identify candidate Operational Group Keys for decrypting incoming groupcast messages without trying all available keys. On receipt of a group-session-type message, all valid installed operational group key candidates referenced by the Group Session ID SHALL be attempted until authentication passes or no more keys remain.

### 2.6 Message Counter Synchronization Protocol (MCSP)

Two synchronization policies are configurable per group key via `GroupKeySecurityPolicy`:

**Trust-first**: The first authenticated message counter from an unsynchronized peer is trusted and used to configure replay protection. All control messages (C Flag set) SHALL use Trust-first.

**Cache-and-sync**: The triggering message is cached; a synchronization exchange is initiated; the original message is processed only after synchronization completes.

**MCSP Exchange Flow (Cache-and-sync / Scenario 1 — Multicast Receiver Initiated):**

1. **Sender** generates, encrypts, and sends `Msg1` as a multicast message.
2. **Receiver(s)** receive, decrypt, authenticate, and cache `Msg1`; each generates, encrypts, and sends a `MsgCounterSyncReq` (unicast) to Sender.  
   - If triggered by multicast, Receiver SHALL first wait a uniformly random time between 0 and `MSG_COUNTER_SYNC_REQ_JITTER`.
3. **Sender** receives `MsgCounterSyncReq`, generates, encrypts, and sends `MsgCounterSyncRsp` (unicast) to each Receiver.
4. **Receiver(s)** receive, decrypt, and verify `MsgCounterSyncRsp`; on success: mark Sender's group key message counter as synchronized, then process cached `Msg1`.

Only one synchronization exchange per (peer Node ID, group key) pair may be outstanding at a time.

### 2.7 BTP Session Establishment

1. Central establishes a BLE connection to peripheral.
2. Central assumes GATT client role; peripheral assumes GATT server role.
3. Client SHOULD perform GATT Exchange MTU procedure.
4. Client executes GATT discovery (primary service discovery, characteristic discovery for C1/C2, CCCD discovery for C2).
5. Client sends BTP handshake request via `ATT_WRITE_REQ` on C1, including supported protocol versions (descending order), client ATT_MTU, and client maximum receive window size.
6. Client starts `BTP_CONN_RSP_TIMEOUT` timer; upon receipt of GATT Write Response, client enables indications on C2 by writing `0x01` to C2's CCCD.
7. Server (upon receiving handshake request AND confirmed C2 subscription) sends BTP handshake response via `ATT_HANDLE_VALUE_IND` on C2, containing selected window size, maximum BTP packet size, and selected protocol version. Server also starts `BTP_CONN_RSP_TIMEOUT` timer.
8. If server and client share no supported protocol version, server SHALL close the BLE connection.

### 2.8 BTP Data Transmission and Flow Control

- Clients use GATT Write Characteristic Value; servers use GATT Indication.
- BTP SDUs are split into ordered, non-overlapping segments; only one BTP SDU may be in transmission per direction at a time.
- Receive windows provide flow control; when a window is closed (counter = 0), the local peer SHALL NOT send packets.
- Sequence numbers (8-bit, monotonically incrementing, wrapping at 255) are sent on all BTP packets.
- Acknowledgement timers (`BTP_ACK_TIMEOUT`) and send-acknowledgement timers (< ½ ACK timeout) manage ack scheduling.
- An idle BTP session still exchanges acknowledgement packets, providing a keep-alive mechanism.

### 2.9 BTP Session Shutdown

- GATT client SHALL unsubscribe from C2 to close a BTP session.
- If the BTP Server needs to close the session, it SHALL terminate its BLE connection to the client.

---

## 3. Normative Requirements

### 4.17 Group Key Management

- [4.540] "credentials required to generate operational group keys **SHALL** only be accessible to Nodes with a certain level of privilege — those deemed a member of the group."
- [4.540] "access to shared keys **SHALL** be computationally infeasible for non-trusted parties."
- [4.543] "Groups **MAY** be introduced or withdrawn over time as need arises."
- [4.544] "Administrators **SHALL** assign Group Ids such that no two operational groups within a Fabric have the same Group ID."
- [4.545] "Multiple operational groups **MAY** share the same operational group key."
- [4.545] "Operational groups which do not share related functionality, such as a lighting group and a sprinkler group, **SHOULD NOT** share the same operational key."
- [4.554] "all input key material **SHALL** be maintained on a per-Fabric basis."

### 4.17.2.1 Group Security Info

- [4.553] "The group security info **SHALL** be the byte stream 'GroupKey v1.0', i.e. 0x47 0x72 0x6f 0x75 0x70 0x4b 0x65 0x79 0x20 0x76 0x31 0x2e 0x30."

### 4.17.3 Epoch Keys

- [4.558] "Each key **SHALL** be a random value of length CRYPTO_SYMMETRIC_KEY_LENGTH_BITS bits."

### 4.17.3.1 Using Epoch Keys

- [4.560] "Nodes sending group messages **SHALL** use operational group keys that are derived from the current epoch key (specifically, the epoch key with the latest start time that is not in the future)."
- [4.561] "Nodes receiving group messages **SHALL** accept the use of any key derived from one of the currently installed epoch keys."
- [4.562] "An epoch key marked with the maximum start time **SHALL** be disabled and render the corresponding epoch key slot unused."

### 4.17.3.2 Managing Epoch Keys

- [4.563] "For every group key set published by the key distribution Administrator, there **SHALL** be at least 1 and at most 3 epoch keys in rotation."
- [4.564] "An epoch key update **SHALL** order the keys from oldest to newest."
- [4.565] "Any epoch key update **MAY** deliver a partial key set but **SHALL** include EpochKey0 and **MAY** include EpochKey1 and EpochKey2."
- [4.565] "Any update of the key set, including a partial update, **SHALL** remove all previous keys in the set, however many were defined."
- [4.566] "An Administrator **MAY** completely remove a group key set from a Node using the KeySetRemove command."

### 4.17.3.4 Epoch Key Rotation Without Time Synchronization

- [4.570] "The Administrator **SHOULD** provide a sufficient set of epoch keys to Nodes that do not maintain synchronized time so that they can maintain communication with other group members while a key update is in progress."
- [4.570] "The key distribution Administrator **SHOULD** update all Nodes without time, such as ICDs, before the new epoch key is activated, and then let Nodes with time all roll to the new epoch key at the synchronized start time."

### 4.17.3.5 Group Key Set ID

- [4.576] "The Group Key Set ID of 0 **SHALL** be reserved for managing the Identity Protection Key (IPK) on a given Fabric."
- [4.576] "It **SHALL NOT** be possible to remove the IPK Key Set if it exists."

### 4.17.3.6 Group Session ID

- [4.577] "When Session Type is 1, denoting a group session, the Session ID **SHALL** be a Group Session ID as defined here."
- [4.581] "The Group Session ID **MAY** help receiving nodes efficiently locate the Operational Group Key used to encrypt an incoming groupcast message. It **SHALL NOT** be used as the sole means to locate the associated Operational Group Key, since it **MAY** collide within the fabric."
- [4.582] "On receipt of a message of Group Session Type, all valid, installed, operational group key candidates referenced by the given Group Session ID **SHALL** be attempted until authentication is passed or there are no more operational group keys to try."

### 4.18.1.1 Trust-first Policy

- [4.593] "All control messages (any message with C Flag set) use the control message counter and **SHALL** use Trust-first for synchronization."

### 4.18.2 Group Peer State

- [4.599] "There **SHALL** be at least 10 entries per supported fabric for Peer Encrypted Group data Message Status in the Group Peer State table."
- [4.599] "This number **SHOULD NOT** be less than 5 entries per fabric per instance of Groups cluster on an endpoint."
- [4.600] "There **SHALL** be at least 2 entries per supported fabric for Peer Encrypted Group Control Message Status in the Group Peer State table."

### 4.18.3.1 MsgCounterSyncReq

- [4.603] "A MsgCounterSyncReq message **SHALL** set the C Flag in the message header. The control message counter **SHALL** be used for message protection."
- [4.604] "A MsgCounterSyncReq message **SHALL** be secured with the group key for which counter synchronization is requested and **SHALL** set the Session Type to 1."

### 4.18.3.2 MsgCounterSyncRsp

- [4.607] "A MsgCounterSyncRsp message **SHALL** set the C Flag in the message header. The control message counter **SHALL** be used for message protection."

### 4.18.4 Unsynchronized Message Processing

- [4.609] "The message **SHALL** be of Group Session Type, otherwise discard the message."
- [4.609] "If the key has a trust-first security policy, the receiver **SHALL**: Set the peer's group key data message counter to Message Counter of the message. Clear the Message Reception State bitmap for the group session from the peer. Mark the peer's group key data message counter as synchronized. Process the message."
- [4.609] "If the key has a cache-and-sync security policy, the receiver **SHALL**: If MCSP is not in progress for the given peer Node ID and group key: Store the message for later processing. Proceed to Section 4.18.5."
- [4.609] "An implementation **MAY** queue the message for later processing after MCSP completes if resources allow."
- [4.610] "For each peer Node ID and group key pair there **SHALL** be at most one synchronization exchange outstanding at a time."

### 4.18.5 Message Counter Synchronization Exchange — Sender of MsgCounterSyncReq

- [4.611] "When a synchronization request is triggered by an incoming multicast message, the Node **SHALL** first wait for a uniformly random amount of time between 0 and MSG_COUNTER_SYNC_REQ_JITTER."
- [4.612] The S Flag **SHALL** be set to 1.
- [4.612] The DSIZ field **SHALL** be set to 1.
- [4.612] The P Flag **SHALL** be set to 1.
- [4.612] The C Flag **SHALL** be set to 1.
- [4.612] The Session Type field **SHALL** be set to 1.
- [4.612] The Session ID field **SHALL** be set to the Group Session Id for the operational group key being synchronized.
- [4.612] The Source Node ID field **SHALL** be set to the Node ID of the sender Node.
- [4.612] The Destination Node ID field **SHALL** be set to the Source Node ID of the message that triggered the synchronization attempt.
- [4.612] The Exchange ID of the message **SHALL** be set to match the new Exchange.
- [4.612] The I Flag **SHALL** be set to 1.
- [4.612] The A Flag **SHALL** be set to 0.
- [4.612] The R Flag **SHALL** be set to 1.
- [4.612] Upon timer firing: "The synchronization exchange **SHALL** be closed. Any message waiting on synchronization associated with the exchange **SHALL** be discarded."
- [4.612] "The request message **SHALL** use the same operational group key as the message which triggered synchronization."
- [4.612] "The group key **SHALL** be known/derivable by both parties (sender and receiver)."

### 4.18.5 Receiver of MsgCounterSyncReq

- [4.613] "Verify the Destination Node ID field **SHALL** match the Node ID of the receiver, otherwise discard the message."

### 4.18.5 Sender of MsgCounterSyncRsp

- [4.614] The S Flag **SHALL** be set to 1.
- [4.614] The DSIZ field **SHALL** be set to 1.
- [4.614] The P Flag **SHALL** be set to 1.
- [4.614] The C Flag **SHALL** be set to 1.
- [4.614] The Session Type field **SHALL** be set to 1.
- [4.614] The Session ID field **SHALL** be set to the Group Session Id for the operational group key being synchronized.
- [4.614] The Source Node ID field **SHALL** be set to the Node ID of the sender Node.
- [4.614] The Destination Node ID field **SHALL** be set to the Source Node ID of the MsgCounterSyncReq.
- [4.614] "The Response field **SHALL** be set to the value of the Challenge field from the MsgCounterSyncReq."
- [4.614] "The Synchronized Counter field **SHALL** be set to the current Global Group Encrypted Data Message Counter of the sender."
- [4.614] The Exchange ID **SHALL** be set to the Exchange ID of the MsgCounterSyncReq.
- [4.614] The I Flag **SHALL** be set to 0.
- [4.614] The A Flag **SHALL** be set to 1.
- [4.614] The R Flag **SHALL** be set to 1.

### 4.18.5 Receiver of MsgCounterSyncRsp (verification requirements)

- [4.615] "An active synchronization exchange **SHALL** exist with the source node."
- [4.615] "The Exchange ID field **SHALL** match the Exchange ID used for the original MsgCounterSyncReq message."
- [4.615] "The Response field **SHALL** match the Challenge field used for the original MsgCounterSyncReq message."
- [4.615] "The Destination Node ID field **SHALL** match the Source Node ID of the original MsgCounterSyncReq message."
- [4.615] "The Source Node ID field **SHALL** match the Destination Node ID of the original MsgCounterSyncReq message."
- [4.615] On verification failure: "Silently ignore the MsgCounterSyncRsp message."
- [4.615] On success: "If more than one message is queued from the synchronized peer, using the same operational group key, the messages **SHALL** be processed in the order received."

### 4.18.6 MCSP Session Context

- [4.616] "nodes **SHALL** maintain the following session context."

### 4.19.2 BTP Frame Format

- [4.627] "Unused fields **SHALL** be set to '0'."
- [4.629] "All segments of a message **SHALL** set this bit [M bit] to the same value."

### 4.19.3.1 BTP Handshake Request

- [4.643] "If BTP is not aware of the negotiated GATT MTU, the value **SHALL** be set to '23'."

### 4.19.3.2 BTP Handshake Response

- [4.648] Reserved field: "Must be set to '0'."

### 4.19.4.2 BTP GATT Service

- [4.653] "The client **SHALL** exclusively use C1 to initiate BTP sessions by sending BTP handshake requests and send data to the server via GATT ATT_WRITE_REQ PDUs."
- [4.653] "the server **SHALL** exclusively use C2 to respond to BTP handshake requests and send data to the client via GATT ATT_HANDLE_VALUE_IND PDUs."
- [4.654] "For all messages sent from the BTP Client to BTP Server, BTP **SHALL** use the GATT Write Characteristic Value sub-procedure. For all messages sent from the BTP Server to BTP Client, BTP **SHALL** use the GATT Indications sub-procedure."
- [4.655] "The values of C1 and C2 **SHALL** both be limited to a maximum length of 247 bytes."
- [4.657] "BTP Clients **SHALL** perform certain GATT operations synchronously, including GATT discovery, subscribe, and unsubscribe operations. GATT discovery, subscribe, or unsubscribe **SHALL NOT** be initiated while the result of a previous operation remains outstanding."

### 4.19.4.3 Session Establishment

- [4.658] "Before a BTP session can be initiated, a central **SHALL** first establish a BLE connection to a peripheral."
- [4.658] "centrals **SHALL** assume the GATT client role for BTP session establishment and data transfer, and peripherals **SHALL** assume the GATT server role."
- [4.658] "If peripheral supports LE Data Packet Length Extension (DPLE) feature it **SHOULD** perform data length update procedure before establishing a GATT connection."
- [4.659] "Before establishing a BTP session, the GATT client **SHOULD** perform a GATT Exchange MTU procedure."
- [4.660] "the BTP Client **SHALL** execute the GATT discovery procedure."
- [4.661] "The BTP Client **SHALL** perform either the GATT Discover All Characteristics of a Service sub-procedure or the GATT Discover Characteristics by UUID sub-procedure in order to discover the C1 and C2 characteristics."
- [4.662] "The BTP Client **SHALL** perform the GATT Discover All Characteristic Descriptors sub-procedure in order to discover the Client Characteristic Configuration descriptor (CCCD) of C2 characteristic."
- [4.663] "a BTP Client **SHALL** send a BTP handshake request packet to the BTP Server via a ATT_WRITE_REQ PDU on characteristic C1." "The list of supported protocol versions **SHALL** be sorted in descending numerical order." "If the client cannot determine the BLE connection's ATT_MTU, it **SHALL** specify a value of '23'."
- [4.664] "the BTP Client **SHALL** enable indications over C2 characteristics by writing value 0x01 to C2's Client Characteristic Configuration descriptor."
- [4.665] "it **SHALL** send a BTP handshake response to the client via a ATT_HANDLE_VALUE_IND PDU on C2." "This response **SHALL** contain the window size, maximum BTP packet size, and BTP protocol version selected by the server."
- [4.666] "The server **SHALL** select a window size equal to the minimum of its and the client's maximum window sizes." "the server **SHALL** select a maximum BTP Segment Size for the BLE connection by taking the minimum of 244 bytes…, server's ATT_MTU-3 and ATT_MTU-3 as declared by the client."
- [4.667] "The server **SHALL** select a BTP protocol version that is the newest which it and the client both support." "The version number returned in the handshake response **SHALL** determine the version of the BTP protocol used by client and server for the duration of the session."
- [4.668] "If the server determines that it and the client do not share a supported BTP protocol version, the server **SHALL** close its BLE connection to the client." "If this timer expires before the client receives a handshake response from the server, the client **SHALL** close the BTP session and report an error to the application." "If this timer expires before the server receives a subscription request on C2, the server **SHALL** close the BTP session and report an error to the application."

### 4.19.4.4 Data Transmission

- [4.669] "Clients **SHALL** exclusively use GATT Write Characteristic Value sub-procedure to send data to servers and servers **SHALL** exclusively use GATT Indication sub-procedure to send data to clients."
- [4.670] "All BTP packets sent on an open BLE connection **SHALL** adhere to the BTP Packet PDU binary data format." "All BTP packets **SHALL** include a header flags byte and an 8-bit unsigned sequence number."

### 4.19.4.5 Message Segmentation and Reassembly

- [4.672] "that BTP SDU **SHALL** be split into ordered, non-overlapping BTP segments." "Each BTP segment **SHALL** be prepended with a BTP packet header." "the BTP segments **SHALL** be sent in order of their position in the original BTP SDU."
- [4.673] "The transmission of BTP segments of any two BTP SDUs **SHALL NOT** overlap." "the new BTP SDU **SHALL** be appended to a first-in, first-out queue."
- [4.674] "The first BTP segment of a BTP SDU sent over a BTP session **SHALL** have its Beginning Segment header flag set." "The last BTP segment for a given BTP SDU **SHALL** have its Ending Segment flag set." "A BTP packet that bears an unsegmented BTP SDU…**SHALL** have both its Beginning Segment and Ending Segment flags set."
- [4.677] "it **SHALL** reassemble them in the order received, and verify that the reassembled BTP SDU's total length matches that specified by the Beginning Segment's Message Length value. If they match, the receiver **SHALL** pass the reassembled BTP SDU up to the next-higher-layer." "the receiver BTP **SHALL** close the BTP session and report an error to the application" [on mismatch, size exceeded, or segment ordering errors].

### 4.19.4.6 Sequence Numbers

- [4.678] "All BTP packets **SHALL** be sent with sequence numbers, regardless of whether they contain SDU segments." "A BTP sequence number **SHALL** be defined as an unsigned 8-bit integer value that monotonically increments by 1 with each packet sent by a given peer. A sequence number incremented past 255 **SHALL** wrap to zero."
- [4.679] "Sequence numbers **SHALL** be separately defined for either direction of a BTP session." "the sequence number of the first packet sent by the client after completion of the BTP session handshake **SHALL** be zero." "the sequence number of the first data packet sent by the server after completion of the BTP session handshake **SHALL** be 1."
- [4.680] "Peers **SHALL** check to ensure that all received BTP packets properly increment the sender's previous sequence number by 1. If this check fails, the peer **SHALL** close the BTP session and report an error to the application."

### 4.19.4.7 Receive Windows

- [4.683] "Both peers in a BTP session **SHALL** define a receive window." "A maximum window size **SHALL** be established for both peers as part of the BTP session handshake." "the largest maximum window size any peer may support is 255."
- [4.684] "Both peers **SHALL** maintain a counter to reflect the current size of the remote peer's receive window." "Each peer **SHALL** decrement this counter when it sends a packet…and increment this counter when a sent packet is acknowledged."
- [4.685] "If a local peer's counter for a remote peer's receive window is zero, the window **SHALL** be considered closed, and the local peer **SHALL NOT** send packets until the window reopens."
- [4.686] "A local peer **SHALL** also not send packets if the remote peer's receive window has one slot open and the local peer does not have a pending packet acknowledgement." "a server **SHALL** initialize its counter for the client's receive window size at (negotiated maximum window size - 1). A client **SHALL** initialize its counter for the server's receive window at the negotiated maximum window size."
- [4.687] "Both peers **SHALL** also keep a counter of their own receive window size based on the sequence number difference between the last packet they received and the last packet they acknowledged."

### 4.19.4.8 Packet Acknowledgements

- [4.690] "BTP packet receipt acknowledgements **SHALL** be received as unsigned 8-bit integer values in the header of a BTP packet. The value of this field **SHALL** indicate the sequence number of the acknowledged packet."
- [4.693] "Each peer **SHALL** maintain an acknowledgement-received timer. When a peer sends any BTP packet, it **SHALL** start this timer if it is not already running."
- [4.694] "If a peer's acknowledgement-received timer expires, or if a peer receives an invalid acknowledgement, the peer **SHALL** close the BTP session and report an error to the application."
- [4.695] "a server **SHALL** start its acknowledgement-received timer when it sends a handshake response."
- [4.696] "When it receives any BTP packet, a peer **SHALL** record the packet's sequence number as the corresponding BTP session's pending acknowledgement value and start the send-acknowledgement timer if it is not already running."
- [4.697] "If this timer expires and the peer has a pending acknowledgement, the peer **SHALL** immediately send that acknowledgement. If the peer sends any packet before this timer expires, it **SHALL** piggyback any pending acknowledgement on the transmitted packet and stop the send-acknowledgement timer."
- [4.698] "a client **SHALL** set its pending acknowledgement value to zero and start its send-acknowledgement timer when it receives the server's handshake response."
- [4.699] "If a peer detects that its receive window has shrunk to two or fewer free slots, it **SHALL** immediately send any pending acknowledgement as a stand-alone BTP packet."

### 4.19.4.10 Connection Shutdown

- [4.701] "To close a BTP session, a GATT client **SHALL** unsubscribe from characteristic C2. The GATT server **SHALL** take this action to indicate closure of any BTP session open to the client."
- [4.702] "If a BTP Server needs to close the BTP session, it **SHALL** terminate its BLE connection to the client."

---

## 4. Message Formats & Data Structures

### MsgCounterSyncReq Payload (Table 26)

| Field Size | Message Field | Description |
|---|---|---|
| 8 bytes | Challenge | 64-bit random number generated using DRBG by the initiator to uniquely identify the synchronization request cryptographically. |

### MsgCounterSyncRsp Payload (Table 27)

| Field Size | Message Field | Description |
|---|---|---|
| 4 bytes | Synchronized Counter | The current data message counter for the node sending the MsgCounterSyncRsp. |
| 8 bytes | Response | SHALL be the same as the 64-bit value sent in the Challenge field of the corresponding MsgCounterSyncReq. |

### Group Session ID Derivation

Derived via Crypto_KDF against the Operational Group Key with empty (zero-length) input; output treated as big-endian 16-bit integer.

Example: Operational Group Key `a6:f5:30:6b:af:6d:05:0a:f2:3b:a4:bd:6b:9d:d9:60` → Raw GroupKeyHash: `b9:f7` → Group Session ID: `0xB9F7` (47607 decimal).

### BTP Packet PDU Format (Table 28)

```
bit 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15
------------------------------------------------------------
Control Flags | [Management Opcode]
[Ack Number] | [Sequence Number]
[Message Length]
[Segment Payload]...
```

BTP uses little-endian encoding for header fields larger than one byte.

### BTP Control Flags (bit assignments)

| Bit | Flag | Meaning |
|---|---|---|
| 0 | B (Beginning Segment) | Set to '1' on first segment of BTP SDU; indicates presence of Message Length field. |
| 1 | C (Continuing Segment) | '0' on first segment; '1' for all remaining segments of the same SDU. |
| 2 | E (Ending Segment) | '1' on last segment; MAY have both B and E set for unsegmented SDU. |
| 3 | A (Acknowledgement) | Indicates presence of Ack Number field. |
| 5 | M (Management Message) | Indicates presence ('1') or absence ('0') of Management Opcode field. |
| 6 | H (Handshake) | '0' for normal BTP packets; '1' for handshake packets. |

### BTP Control Codes (Table 30)

| Management Opcode | Name | Description |
|---|---|---|
| 0x6C | Handshake | Request and response for BTP session establishment. |

### BTP Handshake Request Format

```
bit 0-7: Control Flags = 0x65
bit 8-15: Management Opcode = 0x6C
Ver[0] | Ver[1] | Ver[2] | Ver[3]
Ver[4] | Ver[5] | Ver[6] | Ver[7]
Requested ATT_MTU (16-bit)
Client Window Size (8-bit)
```

Version nibble values: `0` = unused; `4` = BTP as defined by Matter v1.0.

### BTP Handshake Response Format

```
bit 0-7: Control Flags = 0x65
bit 8-15: Management Opcode = 0x6C
Final Protocol Version (4-bit) | Reserved (must be 0) | Selected ATT_MTU low byte
Selected ATT_MTU high byte | Selected Window Size
```

### BTP GATT Service Characteristics (Table 34)

| Attribute | Description |
|---|---|
| BTP Service | UUID = MATTER_BLE_SERVICE_UUID |
| C1 (Client TX Buffer) | UUID = 18EE2EF5-263D-4559-959F-4F9C429F9D11; Properties = Write; max length = 247 bytes |
| C2 (Client RX Buffer) | UUID = 18EE2EF5-263D-4559-959F-4F9C429F9D12; Properties = Indication; max length = 247 bytes |
| C3 (Additional commissioning data) | UUID = 64630238-8772-45F2-B87D-748A83218F04; Properties = Read; max length = 512 bytes |

### Operational Group Key Derivation Example

- Epoch Key: `23:5b:f7:e6:28:23:d3:58:dc:a4:ba:50:b1:53:5f:4b`
- CompressedFabricIdentifier (Salt): `87:e1:b0:04:e2:35:a1:30`
- Info: `"GroupKey v1.0"` = `0x47 0x72 0x6f 0x75 0x70 0x4b 0x65 0x79 0x20 0x76 0x31 0x2e 0x30`
- Resulting operational group key: `a6:f5:30:6b:af:6d:05:0a:f2:3b:a4:bd:6b:9d:d9:60`

---

## 5. Security Considerations

- Credentials required to generate operational group keys SHALL only be accessible to Nodes with sufficient privilege (group members); access SHALL be computationally infeasible for non-trusted parties.
- Epoch keys are generated on a per-Fabric basis and maintained per-Fabric. Group membership is enforced solely by controlling access to epoch keys.
- The Group Session ID SHALL NOT be used as the sole means to locate an Operational Group Key, since it MAY collide within the Fabric. The collision probability is 2⁻¹⁶; the probability of both Group Session ID collision and MIC match with two different keys is 2⁻⁸⁰.
- The Group Key Set ID 0 is reserved for the Identity Protection Key (IPK); the IPK Key Set SHALL NOT be removable if it exists.
- MsgCounterSyncReq Challenge is a 64-bit value generated by Crypto_DRBG to cryptographically uniquely identify each synchronization request; the response MUST echo this value to prevent replay or mismatched response attacks.
- All group messages must be authenticated; unsynchronized messages are accepted only under trust-first or after MCSP completion — **trust-first is explicitly noted as susceptible to accepting a replayed message after a Node reboot**.
- Cache-and-sync provides replay protection even after Node reboot, at the expense of higher latency; support is optional.
- BTP handshake request and response both set H and M bits to '1'; normal BTP packets set H to '0'. Control Flags for handshake packets are `0x65` on both request and response.
- MCSP messages are secured using the same operational group key as the message triggering synchronization; the group key must be known/derivable by both parties.

---

## 6. Error Handling & Timing

### Timeout Values

| Constant | Description | Default |
|---|---|---|
| `BTP_CONN_RSP_TIMEOUT` | Max time after sending BTP handshake request to wait for handshake response before closing. | 5 seconds |
| `BTP_ACK_TIMEOUT` | Max time after receipt of a segment before a stand-alone ACK must be sent. | 15 seconds |
| `BTP_CONN_IDLE_TIMEOUT` | Max time no unique data has been sent before Central must close the BTP session. | 30 seconds |
| `MSG_COUNTER_SYNC_TIMEOUT` | (Referenced in spec text) Max time to wait for a synchronization response before closing the sync exchange. | (numeric value not provided in supplied spec text) |
| `MSG_COUNTER_SYNC_REQ_JITTER` | Random jitter range (0 to this value) before sending MsgCounterSyncReq on multicast-triggered sync. | (numeric value not provided in supplied spec text) |

Send-acknowledgement timer duration SHALL be any value less than one-half the `BTP_ACK_TIMEOUT`.

### BTP Error Handling

- If sequence number check fails (received packet does not increment sender's previous by 1): peer SHALL close the BTP session and report an error to the application.
- If acknowledgement-received timer expires or invalid acknowledgement is received: peer SHALL close the BTP session and report an error to the application.
- If reassembled BTP SDU length does not match the Beginning Segment's Message Length, or received segment payload would exceed max BTP packet size, or Ending Segment received without prior Beginning Segment, or Beginning Segment received while another SDU transmission is in progress: receiver BTP SHALL close the BTP session and report an error to the application.
- If server and client share no supported BTP protocol version: server SHALL close its BLE connection to the client.
- If BTP_CONN_RSP_TIMEOUT fires at client (no handshake response received): client SHALL close the BTP session and report an error.
- If BTP_CONN_RSP_TIMEOUT fires at server (no C2 subscription received): server SHALL close the BTP session and report an error.

### MCSP Error Handling

- On MsgCounterSyncRsp verification failure: silently ignore the message.
- If MSG_COUNTER_SYNC_TIMEOUT fires: synchronization exchange SHALL be closed; any message waiting on synchronization associated with the exchange SHALL be discarded.
- Unsynchronized messages that are not of Group Session Type: discard the message.
- MsgCounterSyncReq received with Destination Node ID not matching receiver's Node ID: discard the message.
- If cache-and-sync MCSP is already in progress for a (peer Node ID, group key) pair: do not process the new unsynchronized message (MAY queue if resources allow).