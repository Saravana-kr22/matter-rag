# Matter Spec Summary: SC CASE

**Source sections matched:** 28  
**Source chars sent to LLM:** 40,878  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 4,474  

---

## 1. Overview

Certificate Authenticated Session Establishment (CASE) is a protocol for establishing authenticated key exchange between exactly two peers using Node Operational credentials. It maintains privacy of each peer during session establishment. A resumption mechanism allows bootstrapping a new session from a previous one, reducing computation and the number of messages exchanged. Two roles participate: **Initiator** and **Responder**.

Session resumption SHOULD be preferred for low-powered devices when state is known to the initiator, as it avoids expensive signature creation and verification.

---

## 2. Protocol Flow & State Machine

### Standard Session Establishment (New Session)

```
Initiator                          Responder
    |--- Sigma1 ------------------>|
    |                              | (Validate Sigma1 Destination ID)
    |<-- Sigma2 -------------------|
    | (Validate Sigma2)            |
    |--- Sigma3 ------------------>|
    |                              | (Validate Sigma3)
    |<-- SigmaFinished (StatusReport SUCCESS) ---|
```

**Initiator sends Sigma1:**
- Generates `InitiatorRandom = Crypto_DRBG(len = 32 * 8)`
- Generates `InitiatorSessionId` (no overlap with existing PASE/CASE sessions)
- Generates `DestinationId` via HMAC-based Destination Identifier procedure
- Generates `InitiatorEphKeyPair = Crypto_GenerateKeypair()`
- Optionally encodes MRP parameters

**Responder validates Sigma1 and sends Sigma2:**
- Traverses installed NOCs to compute `candidateDestinationId` values, validates incoming `destinationId`
- Generates `ResponderEphKeyPair = Crypto_GenerateKeypair()`
- Computes `SharedSecret = Crypto_ECDH(ResponderEphKeyPair.privateKey, Msg1.initiatorEphPubKey)`
- Signs `sigma-2-tbsdata` with `ResponderNOKeyPair.privateKey`
- Encrypts `sigma-2-tbedata` using S2K key
- Generates `ResponderSessionId` (no overlap with existing PASE/CASE sessions)

**Initiator validates Sigma2 and sends Sigma3:**
- Computes `SharedSecret = Crypto_ECDH(InitiatorEphKeyPair.privateKey, Msg2.responderEphPubKey)`
- Decrypts and verifies `TBEData2` using S2K
- Verifies certificate chain and signature in `TBEData2`
- Signs `sigma-3-tbsdata`, encrypts into `sigma-3-tbedata` using S3K
- Generates session encryption keys

**Responder validates Sigma3 and sends SigmaFinished:**
- Decrypts and verifies `TBEData3` using S3K
- Verifies certificate chain and signature in `TBEData3`
- Generates session encryption keys
- Sends `StatusReport(GeneralCode: SUCCESS, ProtocolId: SECURE_CHANNEL, ProtocolCode: SESSION_ESTABLISHMENT_SUCCESS)`

---

### Session Resumption

**Happy path (resumption succeeds):**
```
Initiator                          Responder
    |--- Sigma1 (w/ resumptionID + initiatorResumeMIC) -->|
    |                              | (Validate Sigma1 with Resumption)
    |<-- Sigma2_Resume ------------|
    | (Validate Sigma2_Resume)     |
    |--- SigmaFinished ----------->|
```

**Fallback path (resumption fails, falls back to new session):**
```
Initiator                          Responder
    |--- Sigma1 (w/ resumptionID + initiatorResumeMIC) -->|
    |                              | (Resume1MIC verify fails → treat as plain Sigma1)
    |<-- Sigma2 ------------------|
    |--- Sigma3 ------------------>|
    |<-- SigmaFinished ------------|
```

**Session Resumption State required at both peers:**
- `SharedSecret`
- Local Fabric Index
- Peer Node ID
- Peer CASE Authenticated Tags
- `ResumptionID`

---

### Message Exchange Groupings

- Standard new session: Sigma1 + Sigma2 + Sigma3 + SigmaFinished belong to the same message exchange.
- Resumption (success): Sigma1 with resumption + Sigma2_Resume + SigmaFinished belong to the same message exchange.
- Resumption (failed, fell back): Sigma1 with resumption + Sigma2 + Sigma3 + SigmaFinished belong to the same message exchange.

---

## 3. Normative Requirements

### 4.14.2.2 — Session Resumption (General)

- [4.466] "Session resumption SHOULD be used by initiators when the necessary state is known to the initiator."
- [4.468] "In the case where a Responder is not able to resume a session as requested by a Sigma1 with Resumption, the information included in the Sigma1 with Resumption message SHALL be processed as a Sigma1 message without any resumption fields to construct a Sigma2 message and continue the standard session establishment protocol without resumption."
- [4.469] "To make the resumption succeed, both the Initiator and the Responder SHALL have remembered the SharedSecret they have computed during the previous execution of the CASE session establishment. It SHALL be that SharedSecret that is used to compute the resumption ID."

### Generate and Send Sigma1

- "The initiator SHALL generate a random number `InitiatorRandom = Crypto_DRBG(len = 32 * 8)`."
- "The initiator SHALL generate a session identifier (`InitiatorSessionId`) for subsequent identification of this session. The `InitiatorSessionId` field SHALL NOT overlap with any other existing PASE or CASE session identifier in use by the initiator."
- "The initiator SHALL generate a destination identifier (`DestinationId`) according to Destination Identifier to enable the responder to properly select a mutual Fabric and trusted root for the secure session."
- "The initiator SHALL generate an ephemeral key pair `InitiatorEphKeyPair = Crypto_GenerateKeypair()`."
- "The initiator MAY encode any relevant MRP parameters."
- "Any context-specific tags not listed in the above TLV schemas SHALL be reserved for future use, and SHALL be silently ignored if seen by a responder which cannot understand them."
- "The initiator SHALL send a message with Secure Channel Protocol ID and Sigma1 Protocol Opcode from Table 18, 'Secure Channel Protocol Opcodes' whose payload is the TLV-encoded Sigma1 Msg1 with an anonymous tag for the outermost struct."
- (If resuming) "the initiator SHALL: Note the `ResumptionID` of the previous session. Generate the S1RK key. Generate the `initiatorResumeMIC` using the `SharedSecret` from the previous session."

### Generate and Send Sigma2

- "The responder SHALL generate a random resumption ID `ResumptionID = Crypto_DRBG(len = 16 * 8)`."
- "The responder SHALL set the Resumption ID in the Session Context to the value `ResumptionID`."
- "The responder SHALL use the Node Operational Key Pair `ResponderNOKeyPair`, `responderNOC`, and `responderICAC` (if present) corresponding to the NOC obtained in Section 4.14.2.3.4, 'Validate Sigma1'."
- "The responder SHALL generate an ephemeral key pair `ResponderEphKeyPair = Crypto_GenerateKeypair()`."
- "The responder SHALL generate a shared secret: `SharedSecret = Crypto_ECDH(privateKey = ResponderEphKeyPair.privateKey, publicKey = Msg1.initiatorEphPubKey)`."
- "The responder SHALL encode the following items as a `sigma-2-tbsdata` with an anonymous tag: `responderNOC` as a `matter-certificate`; `responderICAC` (if present) as a `matter-certificate`; `ResponderEphKeyPair.publicKey`; `Msg1.initiatorEphPubKey`."
- "The responder SHALL generate a signature: `TBSData2Signature = Crypto_Sign(message = sigma-2-tbsdata, privateKey = ResponderNOKeyPair.privateKey)`."
- "The responder SHALL encode the following items as a `sigma-2-tbedata`, where the encoding of `responderNOC` and `responderICAC` items SHALL be byte-for-byte identical to the encoding in `sigma-2-tbsdata`."
- "The `responderNOC` as a `matter-certificate`. This encoding SHALL be byte-for-byte identical to the encoding in `sigma-2-tbsdata`."
- "The `responderICAC` (if present) as a `matter-certificate`. This encoding SHALL be byte-for-byte identical to the encoding in `sigma-2-tbsdata`."
- "The responder SHALL generate a random number `Random = Crypto_DRBG(len = 32 * 8)`."
- "The responder SHALL generate the S2K key using Random as Responder Random and `ResponderEphKeyPair.publicKey` as Responder Ephemeral Public Key."
- "The responder SHALL generate a session identifier (`ResponderSessionId`) for subsequent identification of this secured session. The `ResponderSessionId` field SHALL NOT overlap with any other existing PASE or CASE session identifier in use by the responder."
- "The responder SHALL set the Local Session Identifier in the Session Context to the value `ResponderSessionId`."
- "The responder SHALL use the Fabric IPK configured as described in Section 4.14.2.6.1, 'Identity Protection Key (IPK)'."
- "Any context-specific tags not listed in the above TLV schemas SHALL be reserved for future use, and SHALL be silently ignored if seen by an initiator which cannot understand them."
- "The responder SHALL send a message with Secure Channel Protocol ID and Sigma2 Protocol Opcode from Table 18, 'Secure Channel Protocol Opcodes' whose payload is the TLV-encoded Sigma2 Msg2 with an anonymous tag for the outermost struct."

### Generate and Send Sigma2_Resume

- "The responder SHALL encode and send a Sigma2_Resume message in response to a valid Sigma1 with response."
- "The responder SHALL generate a new resumption ID `ResumptionID = Crypto_DRBG(len = 128)`."
- "The responder SHALL generate a session identifier (`ResponderSessionId`) for subsequent identification of this session. The `ResponderSessionId` field SHALL NOT overlap with any other existing PASE or CASE session identifier in use by the responder."
- "The responder SHALL set the Local Session Identifier in the Session Context to the value `ResponderSessionId`."
- "The responder SHALL generate the S2RK key."
- "Any context-specific tags not listed in the above TLV schemas SHALL be reserved for future use, and SHALL be silently ignored if seen by an initiator which cannot understand them."
- "The responder SHALL send a message with the Secure Channel Protocol ID and Sigma2Resume Protocol Opcode from Table 18, 'Secure Channel Protocol Opcodes' whose payload is the TLV-encoded Sigma2_Resume `ResumeMsg2` with an anonymous tag for the outermost struct."
- "The responder SHALL generate the session keys as described in Section 4.14.2.6.7, 'Resumption Session Encryption Keys'."

### Generate and Send Sigma3

- "The initiator SHALL select its Node Operational Key Pair `InitiatorNOKeyPair`, Node Operational Certificates `initiatorNOC` and `initiatorICAC` (if present), and Trusted Root CA Certificate `TrustedRCAC` corresponding to the chosen Fabric as determined by the Destination Identifier from Sigma1."
- "The initiator SHALL encode the following items as a `sigma-3-tbsdata` with an anonymous tag: `initiatorNOC` as a `matter-certificate`; `initiatorICAC` (if present) as a `matter-certificate`; `InitiatorEphKeyPair.publicKey`; `Msg2.responderEphPubKey`."
- "The initiator SHALL generate a signature: `TBSData3Signature = Crypto_Sign(message = sigma-3-tbsdata, privateKey = InitiatorNOKeyPair.privateKey)`."
- "The initiator SHALL encode the following items as a `sigma-3-tbedata`: `initiatorNOC` as a `matter-certificate`. This encoding SHALL be byte-for-byte identical to the encoding in `sigma-3-tbsdata`; `initiatorICAC` (if present) as a `matter-certificate`. This encoding SHALL be byte-for-byte identical to the encoding in `sigma-3-tbsdata`; `TBSData3Signature`."
- "The initiator SHALL generate the S3K key."
- "Any context-specific tags not listed in the above TLV schemas SHALL be reserved for future use, and SHALL be silently ignored if seen by a responder which cannot understand them."
- "The initiator SHALL send a message with Secure Channel Protocol ID and Sigma3 Protocol Opcode from Table 18, 'Secure Channel Protocol Opcodes' whose payload is the TLV-encoded Sigma3 `Msg3 = { encrypted3 (1) = TBEData3Encrypted }` with an anonymous tag for the outermost struct."
- "The initiator SHALL generate the session encryption keys using the method described in Section 4.14.2.6.6, 'Session Encryption Keys'."

### Message Exchange

- [4.476] "Each message SHALL use `PROTOCOL_ID_SECURE_CHANNEL` as Protocol ID and the corresponding Protocol Opcode as defined in Table 18, 'Secure Channel Protocol Opcodes'."
- [4.479] "All CASE messages SHALL be sent reliably. This may be implicit (e.g. TCP) or explicit (e.g. MRP reliable messaging) in the underlying transport."

### Message Format

- [4.471] "All CASE messages SHALL be structured as specified in Section 4.4, 'Message Frame Format'."
- [4.472] "All CASE messages are sent using an Unsecured Session: The Session ID field SHALL be set to 0. The Session Type bits of the Security Flags SHALL be set to 0. In the CASE messages from the initiator, S Flag SHALL be set to 1 and DSIZ SHALL be set to 0. In the CASE messages from the responder, S Flag SHALL be set to 0 and DSIZ SHALL be set to 1."

### Validate Sigma1

- [4.482] "If Msg1 contains either a `resumptionID` or an `initiatorResumeMIC` field but not both, the responder SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` and perform no further processing."
- "If Msg1 contains both the `resumptionID` and `initiatorResumeMIC` fields, the responder SHALL search for an existing session that has a Resumption ID equal to the incoming `resumptionID`. If a single such session exists, the responder SHALL follow the steps in Section 4.14.2.3.10, 'Validate Sigma1 with Resumption' rather than continue the steps outlined in Section 4.14.2.3.5, 'Validate Sigma1 Destination ID'."

### Validate Sigma1 Destination ID

- "The responder SHALL traverse all its installed Node Operational Certificates (NOC), gathering the associated trusted roots' public keys from the associated chains and SHALL generate a `candidateDestinationId` based on the procedure in Section 4.14.2.4.1, 'Destination Identifier'."
- "The responder SHALL verify that the incoming `destinationId` matches one of the `candidateDestinationId` generated above."
- "If there is no `candidateDestinationId` matching the incoming `destinationId`, the responder SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: NO_SHARED_TRUST_ROOTS)` and perform no further processing."
- "Otherwise, if a match was found for the `destinationId`, the matched NOC, ICAC (if present), and associated trusted root SHALL be used for selection of the `responderNOC` and `responderICAC` in the steps for Sigma2."

### Validate Sigma1 with Resumption

- [4.487] "If the value of Success is FALSE, the responder SHALL continue processing Sigma1 as if it didn't include any resumption information by continuing the steps in Section 4.14.2.3.5, 'Validate Sigma1 Destination ID'."
- "If the value of Success is TRUE, the responder SHALL: Set the Peer Session Identifier in the in the Session Context to the value `Msg1.initiatorSessionId`. Send a Sigma2_Resume message."

### Validate Sigma2

- [4.484] "If the value of Success is FALSE, the initiator SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` and perform no further processing."
- "The initiator SHALL verify that the NOC in `TBEData2.responderNOC` and ICAC in `TBEData2.responderICAC` (if present) fulfills the following constraints: The Fabric ID and Node ID SHALL match the intended identity of the receiver Node, as included in the computation of the Destination Identifier when generating Sigma1. If an ICAC is present, and it contains a Fabric ID in its subject, then it SHALL match the FabricID in the NOC leaf certificate. The certificate chain SHALL chain back to the Trusted Root CA Certificate TrustedRCAC whose public key was used in the computation of the Destination Identifier when generating Sigma1. All the elements in the certificate chain SHALL respect the Matter Certificate DN Encoding Rules, including range checks for identifiers such as Fabric ID and Node ID."
- "If any of the validations from the previous step fail, the initiator SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` and perform no further processing."
- "If the value of Success is FALSE, the initiator SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` and perform no further processing." (after chain verification)
- "If the value of Success is FALSE, the initiator SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` and perform no further processing." (after signature verification)
- "Set the Resumption ID in the Session Context to the value `TBEData2.resumptionID`."
- "Set the Peer Session Identifier in the Session Context to the value `Msg2.responderSessionId`."

### Validate Sigma2_Resume

- [4.489] "If Success is FALSE, the initiator SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER_)` and perform no further processing."
- "The initiator SHALL set the Resumption ID in the Session Context to the value `Resume2Msg.resumptionID`."
- "The initiator SHALL generate the session keys as described in Section 4.14.2.6.7, 'Resumption Session Encryption Keys'."
- "The initiator SHALL reset its Local Message Counter in the Session Context per Section 4.6.1.1, 'Message Counter Initialization'."
- "The initiator SHALL reset the Message Reception State of the Session Context and set the synchronized `max_message_counter` of the peer to 0."
- "The initiator SHALL set `SessionTimestamp` to a timestamp from a clock which would allow for the eventual determination of the last session use relative to other sessions."
- "The initiator SHALL set the Peer Session Identifier in the in the Session Context to the value `ResumeMsg2.responderSessionId`."
- "The initiator SHALL send Section 4.14.2.3.13, 'SigmaFinished'."

### Validate Sigma3

- [4.486] "If the value of Success is FALSE, the responder SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` and perform no further processing." (after TBEData3 decrypt)
- "The responder SHALL verify that the NOC in `TBEData3.initiatorNOC` and the ICAC in `TBEData3.initiatorICAC` fulfill the following constraints: The Fabric ID SHALL match the Fabric ID matched during processing of the Destination Identifier after receiving Sigma1. If an ICAC is present, and it contains a Fabric ID in its subject, then it SHALL match the FabricID in the NOC leaf certificate. The certificate chain SHALL chain back to the Trusted Root CA Certificate TrustedRCAC whose public key was matched during processing of the Destination Identifier after receiving Sigma1. All the elements in the certificate chain SHALL respect the Matter Certificate DN Encoding Rules, including range checks for identifiers such as Fabric ID and Node ID."
- "If any of the validations from the previous step fail, the responder SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` and perform no further processing."
- "If the value of Success is FALSE, the responder SHALL send a status report: `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER_)` and perform no further processing." (after signature verification)
- "The responder SHALL generate the session keys as described in Section 4.14.2.6.6, 'Session Encryption Keys'."
- "The responder SHALL initialize its Local Message Counter in the Session Context per Section 4.6.1.1, 'Message Counter Initialization'."
- "The responder SHALL initialize the Message Reception State in the Session Context and set the synchronized `max_message_counter` of the peer to 0."
- "The responder SHALL set `SessionTimestamp` to a timestamp from a clock which would allow for the eventual determination of the last session use relative to other sessions."
- "The responder SHALL encode and send SigmaFinished."

### SigmaFinished

- [4.490] "The Node receiving Sigma3 (if a new session is being established) or Sigma2_Resume (if a session is being resumed) SHALL send a status report: `StatusReport(GeneralCode: SUCCESS, ProtocolId: SECURE_CHANNEL, ProtocolCode: SESSION_ESTABLISHMENT_SUCCESS)`."
- [4.491] "The receiving node SHALL initialize the Local Message Counter according to Section 4.6.1.1, 'Message Counter Initialization' for the newly established secure session whose success is acknowledged by this message."
- "The receiving node SHALL set `SessionTimestamp` to a timestamp from a clock which would allow for the eventual determination of the last session usage relative to other sessions."
- [4.492] "If this message is received out-of-order or unexpectedly, then it SHALL be ignored."

### Identity Protection Key (IPK)

- [4.501] "The Identity Protection Key (IPK) SHALL be the operational group key under `GroupKeySetID` of 0 for the fabric associated with the originator's chosen destination."
- [4.502] "The IPK SHALL be exclusively used for Certificate Authenticated Session Establishment. The IPK SHALL NOT be used for operational group communication."
- [4.503] "For the generation of the Destination Identifier, the originator SHALL use the operational group key with the second newest `EpochStartTime`, if one exists, otherwise it SHALL use the single operational group key available."

### Session Encryption Keys

- "A transcript hash SHALL be generated: `TranscriptHash = Crypto_Hash(message = Msg1 || Msg2 || Msg3)`."
- "The initiator SHALL use `I2RKey` to encrypt and integrity protect messages and the `R2IKey` to decrypt and verify messages."
- "The responder SHALL use `R2IKey` to encrypt and integrity protect messages and the `I2RKey` to decrypt and verify messages."
- "The `AttestationChallenge` SHALL only be used as a challenge during device attestation."

### Resumption Session Encryption Keys

- "The resumption session encryption keys SHALL be generated" (using `SharedSecret`, salt = `Sigma1.initiatorRandom || ResumptionID`, info = `"SessionResumptionKeys"`).
- "The initiator SHALL use `I2RKey` to encrypt and integrity protect messages and the `R2IKey` to decrypt and verify messages."
- "The responder SHALL use `R2IKey` to encrypt and integrity protect messages and the `I2RKey` to decrypt and verify messages."
- "The `AttestationChallenge` SHALL only be used as a challenge during device attestation."

### Session Context Storage

- [4.513] After the session is established, the following fields SHALL be recorded in the secure session context: "The peer NOC's `matter-node-id` (1.3.6.1.4.1.37244.1.1) from the subject field"; "The Section 2.5.1, 'Fabric References and Fabric Identifier' for the Fabric within which this secure session is being established"; "All peer NOC's `case-authenticated-tag` (1.3.6.1.4.1.37244.1.6) from the subject field, if present."
- [4.514] "These fields MAY be recorded at any opportune point during the protocol, but SHALL only be committed to the secure session context once the session is established successfully at both peers."

### Minimal Number of CASE Sessions

- [4.515] "A node SHALL support at least 3 CASE session contexts per fabric. Device type specifications MAY require a larger minimum. Unless the device type specification says otherwise, a minimum number it defines is a per-fabric minimum."

---

## 4. Message Formats & Data Structures

### Message Transport Header Fields (Unsecured Session)

| Field | Value |
|---|---|
| Session ID | 0 |
| Session Type bits (Security Flags) | 0 |
| S Flag (Initiator messages) | 1 |
| DSIZ (Initiator messages) | 0 |
| S Flag (Responder messages) | 0 |
| DSIZ (Responder messages) | 1 |

### Exchange Flags — I Flag per Message

| Message | I Flag |
|---|---|
| CASE Sigma1 | 1 |
| CASE Sigma2 | 0 |
| CASE Sigma3 | 1 |
| CASE Sigma2_Resume | 0 |

### Payload TLV Encoding per Message

| Message Name | Payload TLV Encoding |
|---|---|
| Sigma1 | `sigma-1-struct` |
| Sigma2 | `sigma-2-struct` |
| Sigma3 | `sigma-3-struct` |
| Sigma2_Resume | `sigma-2-resume-struct` |
| SigmaFinished | N/A (encoded via `StatusReport`) |

### Sigma1 TLV Structure (Msg1)

```
{
  initiatorRandom (1) = InitiatorRandom,
  initiatorSessionId (2) = InitiatorSessionId,
  destinationId (3) = DestinationId,
  initiatorEphPubKey (4) = InitiatorEphKeyPair.publicKey,
  initiatorSessionParams (5) = session-parameter-struct (optional),
  resumptionID (6) = ResumptionID (optional, only present if performing resumption),
  initiatorResumeMIC (7) = InitiatorResume1MIC (optional, only present if performing resumption)
}
```

### Sigma2 TLV Structure (Msg2)

```
{
  responderRandom (1) = Random,
  responderSessionId (2) = ResponderSessionId,
  responderEphPubKey (3) = ResponderEphKeyPair.publicKey,
  encrypted2 (4) = TBEData2Encrypted,
  responderSessionParams (5) = session-parameter-struct (optional)
}
```

### Sigma3 TLV Structure (Msg3)

```
{ encrypted3 (1) = TBEData3Encrypted }
```

### Sigma2_Resume TLV Structure (ResumeMsg2)

```
{
  resumptionID (1) = ResumptionID,
  sigma2ResumeMIC (2) = ResumeMIC2,
  responderSessionID (3) = ResponderSessionId,
  responderSessionParams (4) = session-parameter-struct (optional)
}
```

### AEAD Nonces (13-byte values, exact byte sequences from spec)

| Key | Nonce String | Nonce Bytes |
|---|---|---|
| S1RK (Sigma1 Resume MIC) | `"NCASE_SigmaS1"` | `{0x4e,0x43,0x41,0x53,0x45,0x5f,0x53,0x69,0x67,0x6d,0x61,0x53,0x31}` |
| S2K (Sigma2 encrypt) | `"NCASE_Sigma2N"` | `{0x4e,0x43,0x41,0x53,0x45,0x5f,0x53,0x69,0x67,0x6d,0x61,0x32,0x4e}` |
| S3K (Sigma3 encrypt) | `"NCASE_Sigma3N"` | `{0x4e,0x43,0x41,0x53,0x45,0x5f,0x53,0x69,0x67,0x6d,0x61,0x33,0x4e}` |
| S2RK (Sigma2 Resume MIC) | `"NCASE_SigmaS2"` | `{0x4e,0x43,0x41,0x53,0x45,0x5f,0x53,0x69,0x67,0x6d,0x61,0x53,0x32}` |
| S1RK verify (Resume1MIC) | `"NCASE_SigmaR1"` | `{0x4e,0x43,0x41,0x53,0x45,0x5f,0x53,0x69,0x67,0x6d,0x61,0x53,0x31}` |
| S2RK verify (Resume2MIC) | `"NCASE_SigmaR2"` | `{0x4e,0x43,0x41,0x53,0x45,0x5f,0x53,0x69,0x67,0x6d,0x61,0x53,0x32}` |

### Key Derivation Info Strings

| Key | `info` value | info bytes |
|---|---|---|
| S2K | `"Sigma2"` | `{0x53,0x69,0x67,0x6d,0x61,0x32}` |
| S3K | `"Sigma3"` | `{0x53,0x69,0x67,0x6d,0x61,0x33}` |
| Session Encryption Keys | `"SessionKeys"` | `{0x53,0x65,0x73,0x73,0x69,0x6f,0x6e,0x4b,0x65,0x79,0x73}` |
| Resumption Session Keys | `"SessionResumptionKeys"` | `{0x53,0x65,0x73,0x73,0x69,0x6f,0x6e,0x52,0x65,0x73,0x75,0x6d,0x70,0x74,0x69,0x6f,0x6e,0x4b,0x65,0x79,0x73}` |

### Session Encryption Key Derivation

**New session (`SEKeys`):**
```
TranscriptHash = Crypto_Hash(message = Msg1 || Msg2 || Msg3)
I2RKey || R2IKey || AttestationChallenge = Crypto_KDF(
  inputKey = SharedSecret,
  salt = IPK || TranscriptHash,
  info = SEKeys_Info,
  len = 3 * CRYPTO_SYMMETRIC_KEY_LENGTH_BITS
)
```

**Resumption session (`RSEKeys`):**
```
I2RKey || R2IKey || AttestationChallenge = Crypto_KDF(
  inputKey = SharedSecret,
  salt = Sigma1.initiatorRandom || ResumptionID,
  info = RSEKeys_Info,
  len = 3 * CRYPTO_SYMMETRIC_KEY_LENGTH_BITS
)
```

**S2K:**
```
TranscriptHash = Crypto_Hash(message = Msg1)
S2K = Crypto_KDF(
  inputKey = SharedSecret,
  salt = IPK || Responder Random || Responder Ephemeral Public Key || TranscriptHash,
  info = S2K_Info,
  len = CRYPTO_SYMMETRIC_KEY_LENGTH_BITS
)
```

**S3K:**
```
TranscriptHash = Crypto_Hash(message = Msg1 || Msg2)
S3K = Crypto_KDF(
  inputKey = SharedSecret,
  salt = IPK || TranscriptHash,
  info = S3K_Info,
  len = CRYPTO_SYMMETRIC_KEY_LENGTH_BITS
)
```

### IPK Epoch Key Selection Table

| Number of keys in Group Key Set | Operational key index | Epoch Key |
|---|---|---|
| 1 | 0 | EpochKey0 |
| 2 | 0 | EpochKey0 |
| 3 | 1 | EpochKey1 |

### Destination Identifier Computation

Components concatenated as `destinationMessage`:
1. `initiatorRandom` — random value from the same Sigma1 message
2. `rootPublicKey` — public key of the root of trust (uncompressed EC point, SEC 1 §2.3.3)
3. `fabricId` — 64-bit Fabric ID encoded as little-endian octet string
4. `nodeId` — 64-bit Node ID encoded as little-endian octet string

```
destinationIdentifier = Crypto_HMAC(key = IPK, message = destinationMessage)
  // result length = CRYPTO_HASH_LEN_BYTES
```

---

## 5. Security Considerations

- [4.501] "The Identity Protection Key (IPK) SHALL be the operational group key under `GroupKeySetID` of 0 for the fabric associated with the originator's chosen destination."
- [4.502] "The IPK SHALL be exclusively used for Certificate Authenticated Session Establishment. The IPK SHALL NOT be used for operational group communication."
- [4.503] For Destination Identifier generation, the originator SHALL use the operational group key with the second newest `EpochStartTime`, or the single available key if only one exists.
- [4.494] The Destination Identifier "requires an initiator to have knowledge of both the IPK and one of the full identities of the responder Node before it forces the responder node to generate a costly Sigma2 message." It "hides which Fabric was chosen by the initiator." A Node "MAY choose to keep memory of some prior destination identifiers that were successfully processed which it would later reject if seen again, for additional replay protection."
- [4.513–4.514] Peer NOC `matter-node-id`, Fabric ID, and `case-authenticated-tag` SHALL be committed to the secure session context only once the session is established successfully at both peers.
- Certificate chain validation is required in both directions: initiator verifies responder's NOC/ICAC against the expected `TrustedRCAC` and Destination Identifier; responder verifies initiator's NOC/ICAC against the Fabric ID matched from Sigma1.
- All certificate elements SHALL respect Matter Certificate DN Encoding Rules including range checks for Fabric ID and Node ID.
- `AttestationChallenge` SHALL only be used as a challenge during device attestation.
- Session encryption keys are asymmetric by direction: I2RKey (Initiator-to-Responder) and R2IKey (Responder-to-Initiator); each party encrypts with one and decrypts with the other.

---

## 6. Error Handling & Timing

### Error Codes and Conditions

| Condition | Status Report Sent |
|---|---|
| Sigma1 contains `resumptionID` but not `initiatorResumeMIC`, or vice versa | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| No `candidateDestinationId` matches incoming `destinationId` | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: NO_SHARED_TRUST_ROOTS)` |
| Sigma2 `TBEData2` AEAD decrypt fails (Success = FALSE) | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma2 NOC/ICAC constraint validation fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma2 chain verification fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma2 signature verification fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma3 `TBEData3` AEAD decrypt fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma3 NOC/ICAC constraint validation fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma3 chain verification fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma3 signature verification fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Sigma2_Resume MIC verification fails | `StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER)` |
| Session establishment succeeds | `StatusReport(GeneralCode: SUCCESS, ProtocolId: SECURE_CHANNEL, ProtocolCode: SESSION_ESTABLISHMENT_SUCCESS)` |

### Out-of-Order / Unexpected Messages

- [4.492] "If this message [SigmaFinished] is received out-of-order or unexpectedly, then it SHALL be ignored."

### Session Counter Initialization

- On successful receipt of SigmaFinished: "The receiving node SHALL initialize the Local Message Counter according to Section 4.6.1.1, 'Message Counter Initialization' for the newly established secure session."
- After Sigma2_Resume validation: initiator SHALL reset Local Message Counter and Message Reception State, and set synchronized `max_message_counter` of the peer to 0.
- After Sigma3 validation: responder SHALL initialize Local Message Counter and Message Reception State, and set synchronized `max_message_counter` of the peer to 0.

### Resumption Fallback

- If Resume1MIC decryption fails (Success = FALSE during Validate Sigma1 with Resumption), the responder SHALL continue processing Sigma1 as a standard (non-resumption) Sigma1 — no error is sent; the flow falls back gracefully to full session establishment.

### Reliable Delivery

- [4.479] "All CASE messages SHALL be sent reliably. This may be implicit (e.g. TCP) or explicit (e.g. MRP reliable messaging) in the underlying transport."