# Matter Spec Summary: SC TCP

**Source sections matched:** 6  
**Source chars sent to LLM:** 6,364  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 1,151  

---

## 1. Overview

This section covers Matter's use of TCP as an alternative transport protocol for secure communications, specifically to handle large messages that cannot fit within the IPv6 MTU limit of 1280 bytes required by MRP (Matter Reliable Protocol). TCP support is advertised via the TXT record key `T` in DNS-SD operational service records. The section addresses session establishment over TCP, connection management (configuration, closure, re-establishment), and memory requirements for large messages.

---

## 2. Protocol Flow & State Machine

**Session Establishment (§4.15.1)**

- When two nodes that both support TCP intend to establish a secure session, they MAY choose TCP as the underlying transport.
- A node SHOULD typically have either a secure session over MRP or one over TCP with a given peer, but MAY also have both simultaneously (e.g., MRP for general interactions, TCP for data-heavy operations such as OTA).
- The underlying TCP connection MAY be long-lived or short-lived depending on use case.
- A secure session over TCP becomes unusable when its TCP connection is broken or closed.
- Nodes MAY remove the secure session when the connection goes down.
- If the session is retained after the connection goes away, it SHALL be marked appropriately so the underlying connection is re-established before the session can be used again.
- Session resumption state MAY be retained to expedite re-establishment with the same peer.

**Connection Closure & Re-establishment (§4.15.2.2)**

- Either side MAY close the connection, including proactively when instructed by the upper layer or based on current state (e.g., Keep Alive timeout expiry, TCP User Timeout expiry on unacknowledged data).
- When the TCP layer is notified that the peer has closed the connection, the node SHALL close its own end and notify the application; all active Exchanges over that connection SHOULD also be closed.
- Either side MAY choose to re-establish the connection when it is closed or broken.
- A node SHOULD back off (typically based on a Fibonacci back-off sequence) a random amount of time after closure before attempting to reconnect.
- Upon receipt of a connection request from a peer, a node SHOULD discard its own backed-off connection retry timer to that same peer, if one is active.

---

## 3. Normative Requirements

### §4.15 (Secure Communications over TCP)

> [4.518] "a node that is using TCP as the underlying transport protocol SHALL NOT use MRP reliability semantics on its message exchanges."

### §4.15.1 (Secure Session Establishment)

> [4.519] "When a pair of nodes that both support TCP intend to establish a secure session between themselves, they MAY choose TCP as the underlying transport protocol."

> [4.519] "With a given peer, a node SHOULD, typically, either have a secure session over MRP or one over TCP, but MAY also have both."

> [4.520] "The underlying TCP connection MAY be long-lived or short-lived depending on the use case."

> [4.520] "Nodes MAY choose to remove the secure session when the connection goes down."

> [4.520] "If the session is retained after the connection goes away, then the session SHALL be marked appropriately so that the underlying connection is re-established before the session can be used again."

> [4.520] "Moreover, the session resumption state MAY be retained to expedite session establishment when the connection is re-established with the corresponding peer."

### §4.15.2.1 (TCP Connection Configuration)

> [4.522] "The configurable parameters SHALL be: TCP_KEEP_ALIVE_TIME : The interval between the last data packet sent and the first keep-alive probe. TCP_KEEP_ALIVE_INTERVAL : The interval between subsequent keep-alive probes. TCP_KEEP_ALIVE_PROBES : The number of unacknowledged probes to send before considering the connection dead and notifying the application."

### §4.15.2.2 (TCP Connection Closures And Re-establishment)

> [4.526] "When the TCP layer of a node gets notified that the peer has closed the connection, it SHALL close its end of the connection as well, and notify the application."

> [4.526] "Subsequently, all active Exchanges over that connection SHOULD also be closed as they would be unusable over a closed connection."

> [4.527] "A node SHOULD back-off (typically, based on a Fibonacci back-off sequence) a random amount of time after the connection closure, before attempting to establish the connection again."

> [4.528] "A node, upon receipt of a connection request from a peer, SHOULD discard its own backed-off connection retry timer to the same peer, if one is active."

> [4.529] "This random back-off mechanism SHOULD prevent connection races between peers for most, if not all scenarios. In addition, nodes SHOULD try to reap old unused connections as much as possible to conserve resources."

### §4.15.2.3 (Memory Requirements for Large Messages)

> [4.530] "If a node receives a message header that indicates that the message is larger than the Maximum Message Size that it supports, then it SHALL close the connection, and SHOULD send a Status Report error message with a status code set to MESSAGE_TOO_LARGE back to the sender, before closing the connection."

> [4.531] "the system SHOULD be able to dynamically change this configuration based on what transport the current Exchange is using."

---

## 4. Message Formats & Data Structures

**Status/Error Code:**

- `MESSAGE_TOO_LARGE` — status code sent in a Status Report error message when a received message header indicates the message exceeds the node's configured Maximum Message Size.

**TCP Keep Alive Parameters (§4.15.2.1):**

- `TCP_KEEP_ALIVE_TIME`: interval between the last data packet sent and the first keep-alive probe.
- `TCP_KEEP_ALIVE_INTERVAL`: interval between subsequent keep-alive probes.
- `TCP_KEEP_ALIVE_PROBES`: number of unacknowledged probes before considering the connection dead.

**DNS-SD Advertisement:**

- TXT record key `T` is used by nodes to communicate TCP support in their DNS-SD operational service records.

---

## 5. Security Considerations

(Not covered in provided spec sections.)

---

## 6. Error Handling & Timing

**Connection Establishment Timeout:**
- A node MAY configure the amount of time it waits for a connection establishment attempt to succeed before giving up and notifying the application.

**TCP Keep Alive Timeout:**
- A node MAY close the connection when the TCP Keep Alive Timeout expires on an idle connection.
- The total TCP Keep Alive Timeout is given by a formula referenced in §4.523, but the formula itself is not included in the provided spec text.

**TCP User Timeout:**
- Specifies the amount of time that transmitted data may remain unacknowledged before the TCP connection is forcibly closed.
- A node MAY close the connection when sent data is not acknowledged and the configured TCP User Timeout expires.

**Maximum Message Size Violation:**
- If a node receives a message header indicating the message exceeds its Maximum Message Size, it SHALL close the connection and SHOULD send a Status Report with status code `MESSAGE_TOO_LARGE` before closing.

**Connection Re-establishment Back-off:**
- A node SHOULD back off (typically Fibonacci back-off sequence) a random amount of time after connection closure before attempting reconnection.
- Upon receiving a connection request from a peer, a node SHOULD discard its own active backed-off retry timer to that peer.