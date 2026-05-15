# Matter Spec Summary: SC PASE

**Source sections matched:** 10  
**Source chars sent to LLM:** 11,592  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 1,758  

---

## 1. Overview

PASE (Passcode-Authenticated Session Establishment) is the session establishment protocol used exclusively when commissioning a Node (i.e., the Commissionee). It uses a shared passcode together with an augmented Password-Authenticated Key Exchange (PAKE), in which only one party knows the passcode beforehand, to generate shared keys.

The protocol involves two roles: an **initiator** (Commissionee) and a **responder**.

---

## 2. Protocol Flow & State Machine

The PASE handshake consists of six messages in strict sequence, all part of the same message exchange:

```
Initiator                                Responder
   |                                         |
   |-------- PBKDFParamRequest ------------>|
   |                                         | Verify passcodeID == 0
   |                                         | Generate ResponderRandom, ResponderSessionId
   |<-------- PBKDFParamResponse -----------|
   |                                         |
   | Generate Crypto_PAKEValues_Initiator    |
   | Compute pA                              |
   |-------- Pake1 (pA) ------------------>|
   |                                         | Compute pB, TT, (cA, cB, Ke)
   |<-------- Pake2 (pB, cB) --------------|
   |                                         |
   | Compute TT, (cA, cB, Ke)               |
   | Verify Pake2.cB                         |
   |-------- Pake3 (cA) ------------------>|
   |                                         | Verify Pake3.cA
   |                                         | Set SessionTimestamp
   |<-------- PakeFinished (StatusReport) --|
   |                                         |
   | Set SessionTimestamp                    |
   | Derive session keys                     | Derive session keys
```

**Key state transitions:**

- On **PBKDFParamRequest**: Initiator sets its Local Session Identifier to `InitiatorSessionId`. Responder sets its Local Session Identifier to `ResponderSessionId` and sets Peer Session Identifier to `PBKDFParamRequest.initiatorSessionId`.
- On **PBKDFParamResponse**: Initiator sets Peer Session Identifier to `PBKDFParamResponse.responderSessionId`.
- On **Pake3 receipt**: Responder sets `SessionTimestamp`.
- On **PakeFinished receipt**: Initiator sets `SessionTimestamp`. Session keys are installed by both parties.

**Encrypted application data is blocked** until PakeFinished is received by the initiator.

---

## 3. Normative Requirements

### Message Exchange

> [4.446] "The initiator and responder SHALL NOT send encrypted application data in the newly established session until PakeFinished is received by the initiator within the unencrypted session used for establishment."

> [4.447] "Each message SHALL use PROTOCOL_ID_SECURE_CHANNEL as Protocol ID and the corresponding Protocol Opcode as defined in Table 18, "Secure Channel Protocol Opcodes"."

> [4.449] "All PASE messages SHALL be sent reliably. This may be implicit (e.g. TCP) or explicit (e.g. MRP reliable messaging) in the underlying transport."

### Message Format

> [4.441] "All PASE messages SHALL be structured as specified in Section 4.4, "Message Frame Format"."

> [4.442] "All PASE messages are sent using an Unsecured Session:"
> - "The Session ID field SHALL be set to 0."
> - "The Session Type bits of the Security Flags SHALL be set to 0."
> - "In the PASE messages from the initiator, S Flag SHALL be set to 1 and DSIZ SHALL be set to 0."
> - "In the PASE messages from the responder, S Flag SHALL be set to 0 and DSIZ SHALL be set to 1."

> [4.445] "For all TLV-encoded PASE messages, any context-specific tags not listed in the associated TLV schemas SHALL be reserved for future use, and SHALL be silently ignored if seen by a recipient which cannot understand them."

### PBKDFParamRequest

> [4.451] "The initiator SHALL generate a random number InitiatorRandom = Crypto_DRBG(len = 32 * 8)."

> [4.451] "The initiator SHALL generate a session identifier (InitiatorSessionId) for subsequent identification of this session. The InitiatorSessionId field SHALL NOT overlap with any other existing PASE or CASE session identifier in use by the initiator."

> [4.451] "The initiator SHALL set the Local Session Identifier in the Session Context to the value InitiatorSessionId."

> [4.451] "A value of 0 for the passcodeID SHALL correspond to the PAKE passcode verifier for the currently-open commissioning window, if any."

> [4.451] "The initiator SHALL indicate whether the PBKDF parameters (salt and iterations) are known for the particular passcodeId [...] by setting HasPBKDFParameters. If HasPBKDFParameters is set to True, the responder SHALL NOT return the PBKDF parameters. If HasPBKDFParameters is set to False, the responder SHALL return the PBKDF parameters."

> [4.451] "The initiator SHALL send a message with the appropriate Protocol Id and Protocol Opcode from Table 18, "Secure Channel Protocol Opcodes" whose payload is the TLV-encoded pbkdfparamreq-struct PBKDFParamRequest with an anonymous tag for the outermost struct."

### PBKDFParamResponse

> [4.452] "On receipt of PBKDFParamRequest, the responder SHALL:"
> - "Verify passcodeID is set to 0. If verification fails, the responder SHALL send a status report: StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER) and perform no further processing."
> - "Generate a random number ResponderRandom = Crypto_DRBG(len = 32 * 8)."
> - "Generate a session identifier (ResponderSessionId) [...]. The ResponderSessionId field SHALL NOT overlap with any other existing PASE or CASE session identifier in use by the responder."
> - "The responder SHALL set the Local Session Identifier in the Session Context to the value ResponderSessionId."
> - "Set the Peer Session Identifier in the Session Context to the value PBKDFParamRequest.initiatorSessionId."
> - "If PBKDFParamRequest.hasPBKDFParameters is True the responder SHALL NOT include the PBKDF parameters (i.e. salt and iteration count) in the Crypto_PBKDFParameterSet. If Msg1.hasPBKDFParameters is False the responder SHALL include the PBKDF parameters (i.e. salt and iteration count) in the Crypto_PBKDFParameterSet."

### Pake1

> [4.453] "On receipt of PBKDFParamResponse, the initiator SHALL:"
> - "Set the Peer Session Identifier in the Session Context to the value PBKDFParamResponse.responderSessionId."
> - "Generate the Crypto_PAKEValues_Initiator according to the PBKDFParamResponse.pbkdf_parameters"
> - "Using Crypto_PAKEValues_Initiator, generate pA := Crypto_pA(Crypto_PAKEValues_Initiator)"

### Pake2

> [4.454] "On receipt of Pake1, the responder SHALL:"
> - "Compute pB := Crypto_pB(Crypto_PAKEValues_Responder) using the passcode verifier indicated in PBKDFParamRequest"
> - "Compute TT := Crypto_Transcript(PBKDFParamRequest, PBKDFParamResponse, Pake1.pA, pB)"
> - "Compute (cA, cB, Ke) := Crypto_P2(TT, Pake1.pA, pB)"

### Pake3

> [4.455] "On receipt of Pake2, the initiator SHALL:"
> - "Compute TT := Crypto_Transcript(PBKDFParamRequest, PBKDFParamResponse, Pake1.pA, Pake2.pB)"
> - "Compute (cA, cB, Ke) := Crypto_P2(TT, Pake1.pA, Pake2.pB)"
> - "Verify Pake2.cB against cB. If verification fails, the initiator SHALL send a status report: StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER) and perform no further processing."

> [4.455] "The initiator SHALL NOT send any encrypted application data until it receives PakeFinished from the responder."

> [4.456] "On reception of Pake3, the responder SHALL:"
> - "Verify Pake3.cA against cA. If verification fails, the responder SHALL send a status report: StatusReport(GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER) and perform no further processing."
> - "The responder SHALL set SessionTimestamp to a timestamp from a clock which would allow for the eventual determination of the last session use relative to other sessions."
> - "The responder SHALL encode and send PakeFinished."

### PakeFinished

> [4.457] "To indicate the successful completion of the protocol, the responder SHALL send a status report: StatusReport(GeneralCode: SUCCESS, ProtocolId: SECURE_CHANNEL, ProtocolCode: SESSION_ESTABLISHMENT_SUCCESS)."

> [4.458] "The initiator SHALL set SessionTimestamp to a timestamp from a clock which would allow for the eventual determination of the last session use relative to other sessions."

### Session Encryption Keys

> [4.459] "Each key is exactly CRYPTO_SYMMETRIC_KEY_LENGTH_BITS bits."

> [4.459] "The initiator SHALL use I2RKey to encrypt and integrity protect messages and the R2IKey to decrypt and verify messages."

> [4.459] "The responder SHALL use R2IKey to encrypt and integrity protect messages and the I2RKey to decrypt and verify messages."

> [4.459] "The AttestationChallenge SHALL only be used as a challenge during device attestation."

> [4.460] "Upon initial installation of the new PASE Session Keys:"
> - "The Node SHALL initialize its Local Message Counter in the Session Context per Section 4.6.1.1, "Message Counter Initialization"."
> - "The Node SHALL initialize the Message Reception State in the Session Context and set the synchronized max_message_counter of the peer to 0."

---

## 4. Message Formats & Data Structures

### Exchange Flags (I Flag) per message

| Message | I Flag |
|---|---|
| PBKDFParamRequest | 1 |
| PBKDFParamResponse | 0 |
| Pake1 | 1 |
| Pake2 | 0 |
| Pake3 | 1 |

### TLV Payload Encodings

| Message Name | Payload TLV Encoding |
|---|---|
| PBKDFParamRequest | pbkdfparamreq-struct |
| PBKDFParamResponse | pbkdfparamresp-struct |
| Pake1 | pake-1-struct |
| Pake2 | pake-2-struct |
| Pake3 | pake-3-struct |
| PakeFinished | N/A (encoded via StatusReport) |

### PBKDFParamRequest TLV Schema
```
PBKDFParamRequest =
{
  initiatorRandom (1) = InitiatorRandom,
  initiatorSessionId (2) = InitiatorSessionId,
  passcodeID (3) = PasscodeId,
  hasPBKDFParameters (4) = HasPBKDFParameters,
}
```

### PBKDFParamResponse TLV Schema
```
PBKDFParamResponse =
{
  initiatorRandom (1) = PBKDFParamRequest.initiatorRandom,
  responderRandom (2) = ResponderRandom,
  responderSessionId (3) = ResponderSessionId,
  pbkdf_parameters (4) = PBKDFParameters
}
```

### Pake1 TLV Schema
```
Pake1 =
{
  pA (1) = pA,
}
```

### Pake2 TLV Schema
```
Pake2 =
{
  pB (1) = pB,
  cB (2) = cB,
}
```

### Pake3 TLV Schema
```
Pake3 =
{
  cA (1) = cA,
}
```

All outermost structs use **anonymous tags**. All messages use `PROTOCOL_ID_SECURE_CHANNEL` as Protocol ID with corresponding Protocol Opcodes from Table 18.

---

## 5. Security Considerations

- PASE is used **only** during commissioning of a Node.
- All PASE messages are sent over an **Unsecured Session** (Session ID = 0, Session Type bits = 0).
- The protocol uses an **augmented PAKE** where only one party (the responder) knows the passcode beforehand.
- Session identifiers (`InitiatorSessionId`, `ResponderSessionId`) **SHALL NOT overlap** with any other existing PASE or CASE session identifier in use by the respective party.
- The `AttestationChallenge` derived during PASE **SHALL only be used as a challenge during device attestation**.
- Encrypted application data **SHALL NOT** be sent until `PakeFinished` is received by the initiator, ensuring no data is transmitted over the session before mutual authentication is complete.
- Both parties must verify their respective confirmation values (`cB` and `cA`) before proceeding; failure results in an `INVALID_PARAMETER` status report and immediate termination.
- Session keys are directional: `I2RKey` for initiator-to-responder encryption, `R2IKey` for responder-to-initiator encryption.

---

## 6. Error Handling & Timing

### Error Codes

| Trigger Condition | Status Report Sent By | StatusReport Parameters |
|---|---|---|
| `passcodeID` ≠ 0 in PBKDFParamRequest | Responder | `GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER` |
| `Pake2.cB` verification fails | Initiator | `GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER` |
| `Pake3.cA` verification fails | Responder | `GeneralCode: FAILURE, ProtocolId: SECURE_CHANNEL, ProtocolCode: INVALID_PARAMETER` |
| Successful completion | Responder (PakeFinished) | `GeneralCode: SUCCESS, ProtocolId: SECURE_CHANNEL, ProtocolCode: SESSION_ESTABLISHMENT_SUCCESS` |

### Failure Behavior

In all error cases, the sending party performs **no further processing** after sending the `INVALID_PARAMETER` status report.

### Reliability

All PASE messages **SHALL be sent reliably** — either implicitly (e.g., TCP) or explicitly (e.g., MRP reliable messaging).

### Timing

No explicit timeout values are specified in the provided spec text. However, both parties **SHALL set `SessionTimestamp`** upon session completion (responder on Pake3 verification success; initiator on PakeFinished receipt), using a clock that allows determination of the last session use relative to other sessions.