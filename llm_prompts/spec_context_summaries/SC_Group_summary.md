# Matter Spec Summary: SC Group

**Source sections matched:** 4  
**Source chars sent to LLM:** 4,683  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 1,224  

---

## 1. Overview

This section specifies the semantics of sending and receiving multicast group messages and the lifecycle of groupcast sessions. Multicast addressing uses a 16-bit Group ID as the destination address. A multicast group is a collection of Nodes registered under the same multicast Group ID. A multicast message is sent to a particular destination group and received by all members of that group. Group membership is tracked in the Group Key Management Cluster. Groupcast sessions are conceptually long-running, lasting the duration of a node's membership in a group.

---

## 2. Protocol Flow & State Machine

### Session Context Lifecycle

On ingress of each groupcast message, an ephemeral Groupcast Session Context is constructed to inform upper layers of message provenance. This context contains:
- **Fabric Index** — the local Fabric Index for the Fabric to which the incoming message's group is scoped
- **Group ID** — the Group ID to which the groupcast message was sent
- **Source Node ID** — the Source Node ID enclosed by the sender
- **Source IP Address** — the unicast source IP address of the originator
- **Source Port** — the source port of the originator
- **Operational Group Key** — the key used to encrypt the incoming group message
- **Group Session ID** — derived from the Operational Group Key used to encrypt the message

The Source IP Address and Source Port may be used for unicast responses to group communication peers, as required for the Message Counter Synchronization Protocol.

### Sending a Group Message

To prepare a multicast message to a Group ID with a given GroupKeySetID and IPv6 hop count parameter:

1. Obtain the current Operational Group Key (as Encryption Key) and associated Group Session ID for the given GroupKeySetID.
2. Perform Message Transmission processing (Section 4.7.1) with:
   - Destination Node Id = Group Node Id corresponding to the given Group ID
   - Session Id = Group Session ID from step 1
   - Security Flags = only the P Flag set
   - Transport = a site-local routable IPv6 interface
3. Prepare the message as an IPv6 packet:
   - Set the secured message from step 2 as the IPv6 payload
   - Set the IPv6 hop count to the given value
   - Set the IPv6 destination based on the Section 2.5.6.2 IPv6 Multicast Address derived from the destination Group ID, Fabric ID, and the address policy of that group:
     - Look up the group data record based on Fabric ID and Group ID
     - If sourced by the Groupcast cluster, set IPv6 address per the McastAddrPolicy field (Section 11.29.5.4.5)
     - Otherwise set IPv6 address per the PerGroup Multicast Address rules
   - Set the IPv6 source to an operational IPv6 Unicast Address of the sending Node
   - Set the IPv6 UDP port number to the Matter IPv6 multicast port

### Receiving a Group Message

Nodes supporting groups register to receive on the associated IPv6 multicast address at the Matter IPv6 multicast port for each group they belong to. Upon receiving an IPv6 message addressed to one of these registered multicast addresses, the Node extracts the message from the IPv6 payload and performs Message Reception processing (Section 4.7.2).

---

## 3. Normative Requirements

### 4.16.1 Groupcast Session Context

- "on ingress of each groupcast message, the following ephemeral context **SHALL** be constructed to inform upper layers of groupcast message provenance"
- "The source IP address and port **MAY** be used for unicast responses to group communication peers, as are required for the Message Counter Synchronization Protocol."
- "Once a Groupcast Session Context with trust-first policy is created to track authenticated messages from a given Source Node ID, that record **SHALL NOT** be deleted or recycled until the node reboots."
- "Any message from a source that cannot be tracked **SHALL** be dropped."

### 4.16.2 Sending a Group Message

- "the Node **SHALL**: Obtain, for the given GroupKeySetID, the current Operational Group Key as the Encryption Key, and the associated Group Session ID."
- "If no key is found for the given GroupKeySetID, security processing **SHALL** fail and no further security processing **SHALL** be done on this message."
- "The Destination Node Id argument **SHALL** be the Group Node Id corresponding to the given Group ID."
- "The Session Id argument **SHALL** be the Group Session ID from step 1."
- "The Security Flags **SHALL** have only the P Flag set."
- "The transport **SHALL** be a site-local routable IPv6 interface."

### 4.16.3 Receiving a Group Message

- "All Nodes supporting groups **SHALL** register to receive on the associated IPv6 multicast address, at the Matter IPv6 multicast port, for each group of which they are a member."
- "Upon receiving an IPv6 message addressed to one of these Multicast Addresses the Node is registered for, the Node **SHALL**: Extract the message from the IPv6 payload. Perform Section 4.7.2, 'Message Reception' processing steps on the message."

---

## 4. Message Formats & Data Structures

**Groupcast Session Context fields** (constructed on ingress of each groupcast message):

| Field | Description |
|---|---|
| Fabric Index | Local Fabric Index for the Fabric to which the incoming message's group is scoped |
| Group ID | The Group ID to which the groupcast message was sent |
| Source Node ID | The Source Node ID enclosed by the sender |
| Source IP Address | Unicast source IP address of the originator |
| Source Port | Source port of the originator |
| Operational Group Key | The key used to encrypt the incoming group message |
| Group Session ID | Derived from the Operational Group Key used to encrypt the message |

**IPv6 packet construction for outgoing group message:**
- Payload: the private, secured message
- Hop count: the given IPv6 hop count parameter
- Destination: IPv6 Multicast Address per Section 2.5.6.2, based on Group ID, Fabric ID, and group address policy
- Source: an operational IPv6 Unicast Address of the sending Node
- UDP port: the Matter IPv6 multicast port

**Security Flags:** Only the P Flag is set for outgoing group messages.

---

## 5. Security Considerations

- The Operational Group Key obtained for the given GroupKeySetID is used as the Encryption Key for outgoing group messages.
- If no key is found for the given GroupKeySetID, security processing SHALL fail and no further security processing SHALL be done on the message.
- Once a Groupcast Session Context with trust-first policy is created to track authenticated messages from a given Source Node ID, that record SHALL NOT be deleted or recycled until the node reboots. This is to prevent replay attacks that first exhaust the memory allocated to group session counter tracking and then inject older messages as valid, and to enforce that trust-first authentication works as intended within the full duration of a boot cycle.
- Any message from a source that cannot be tracked SHALL be dropped.

---

## 6. Error Handling & Timing

- If no Operational Group Key is found for the given GroupKeySetID, security processing SHALL fail and no further security processing SHALL be done on this message (the message is not sent).
- Any incoming groupcast message from a source that cannot be tracked (i.e., cannot have a Groupcast Session Context allocated for it) SHALL be dropped.

(Not covered in provided spec sections: timeout values, retry behavior, or recovery procedures.)