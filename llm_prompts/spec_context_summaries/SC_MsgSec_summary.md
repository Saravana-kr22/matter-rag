# Matter Spec Summary: SC Message Security

**Source sections matched:** 46  
**Source chars sent to LLM:** 40,911  
**Generated:** 2026-04-28 19:00:18  
**Summary words:** 3,893  

---

## 1. Overview

This section describes the Matter message security, privacy, and framing mechanisms that govern how messages are encoded, encrypted, authenticated, and processed across all Matter communication modes (unicast secure sessions, multicast group messaging, and session establishment). The core model is symmetric key encryption shared between communicating parties, with unencrypted messages used only for bootstrapping (e.g., session establishment). All multi-byte integer fields are transmitted in little-endian byte order unless otherwise noted.

Key concepts:
- **Session ID** identifies the key set and algorithm used for a message.
- **Message Counter** serves as a monotonically increasing sequence number used for duplicate detection, MRP acknowledgement, and as an encryption nonce.
- **Privacy Processing** obfuscates certain header fields after encryption to limit traffic analysis.
- **AEAD** (Authenticated Encryption with Additional Data) is the cryptographic primitive used for confidentiality and integrity.

---

## 2. Protocol Flow & State Machine

### Outgoing Message Transmission (§4.7.1)

Steps SHALL be performed in order:
1. Perform counter processing (§4.6.6).
2. If Session Type is Unicast Session:
   - Set `SessionTimestamp`.
   - Perform security processing (§4.8.2).
   - Perform privacy processing (§4.9.3).

### Incoming Message Reception (§4.7.2)

Steps SHALL be performed in order:
1. **Validity checks** — if any fail, stop processing; 'message invalid' error SHOULD be indicated upward:
   - Version field SHALL be 0.
   - For Secure Unicast Session: DSIZ field SHALL NOT indicate a Group ID.
   - For Group Session: DSIZ SHALL NOT be 0; S Flag SHALL NOT be 0.
2. If NOT Unsecured Session:
   - Obtain Privacy and Encryption Keys for the Session ID.
   - If no keys found → fail with 'message security failed'; no further processing.
   - For each Privacy/Encryption Key (may be multiple for group):
     - If P Flag set → privacy deobfuscation (§4.9.4).
     - Security decryption/authentication (§4.8.3).
3. Counter processing for replay protection and duplicate detection (§4.6.7).
4. If UDP transport → MRP reliability processing (§4.12.5.2).
5. If Unicast Session → set `SessionTimestamp` and `ActiveTimestamp`.
6. Deliver to Exchange Message Processing (§4.10.5).

### Security Processing of Outgoing Messages (§4.8.2)

The Node SHALL perform the following steps:
1. Obtain Encryption Key for Session ID + Destination Node ID; if none found → fail.
2. Obtain outgoing message counter (§4.6.6).
3. Set Security fields (Session ID, Security Flags, Session Type).
4. Set Message Flags, Destination Node ID, Source Node ID:
   - Unicast: S Flag = 0, DSIZ = 0, omit both Node IDs.
   - Group: S Flag = 1, DSIZ = 2, set Source Node ID = sender's operational node ID, set Destination Node ID = 16-bit Group ID.
5. Set Message Counter to value from step 2.
6. Execute AEAD generate-and-encrypt:
   - Key K = Encryption Key from step 1.
   - Nonce N = 13-byte nonce per Table 17 (Security Flags || Message Counter || Source Node ID).
   - Plaintext P = Message Payload.
   - Additional data A = Message Header: `Message Flags || Session ID || Security Flags || Message Counter [|| Source Node ID] [|| Destination Node ID] [|| Message Extensions]`.
   - `C = Crypto_AEAD_GenerateEncrypt(K, P, A, N)`.
7. If AEAD error → security processing fails; no further processing.
8. Secured outgoing message = `A || C` (C contains payload ciphertext + MIC).

### Security Processing of Incoming Messages (§4.8.3)

All incoming message processing SHALL occur in a serialized manner. Steps:
1. Determine Session Type, Session ID, Message Counter from message header.
2. Obtain Encryption Key for Session ID/Type; if none → fail with 'message security failed'.
3. Execute AEAD decrypt-and-verify:
   - `{success, P} = Crypto_AEAD_DecryptVerify(K, C, A, N)`.
4. If success = FALSE → fail; no further processing; appropriate error SHOULD be raised to upper layer.
5. Otherwise, set `PlaintextMessage = A || P`; mark as successfully security processed; release to next layer. (Note: counter processing / replay detection has NOT yet occurred at this point.)

### Privacy Processing of Outgoing Messages (§4.9.3)

1. If P Flag not set → do nothing.
2. Obtain Privacy Key for the Encryption Key.
3. Execute privacy encryption:
   - Key K = Privacy Key.
   - MIC = last `CRYPTO_AEAD_MIC_LENGTH_BYTES` bytes of C from security processing.
   - Nonce N = PrivacyNonce (derived from Session ID and MIC).
   - M = `Message Counter || [Source ID] || [Destination ID]`.
   - `CP = Crypto_Privacy_Encrypt(K, M, N)`.
4. CP replaces the corresponding message header fields in the final message.

### Privacy Processing of Incoming Messages (§4.9.4)

1. If P Flag not set → do nothing.
2. Execute privacy decryption with Privacy Key, MIC, PrivacyNonce, and header fields CP.
   - `M = Crypto_Privacy_Decrypt(K, CP, N)`.
3. M replaces the message header fields.
4. The first successfully validated M (per §4.8.3) SHALL terminate iteration through Privacy Keys.

### Stream Framing (§4.5)

For stream-oriented transports (TCP, PAFTP, BTP), each Matter message SHALL be prepended with a Message Length field. This field SHALL NOT be present for datagram channels (UDP, NTL), where message length is conveyed by the underlying channel.

---

## 3. Normative Requirements

### §4.4.1.1 — Message Flags

- [4.226] "All unused bits in the Message Flags field are reserved and SHALL be set to zero on transmission and SHALL be silently ignored on reception."
- [4.228] "Messages with version field set to reserved values SHALL be dropped without sending a message-layer acknowledgement."
- [4.230] "A single bit field which SHALL be set if and only if the Source Node ID field is present." (S Flag)
- [4.231] "This field SHALL indicate the size and meaning of the Destination Node ID field." (DSIZ)
- [4.232] "Messages with DSIZ field set to reserved values SHALL be dropped without sending a message-layer acknowledgement."

### §4.4.1.3 — Security Flags

- [4.235] "All unused bits in the Security Flags field are reserved and SHALL be set to zero on transmission and SHALL be silently ignored on reception."
- [4.237] "The Control message (C) flag is a single bit field which, when set, SHALL identify that the message is a control message, such as for the Message Counter Synchronization Protocol, and uses the control message counter for the nonce field as specified in Section 4.8.1.1."
- [4.238] "The Message Extensions (MX) flag is a single bit field which, when set, SHALL indicate that the Message Extensions portion of the message is present and has non-zero length. Version 1.0 Nodes SHALL set this flag to zero."
- [4.236] "The Privacy (P) flag is a single bit field which, when set, SHALL identify that the message is encoded with privacy enhancements as specified in Section 4.9.3."
- [4.240] "Messages with Session Type set to reserved values are not valid and SHALL be dropped without sending a message-layer acknowledgement."
- [4.242] "The Unsecured Session SHALL be indicated when both Session Type and Session ID are set to 0. The Unsecured Session SHALL have no encryption, privacy, or message integrity checking."

### §4.4.1.7 — Message Extensions

- [4.249] "If the MX Flag is set to 1, the Message Extensions Payload Length field SHALL be present and SHALL contain the length of the Message Extensions Payload. The Message Extensions Payload Length field SHALL NOT be privacy obfuscated."
- [4.250] "Version 1.0 Nodes SHALL ignore the contents of the Message Extensions payload, by skipping it, to access the Message Payload."

### §4.4.2.1 — Message Integrity Check

- [4.252] "The Message Integrity Check field SHALL be present for all messages except those of Unsecured Session Type."

### §4.4.3.1 — Exchange Flags

- [4.255] "All unused bits in the Exchange Flags field are reserved and SHALL be set to zero on transmission and SHALL be silently ignored on reception."
- [4.256] "The Initiator (I) flag is a single bit field which, when set, SHALL indicate that the message was sent by the initiator of the exchange."
- [4.257] "The Acknowledgement (A) flag is a single bit field which, when set, SHALL indicate that the message serves as an acknowledgement of a previous message received by the current message sender."
- [4.258] "The Reliability (R) flag is a single bit field which, when set, SHALL indicate that the message sender wishes to receive an acknowledgement for the message."
- [4.259] "The Secured Extensions (SX) flag is a single bit field which, when set, SHALL indicate that the Secured Extensions portion of the message is present and has non-zero length. Version 1.0 Nodes SHALL set this flag to zero."
- [4.260] "The Vendor (V) protocol flag is a single bit field which, when set, SHALL indicate whether the Protocol Vendor ID is present."

### §4.5 — Stream Framing

- [4.273] "When Matter messages are transferred over stream-oriented transport protocols, such as TCP, PAFTP, or BTP, they need to be framed appropriately to enable the receiver to read each message from the stream. To allow that, each Matter Message SHALL be prepended with a Message Length field. This field SHALL only be present when the message is being transmitted over a stream-oriented channel."
- [4.224] "The Message Payload of a Matter message SHALL contain a Protocol Message with format as follows: [Exchange Flags, Protocol Opcode, Exchange ID, ...]"

### §4.6 — Message Counters

- [4.280] "All message counters SHALL be initialized with a random value using the Crypto_DRBG(len = 28) + 1 primitive."
- [4.281] "All Nodes SHALL implement an unencrypted message counter, which is used to generate counters for unencrypted messages."
- [4.282] "In the event that the Global Unencrypted Message Counter for a Node is lost, Nodes SHALL randomize the initial value of this counter on startup per Section 4.6.1.1."
- [4.284] "Nodes are required to persist the Global Group Encrypted Message Counters in durable storage. In particular, Nodes are required to ensure that the value of the Global Group Encrypted Message Counters never rolls back and that it is monotonic within the bounds of its range for its use for a given group key. A Node SHALL randomize the initial value of this counter on factory reset per Section 4.6.1.1."
- [4.286] "Each peer in a Secure Unicast Session SHALL maintain its own message counters, with independent counters being used for each unique session used. Session Message Counters SHALL exist for as long as the associated security session is in effect. A Node SHALL randomize the initial value of this counter on session establishment per Section 4.6.1.1."
- [4.287] "The Secure Session Message Counter history window SHALL be maintained for the lifetime of the session, and SHALL be deleted at the same time as the session keys, when the session ends."
- [4.288] "Sessions SHALL be discarded and re-established before any Secure Session Message Counter overflow or repetition occurs."
- [4.290] "The device SHALL randomize the initial value of the counter on factory reset per Section 4.6.1.1."
- [4.291] "The Check-In Counter SHALL be monotonically increased each time a Check-In message is sent. This monotonicity guarantee SHALL be preserved across idle and active states."
- [4.294] "In the event that a Global Group Encrypted Message Counter wraps before the associated keys are rotated, all keys associated with that Global Group Encrypted Message Counter are considered exhausted and are no longer valid to use. In such cases, a new unicast session SHALL be established to the Matter Node to rotate such retired keys before secure communication can resume."

### §4.7 — Message Processing

- [4.308] "To prepare a message for transmission with a given Session ID, Destination Node ID ... and Security Flags, the following steps SHALL be performed, in order: [counter processing, security processing, privacy processing]"
- [4.309] "To process a received message, the following steps SHALL be performed in order: [validity checks, security processing, counter processing, MRP processing, exchange delivery]"

### §4.8.1 — AEAD Parameters

- [4.312] "The parameters in this section SHALL apply for all encrypted messages, i.e. all messages except those of Unsecured Session Type."
- [4.313] "The nonce SHALL be formatted as specified in Table 17, 'Nonce'." (Security Flags | Message Counter | Source Node ID)
- [4.315, Group session] "The S Flag of the message SHALL be 1 and the Nonce Source Node ID SHALL be the Source Node ID of the message. If the S Flag of the message is 0 the message SHALL be dropped."

### §4.8.2 — Outgoing Security Processing

- "If no key is found for the given Session ID, security processing SHALL fail and no further security processing SHALL be done on this message."
- "The Session ID field SHALL be set to the value provided to step 1."
- "The Session Type field SHALL be set to the value obtained from step 1."
- "If the AEAD operation invoked in step 6 results in an error, then security processing SHALL fail and no further security processing SHALL be done on this message."

### §4.8.3 — Incoming Security Processing

- [4.319] "All incoming message processing SHALL occur in a serialized manner. If an implementation chooses to process messages in a parallel manner, it must ensure that the behavior is opaque-box identical to a serialized processing implementation."
- "If no key is found for the given Session ID, security processing SHALL indicate a failure to the next higher layer with a status of 'message security failed' and no further security processing SHALL be done on this message."
- "If the success is FALSE, security processing SHALL fail and further processing SHALL NOT be performed on this message. An appropriate error SHOULD be raised to the upper layer to indicate the error."
- "The PlaintextMessage SHALL be marked as successfully security processed and SHALL be released to the next processing layer."

### §4.9.2 — Privacy Nonce

- [4.324] "The Privacy Nonce SHALL be the CRYPTO_AEAD_NONCE_LENGTH_BYTES-octet string constructed as the 16-bit Session ID (in big-endian format) concatenated with the lower 11 (i.e. CRYPTO_AEAD_MIC_LENGTH_BYTES-5) bytes of the MIC."

### §4.9.4 — Incoming Privacy Processing

- "M SHALL be used in the final private message in place of the message header fields. The first successfully validated message, M, by Section 4.8.3, 'Security Processing of Incoming Messages' SHALL terminate iteration through Privacy Keys in step 2."

### §4.6.4 — Nonce Rotation

- [4.294] "Nodes SHOULD rotate their encryption keys on a regular basis, to ensure that old encryption keys are retired before a Global Group Encrypted Message Counter has a chance to wrap to a value previously used with the encryption key. ... Given the importance of confidentiality and message integrity, every effort SHOULD be made to ensure that keys are rotated on a regular basis."

### §4.6.3 — Check-In Counter

- [4.290] "Each Check-In Protocol use-case implemented by a device SHALL be associated with a specific Check-In counter to be used for each node identity (pair of fabric and node identifier). This MAY be a single counter across all node identities, or MAY be one counter per node identity, or anything in between, as long as each node identity uses a single specific instance of the counter."
- [4.284] (Group counters) "Some Nodes might not be required to implement communication using group keys, in which case they MAY omit the Global Group Encrypted Message Counters."

---

## 4. Message Formats & Data Structures

### Matter Message Structure (§4.4)

```
Message Header
  1 byte   | Message Flags
  2 bytes  | Session ID
  1 byte   | Security Flags
  4 bytes  | Message Counter
  0/8 bytes| [ Source Node ID ]
  0/2/8 bytes | [ Destination Node ID ]
  variable | [ Message Extensions ]
Message Payload
  variable | [ Message Payload ] (encrypted)
Message Footer
  variable | [ Message Integrity Check ]
```

### Protocol Message (within Message Payload) (§4.224)

```
Protocol Header
  1 byte   | Exchange Flags
  1 byte   | Protocol Opcode
  2 bytes  | Exchange ID
  2 bytes  | [ Protocol Vendor ID ]
  2 bytes  | Protocol ID
  4 bytes  | [ Acknowledged Message Counter ]
  variable | [ Secured Extensions ]
Application Payload
  variable | [ Application Payload ]
```

### Message Flags (8-bit) (§4.4.1.1)

```
bit 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0
      Version      | - | S | DSIZ
```
- **Version** (bits 4–7): 0 = Matter Message Format version 1.0; 1–15 = Reserved (drop on receipt).
- **S** (bit 2): Source Node ID present when set.
- **DSIZ** (bits 0–1): 0 = no Destination Node ID; 1 = 64-bit Node ID; 2 = 16-bit Group ID; 3 = Reserved (drop on receipt).

### Security Flags (8-bit) (§4.4.1.3)

```
bit 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0
  P   | C | MX | Reserved | Session Type
```
- **P** (bit 7): Privacy encoding applied.
- **C** (bit 6): Control message; uses control message counter for nonce.
- **MX** (bit 5): Message Extensions present; Version 1.0 nodes SHALL set to 0.
- **Session Type** (bits 0–1): 0 = Unicast Session; 1 = Group Session; 2–3 = Reserved (drop on receipt).

### Exchange Flags (8-bit) (§4.4.3.1)

```
bit 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0
  -   | - | - | V | SX | R | A | I
```
- **I** (bit 0): Initiator flag.
- **A** (bit 1): Acknowledgement flag.
- **R** (bit 2): Reliability (sender requests ACK).
- **SX** (bit 3): Secured Extensions present; Version 1.0 SHALL set to 0.
- **V** (bit 4): Protocol Vendor ID present.

### Message Extensions Block (§4.4.1.7)

```
2 bytes  | Message Extensions Payload Length (in bytes)
variable | [ Message Extensions Payload ]
```
Present only when MX Flag = 1.

### Message Length Field for Stream Transports (§4.5.1)

| Protocol | Size    | Description |
|----------|---------|-------------|
| TCP      | 4 bytes | Before Message Header of each Matter message |
| BTP      | 2 bytes | Before segment payload in beginning BTP PDU |
| PAFTP    | 2 bytes | Before segment payload in beginning PAFTP PDU |
| MRP      | N/A     | Not present |
| NTL      | N/A     | Not present |

### AEAD Nonce (13 bytes) (§4.8.1.1 / Table 17)

```
Octets:  1              | 4               | 8
         Security Flags | Message Counter | Source Node ID
```
All scalar fields encoded in little-endian byte order.

### AEAD Additional Data A (§4.8.2)

`Message Flags || Session ID || Security Flags || Message Counter [|| Source Node ID] [|| Destination Node ID] [|| Message Extensions]`

### Privacy Nonce (§4.9.2)

16-bit Session ID (big-endian) || lower 11 bytes of MIC.

### Message Counter Types (§4.6.1 / Table 16)

| Counter Type              | Session Type | Lifetime            | Rollover Policy | Nonvolatile |
|---------------------------|-------------|---------------------|-----------------|-------------|
| Global Unencrypted        | Unsecured   | Unlimited           | Allowed         | Optional    |
| Global Encrypted Data     | Group       | Operational Group Key | Allowed       | Mandatory   |
| Global Encrypted Control  | Group       | Operational Group Key | Allowed       | Mandatory   |
| Secure Session            | Unicast     | Session Key         | Expires         | Optional    |
| Check-In Counter          | Unsecured   | Unlimited           | Allowed         | Mandatory   |

---

## 5. Security Considerations

### Nonce Uniqueness and Confidentiality (§4.6.4)

The uniqueness of an encrypted message's counter is vital to confidentiality: if two messages share the same key and nonce, an attacker can XOR them to derive a "block key" usable to decrypt any message with that key and nonce. Nodes SHOULD rotate encryption keys regularly to prevent counter reuse before a Global Group Encrypted Message Counter wraps. If a counter wraps before key rotation, all keys associated with that counter are considered exhausted and no longer valid; a new unicast session SHALL be established to rotate them before secure communication can resume.

### Counter Initialization (§4.6.1.1)

All message counters SHALL be initialized with a random value using `Crypto_DRBG(len = 28) + 1`, ranging from 1 to 2^28, to increase difficulty of traffic analysis by preventing an observer from determining session age.

### Group Message Counter Monotonicity (§4.6.1.3)

Nodes SHALL ensure the Global Group Encrypted Message Counters never roll back and remain monotonic for a given group key. These counters SHALL be persisted in durable storage.

### Unsecured Session Limitations (§4.4.1.3)

The Unsecured Session (Session Type = 0 AND Session ID = 0) SHALL have no encryption, privacy, or message integrity checking.

### PASE Nonce Security (§4.8.1.1 Note)

Because PASE negotiates strong one-time keys per session and uses distinct I2RKey and R2IKey for each communication direction, the use of Unspecified Node ID as the Nonce Source Node ID remains semantically secure.

### Privacy Obfuscation (§4.9.1–4.9.3)

The Privacy Key is derived from the Encryption Key. Privacy processing obfuscates the Message Counter, Source Node ID, and Destination Node ID header fields after encryption using a separate nonce derived from the Session ID and MIC. The Message Extensions Payload Length field SHALL NOT be privacy obfuscated (§4.249).

### Session Counter Window Deletion (§4.6.2)

The Secure Session Message Counter history window SHALL be deleted at the same time as the session keys when the session ends.

### Check-In Counter Key Refresh (§4.6.3 Note)

The Check-In Counter has an unlimited lifetime until factory reset. To ensure nonce non-reuse (since the nonce is derived from the counter and the ICD key), the key needs to be refreshed before exhausting all valid counter values.

---

## 6. Error Handling & Timing

### Message Validity — Drop Without ACK

The following conditions require silently dropping the message **without sending a message-layer acknowledgement**:
- Version field is set to a reserved value (1–15). [4.228]
- DSIZ field is set to the reserved value (3). [4.232]
- Session Type field is set to a reserved value (2–3). [4.240]
- Group session: S Flag = 0 (Source Node ID absent when Source Node ID is required for nonce). [4.315]

### Security Processing Failures

- If no Encryption Key is found for the given Session ID → security processing SHALL fail; no further security processing SHALL be done; failure SHALL be indicated to the next higher layer with 'message security failed'. [§4.8.2, §4.8.3]
- If AEAD operation on outgoing message returns an error → security processing SHALL fail; no further processing. [§4.8.2]
- If AEAD DecryptVerify returns success = FALSE → security processing SHALL fail; further processing SHALL NOT be performed; an appropriate error SHOULD be raised to the upper layer. [§4.8.3]

### Message Reception Validity Failures

If any validity check fails during §4.7.2 reception processing, processing SHALL stop and a 'message invalid' error SHOULD be indicated to the next higher layer:
- Version field ≠ 0.
- Secure Unicast Session with DSIZ indicating a Group ID.
- Group Session with DSIZ = 0.
- Group Session with S Flag = 0.

### Serialization Constraint (§4.8.3)

All incoming message processing SHALL occur in a serialized manner. Parallel implementations must be opaque-box identical to a serialized implementation.

### Secure Session Counter Overflow (§4.6.2)

Sessions SHALL be discarded and re-established before any Secure Session Message Counter overflow or repetition occurs. [4.288]

### Group Counter Exhaustion (§4.6.4)

If a Global Group Encrypted Message Counter wraps before key rotation, all associated keys are exhausted. A new unicast session SHALL be established to rotate those keys before secure communication can resume. [4.294]

### Check-In Counter NVM Write Strategy (§4.6.3)

To reduce non-volatile storage writes, a node may write `counter + N` (e.g., N = 1000) to storage at startup rather than every increment. The counter MAY increase by more than 1 on reboot. The Check-In Counter MAY roll over to zero when it exceeds 32-bit maximum; rollover is permitted.