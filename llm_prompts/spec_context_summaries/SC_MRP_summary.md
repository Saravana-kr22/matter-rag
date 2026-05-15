# Matter Spec Summary: SC MRP

**Source sections matched:** 36  
**Source chars sent to LLM:** 34,784  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 3,458  

---

## 1. Overview

The Secure Channel MRP (Message Reliability Protocol) section covers two related protocol mechanisms: the **Exchange Layer** and the **Reliable Messaging Protocol (MRP)**. The Exchange Layer groups related messages into logical Exchanges, each bound to exactly one underlying session (secure unicast via PASE or CASE, unsecured, secure group, or MCSP). MRP operates within those Exchanges to provide reliable delivery over unreliable transports (primarily UDP) through retransmission and acknowledgement. Key roles are **Initiator** (the node that sends the first message in an Exchange) and **Responder** (all other participating nodes). The section also covers the Secure Channel Status Report mechanism used for session lifecycle signaling.

---

## 2. Protocol Flow & State Machine

### Exchange Lifecycle

1. The Initiator allocates a fresh Exchange ID (random for the first Exchange; subsequent IDs increment by one, rolling over at max). The Initiator sets the **I Flag** on every message it sends.
2. A new Exchange Context is created, tracking: Exchange ID, Exchange Role (Initiator or Responder), and Session Context. Together these three fields form a unique key.
3. The Responder uses the Exchange ID received in prior messages; it **must not** set the I Flag and **must not** address any node other than the Initiator.
4. Incoming messages are matched to existing Exchanges via the triple {Session, Exchange ID, I Flag/Role polarity}. Unmatched messages are treated as unsolicited.

### Unsolicited Message Handling

- If not a duplicate, has a registered Protocol ID, and I Flag is set → create a new Exchange; forward to upper layer.
- If not matching but R Flag is set → create ephemeral Exchange, send immediate standalone acknowledgement, **do not** forward to upper layer, close the ephemeral Exchange after the ack.
- Otherwise → stop processing.

### Exchange Close Flow

1. Flush any pending acknowledgements (send standalone ack if `StandaloneAckSent = false`).
2. Wait for all pending retransmissions to complete. Remove the Exchange only once the retransmission list is empty.

### MRP Send Flow

1. Check for a matching pending acknowledgement (piggyback processing); if found, set A Flag and populate Acknowledged Message Counter.
2. If reliable delivery over UDP: set R Flag, store message in retransmission table.
3. Transmit. On fatal transport error, notify application and evict from retransmission table. On non-fatal error or no error, start retransmission timer; update send count on each retry up to `MRP_MAX_TRANSMISSIONS`.

### MRP Receive Flow

1. Validate legal flag combinations (R and A flags; drop if Group Session Type and C Flag = 0).
2. Run Exchange Message Matching.
3. Received acknowledgement processing: if A Flag set, look up Acknowledged Message Counter in retransmission table; if found, remove entry and stop timer.
4. Standalone acknowledgement processing: if R Flag set and message is a duplicate, send immediate standalone ack (close ephemeral exchange if applicable) and drop. If R Flag set and not a duplicate, add to acknowledgement table and start acknowledgement timer; if timer fires before cancellation, send standalone ack (set `StandaloneAckSent = true`).

### Session Resource Management (Out of Resources)

When a responder lacks resources after CASE/PASE session establishment:
1. Use SessionTimestamp to find the least-recently-used session.
2. Send `StatusReport(GeneralCode: SUCCESS, ProtocolId: SECURE_CHANNEL, ProtocolCode: CLOSE_SESSION)` to the peer.
3. Remove all state for that session (optionally saving Session Resumption state).
4. Respond to the initiator with the appropriate session establishment message.

### CloseSession Flow

- Sent within a **new** Exchange over an existing PASE or CASE session.
- R Flag **must not** be set.
- After either sending or receiving a CloseSession StatusReport, the node removes all state for that session (optionally retaining Session Resumption state).

### Busy Flow

- Sent only in response to a Sigma1 or PBKDFParamRequest message.
- Carries a 16-bit little-endian minimum retry delay in ProtocolData.
- Initiator waits at least that many milliseconds before retrying with all-new randomized parameters.

---

## 3. Normative Requirements

### 4.10 — Message Exchanges

**[4.330]** "An Exchange SHALL be bound to exactly one underlying session that will transport all associated Exchange messages for the life of that Exchange."

**[4.331]** "The Exchange Layer SHALL NOT accept a message from the upper layer when there is an outbound reliable message pending on the same Exchange."

**[4.337]** "The Node in the Initiator role SHALL always set the I Flag in the Exchange Flags of every message it sends in that Exchange."

**[4.338]** "Each Node in a Responder role for an Exchange SHALL use the Exchange ID received in previous messages for the Exchange."

**[4.338]** "Each Node in the Responder role SHALL NOT set the I Flag in the Exchange Flags of every message it sends in that Exchange."

**[4.338]** "Each Node in a Responder role SHALL NOT set the Destination Node ID field to a value that identifies any Node other than the Node in the Initiator role for the Exchange."

**[4.339]** "Processing SHALL then proceed to Section 4.12.5.1, 'Reliable Message Processing of Outgoing Messages'."

### 4.10.2 — Exchange ID

**[4.333]** "The first message the Initiator sends in a new Exchange SHALL contain a fresh value for the Exchange ID field."

**[4.333]** "The first Exchange ID for a given Initiator Node SHALL be a random integer."

**[4.333]** "All subsequent Exchange IDs created by that Initiator SHALL be the last Exchange ID it created incremented by one."

**[4.334]** "A node SHOULD limit itself to a maximum of 5 concurrent Initiator exchanges over a unicast session."

### 4.10.5.2 — Unsolicited Message Processing

**[4.343]** "The message SHALL NOT be forwarded to the upper layer, and excluding the sending of an immediate standalone acknowledgment, SHALL be ignored." *(duplicate/unknown with R Flag)*

**[4.343]** "Otherwise, processing of the message SHALL stop."

### 4.10.5.3 — Closing an Exchange

**[4.345]** "Any pending acknowledgements associated with the Exchange SHALL be flushed. If there is a pending acknowledgment in the acknowledgement table for the Exchange and it has StandaloneAckSent set to false: Immediately send a standalone acknowledgement for the pending acknowledgement. Remove the acknowledgement table entry for the pending acknowledgement."

### 4.11.1 — Session Establishment Out of Resources

**[4.350]** "a responder SHALL evict an existing session using the following procedure: Use the SessionTimestamp to determine the least-recently used session. … Send a status report: StatusReport(GeneralCode: SUCCESS, ProtocolId: SECURE_CHANNEL, ProtocolCode: CLOSE_SESSION) message to the peer node. Remove all state associated with the session."

### 4.11.1.3 — Secure Channel Status Report Messages

**[4.352]** "All Secure Channel Status Report Messages SHALL use the PROTOCOL_ID_SECURE_CHANNEL protocol id."

**[4.354]** "For each of these cases, a Secure Channel Status Report message SHALL be sent with an appropriate ProtocolCode as detailed below."

**[4.355]** "Secure Channel Status Report messages which are marked as encrypted below SHALL only be sent encrypted in a session established with CASE or PASE."

### 4.11.1.4 — CloseSession

**[4.357]** "The CloseSession StatusReport SHALL only be sent encrypted within an exchange associated with a PASE or CASE session."

**[4.357]** "The CloseSession StatusReport SHALL be sent within a new exchange and SHALL NOT set the R Flag."

**[4.358]** "If a Node has either sent or received a CloseSession StatusReport, that Node SHALL remove all state associated with the session."

### 4.11.1.5 — Busy

**[4.359]** "The BUSY StatusReport SHALL: Set the R Flag to 0. Set the S Flag to 0. Set the StatusReport ProtocolData to a 16-bit (two byte) little-endian value indicating the minimum time in milliseconds to wait before retrying the original request. Set the Exchange ID to the Exchange ID present in the Sigma1 or PBKDFParamRequest message which triggered this response."

**[4.361]** "The BUSY StatusReport SHALL NOT be sent in response to any message except for Sigma1 or PBKDFParamRequest."

**[4.362]** "An initiator receiving a BUSY StatusReport from a responder SHALL wait for at least a period of t milliseconds before retrying the request where t is the value obtained from the Busy StatusReport ProtocolData field."

**[4.363]** "If the initiator sends a new session establishment request after receiving a BUSY StatusReport, the request SHALL contain new values for all randomized parameters."

### 4.12.1 — Reliable Messaging Header Fields

**[4.368]** "This flag SHALL be set by the sender when a message being sent requires the receiver to send back an acknowledgment."

**[4.369]** "When set, the Acknowledged Message Counter field SHALL be present and valid. This flag SHALL always be set for MRP Standalone Acknowledgement messages."

**[4.370]** "This field SHALL be set to the Message Counter of the message that is being acknowledged."

### 4.12.2.1 — Retransmissions

**[4.372]** "the sender SHALL trigger the automatic retry mechanism after a period of mrpBackoffTime milliseconds without receiving an acknowledgement"

**[4.372]** "The sender SHALL retry up to a configured maximum number of times (MRP_MAX_TRANSMISSIONS - 1) before giving up and notifying the application."

**[4.373]** "the sender SHALL choose retransmission timeouts based on the session characteristics of the destination Node exposed via Section 4.3.2, 'Operational Discovery'."

**[4.374]** "The duration of the retransmission timer SHALL be calculated as follows:" *(formula reference in spec)*

**[4.376]** "For each unique Exchange, the sender SHALL wait for the acknowledgement message until the retransmission timer, mrpBackoffTime, expires."

**[4.377]** "An Intermittently Connected Device sender SHOULD increase the mrpBackoffTime by its fast polling interval to take into account the delay that might happen in receiving the acknowledgment while in Active Mode."

**[4.378]** "For the first message of a new exchange, the base interval, i, SHALL be set according to the idle state of the peer node as stored in the Session Context of the session"

**[4.378]** "For all subsequent messages of the exchange, the base interval, i, SHOULD be set according to the active state of the peer node as stored in the Session Context of the session"

**[4.378]** "The backoff base interval SHALL be set to a value at least 10% greater than the idle interval of the destination"

**[4.381]** "The sender SHOULD initiate Section 4.3.2, 'Operational Discovery' in parallel with the first retry to re-resolve the address of the destination Node if the initial transmission fails after one expected round trip."

**[4.381]** "The sender SHOULD use the latest MRP parameters for the destination that result from subsequent Operational Discovery."

**[4.382]** "the sender SHALL initiate Section 4.3.2, 'Operational Discovery' in parallel with the first retry to re-resolve the address of the destination Node if the initial transmission fails after one expected round trip." *(ICD case)*

**[4.382]** "The sender SHALL use the latest MRP parameters for the destination that result from subsequent Operational Discovery." *(ICD case)*

### 4.12.2.2 — Acknowledgements

**[4.383]** "A receiver SHALL acknowledge a reliable message by either using a 'piggybacked' acknowledgment in the next message destined to the peer, or a standalone acknowledgment, or both."

**[4.384]** "The acknowledgement message SHALL set the Acknowledged Message Counter field to the value of the Message Counter of the reliable message to be acknowledged."

### 4.12.2 — Duplicate Message Detection

**[4.386]** "The receiver SHALL detect and mark duplicate messages that it receives using the standard authentication and replay protection mechanisms of the secure message layer"

**[4.386]** "The receiver SHALL send an acknowledgment message to the sender for each instance of an authenticated, reliable message, including duplicates."

### 4.12.3 — Peer Exchange Management

**[4.387]** "MRP SHALL support one pending acknowledgement and one pending retransmission per Exchange."

### 4.12.4 — Transport Considerations

**[4.389]** "When the upper layer requests a reliable message over a UDP transport, the R Flag SHALL be set on that message indicating that MRP SHALL be used."

**[4.389]** "Reliable messages sent over TCP, PAFTP, or BTP SHALL utilize the underlying reliability mechanisms of those transports and SHOULD NOT set the R Flag."

**[4.389]** "Reliable messages sent over NTL SHALL utilize the underlying reliability mechanisms and SHALL not set the R Flag."

### 4.12.5.1 — Outgoing Message Processing

*(R Flag / reliable over UDP)*  
"the R Flag SHALL be set on the given message to request an acknowledgement from the peer upon receipt."

"Any message flagged for reliable delivery (R Flag set) SHALL be stored in the retransmission table to track the message until it has been successfully acknowledged by the peer."

"The same Session ID, Destination Node ID, Security Flags, and transport as were used for the initial message transmission SHALL be used." *(retransmission)*

*(Piggyback)*  
"If there is a matching pending acknowledgement, the A Flag SHALL be set on the outbound message so it will serve as a piggybacked acknowledgement."

"For such a piggybacked acknowledgement, the Acknowledgment Message Counter field SHALL be set to the message counter of the received message for which an acknowledgement was pending."

### 4.12.5.2.2 — Standalone Acknowledgement Processing

"If the message is marked as a duplicate: Immediately send a standalone acknowledgment. If the Exchange is marked as an ephemeral exchange the Exchange SHALL be closed."

"if the acknowledgement timer fires … a standalone acknowledgment SHALL be sent to the source of the message."

"If a pending acknowledgement already exists for the Exchange, and it has StandaloneAckSent set to false, a standalone acknowledgment SHALL be sent immediately for that pending message counter"

### 4.12.6.2 — Acknowledgement Table

**[4.397]** "An entry SHALL remain in the table until one of the following things happens: The exchange associated with the entry is closed. … The exchange associated with the entry has switched to track a pending acknowledgement for a new message counter value. … A message that is not a standalone acknowledgement is sent which serves as an acknowledgement for the entry."

### 4.12.7.1 — MRP Standalone Acknowledgement

**[4.398]** "The MRP Standalone Acknowledgement message SHALL be formed as follows: The application payload SHALL be empty. The A Flag SHALL be set to 1. The Acknowledged Message Counter SHALL be included in the header. The Protocol ID SHALL be set to PROTOCOL_ID_SECURE_CHANNEL. The Protocol Opcode SHALL be set to MRP Standalone Acknowledgement."

### 4.12.8 — MRP Parameters

**[4.400]** "A Node SHALL use the provided default value for each parameter unless the message recipient Node advertises an alternate value for the parameter via Operational Discovery."

---

## 4. Message Formats & Data Structures

### Exchange Flags Fields (MRP)

| Field | Flag | Description |
|---|---|---|
| R Flag | Reliable message indicator | Set by sender to request acknowledgement |
| A Flag | Acknowledgement indicator | Set when message carries an acknowledgement; Acknowledged Message Counter field SHALL be present and valid when set |
| I Flag | Initiator indicator | Set on every message sent by the Initiator role |
| Acknowledged Message Counter | Header field | SHALL be set to the Message Counter of the message being acknowledged |

### Secure Channel Status Report Protocol Codes

| Protocol Code | Error | General Code | Encrypted | Additional Data | Description |
|---|---|---|---|---|---|
| 0x0000 | SESSION_ESTABLISHMENT_SUCCESS | SUCCESS | N | N | Last session establishment message successfully processed |
| 0x0001 | NO_SHARED_TRUST_ROOTS | FAILURE | N | N | Failure to find a common set of shared roots |
| 0x0002 | INVALID_PARAMETER | FAILURE | N | N | Generic failure during session establishment |
| 0x0003 | CLOSE_SESSION | SUCCESS | Y | N | Sender will close the current session |
| 0x0004 | BUSY | BUSY | N | Y | Sender cannot currently fulfill the request |

### Busy StatusReport ProtocolData

- 16-bit (two byte) little-endian value indicating minimum milliseconds to wait before retry.
- Example: 500 ms → `[0xF4, 0x01]`

### MRP Standalone Acknowledgement Message

- Application payload: empty
- A Flag: 1
- Acknowledged Message Counter: present in header
- Protocol ID: `PROTOCOL_ID_SECURE_CHANNEL`
- Protocol Opcode: `MRP Standalone Acknowledgement`

### Retransmission Table Record Fields

- Reference to Exchange Context
- Message Counter
- Reference to fully formed, encoded and encrypted message buffer
- Send count
- Retransmission timeout counter

### Acknowledgement Table Record Fields

- Reference to Exchange Context
- Message Counter
- `StandaloneAckSent` (boolean, initially false)

### MRP Parameters (defaults)

| Parameter | Default |
|---|---|
| MRP_MAX_TRANSMISSIONS | 5 |
| MRP_BACKOFF_BASE | 1.6 |
| MRP_BACKOFF_JITTER | 0.25 |
| MRP_BACKOFF_MARGIN | 1.1 |
| MRP_BACKOFF_THRESHOLD | 1 |
| MRP_STANDALONE_ACK_TIMEOUT | 200 milliseconds |

### Secure Channel Constants

| Constant | Value | Description |
|---|---|---|
| MSG_COUNTER_WINDOW_SIZE | 32 | Max previously processed message counters to accept per node/key |
| MSG_COUNTER_SYNC_REQ_JITTER | 500 ms | Max random delay before sending MsgCounterSyncReq triggered by multicast |
| MSG_COUNTER_SYNC_TIMEOUT | 400 ms | Max wait for MsgCounterSyncRsp after sending MsgCounterSyncReq |

### Retransmission Timing (default parameters)

| Metric | Tx1 [ms] | Tx2 [ms] | Tx3 [ms] | Tx4 [ms] | Tx5 [ms] |
|---|---|---|---|---|---|
| Min Jitter | 330 | 330 | 528 | 845 | 1352 |
| Max Jitter | 413 | 413 | 660 | 1056 | 1690 |
| Min Total | 330 | 660 | 1188 | 2033 | 3385 |
| Max Total | 413 | 825 | 1485 | 2541 | 4231 |

---

## 5. Security Considerations

**Session binding:** An Exchange SHALL be bound to exactly one underlying session for its entire lifetime; session types permitted are secure unicast (PASE/CASE), unsecured (initial session establishment phase), secure group, or MCSP.

**CloseSession encryption:** The CloseSession StatusReport SHALL only be sent encrypted within an exchange associated with a PASE or CASE session.

**Secure Channel Status Reports:** Messages marked as "encrypted" in the status code table SHALL only be sent encrypted in a session established with CASE or PASE.

**Session state removal:** A node that has either sent or received a CloseSession StatusReport SHALL remove all state associated with the session. The node MAY save state necessary to perform Session Resumption.

**Busy response — replay prevention:** After receiving a BUSY StatusReport, if the initiator sends a new session establishment request, that request SHALL contain new values for all randomized parameters.

**Exchange ID space exhaustion:** A node SHOULD limit itself to a maximum of 5 concurrent Initiator exchanges over a unicast session to prevent exhausting the Secure Session Message Counter window of the session with the peer node.

**Duplicate detection:** The receiver SHALL detect and mark duplicate messages using the standard authentication and replay protection mechanisms of the secure message layer. Duplicate reliable messages still receive an acknowledgement, but SHALL be dropped by the reliability layer before delivery to the upper layer.

**Group session reliability flags:** If the R Flag or A Flag is set on a Group Session Type message with C Flag = 0, the message SHALL be dropped.

---

## 6. Error Handling & Timing

### Retransmission Behavior

- Reliable messages are transmitted at most **MRP_MAX_TRANSMISSIONS** (default 5) times.
- Between attempts, the sender waits `mrpBackoffTime` milliseconds, calculated using MRP_BACKOFF_BASE, MRP_BACKOFF_JITTER, MRP_BACKOFF_MARGIN, and MRP_BACKOFF_THRESHOLD.
- A two-phase scheme is used: linear backoff up to MRP_BACKOFF_THRESHOLD retransmissions, then exponential backoff.
- After MRP_MAX_TRANSMISSIONS attempts with no acknowledgement, the message is evicted from the retransmission table and the application is notified of failure.
- If a transport error is **fatal**, the application is notified immediately and the entry is removed from the retransmission table. For non-fatal errors (e.g., no memory), the send is retried per the normal schedule.

### Backoff Base Interval Selection

- First message of a new Exchange: base interval `i` SHALL be set per the idle state of the peer node (SESSION_IDLE_INTERVAL).
- All subsequent messages of the Exchange: `i` SHOULD be set per the active state of the peer (SESSION_ACTIVE_INTERVAL) unless the sender has other means to determine active/idle.
- Final base interval: `i = MRP_BACKOFF_MARGIN * i` (at least 10% greater than the peer idle interval).

### ICD Sender Obligation

- When communicating with an ICD (ICD Management cluster present), the sender SHALL initiate Operational Discovery in parallel with the first retry if the initial transmission fails after one expected round trip.
- The sender SHALL use the latest MRP parameters from subsequent Operational Discovery.

### Acknowledgement Timeout

- Receiver SHOULD wait no longer than **MRP_STANDALONE_ACK_TIMEOUT** (default 200 ms) before sending a standalone acknowledgement.
- If the timer fires without cancellation, a standalone acknowledgement SHALL be sent; the acknowledgement table entry is retained with `StandaloneAckSent = true`.
- On Exchange close, any pending acknowledgement with `StandaloneAckSent = false` SHALL be flushed immediately.

### Busy / Retry Timing

- The BUSY StatusReport ProtocolData carries the minimum retry delay in milliseconds (16-bit little-endian).
- The initiator SHALL wait at least that many milliseconds (`t`) before retrying.
- BUSY SHALL NOT be sent in response to any message other than Sigma1 or PBKDFParamRequest.

### Message Counter Sync Timing

- MsgCounterSyncReq triggered by a multicast message: sender waits a random delay up to **MSG_COUNTER_SYNC_REQ_JITTER** (500 ms) before sending.
- Node waits at most **MSG_COUNTER_SYNC_TIMEOUT** (400 ms) for a MsgCounterSyncRsp after sending a MsgCounterSyncReq.

### Acknowledgement Table Eviction

An entry is removed from the acknowledgement table when: (a) the associated exchange is closed, (b) the exchange moves to track a new pending acknowledgement, or (c) a non-standalone-acknowledgement outbound message serves as a piggybacked acknowledgement for the entry.