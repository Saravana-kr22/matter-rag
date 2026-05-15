# Matter Spec Summary: SC NFC

**Source sections matched:** 35  
**Source chars sent to LLM:** 32,994  
**Generated:** 2026-04-29 12:19:03  
**Summary words:** 2,138  

---

## 1. Overview

The NFC Transport Layer (NTL) defines how to transfer Matter commissioning messages over an NFC interface (referred to as NFC-based Commissioning). NTL is only used for device commissioning over NFC; it is not used for reading the Onboarding Payload from an NFC Tag. Support for NTL and all features requiring its use is provisional.

Messages are embedded into Application Programming Data Units (APDUs) in compliance with ISO/IEC 7816-4. APDUs are transferred in blocks per the ISO-DEP protocol as defined in the NFC Forum Digital Specification. Blocks are transmitted wirelessly using NFC-A technology as defined in the NFC Forum Digital Specification.

NTL provides a reliable, datagram-oriented, transport interface with **asymmetric roles**: one end always transmits first and the other always responds. When NTL is used for Matter commissioning, the Commissioner always transmits first and the Commissionee responds.

Commands (C-APDU) are always issued by the NFC Reader/Writer (the Matter Commissioner); responses (R-APDU) are always issued by the NFC Listener (the Matter Commissionee).

---

## 2. Protocol Flow & State Machine

### NFC-A Activation (ISO-DEP Layer)
1. The NFC Reader Device sends a **RATS** frame encoding the maximum frame size it can receive (FSD).
2. The NFC Tag Device sends an **ATS** frame encoding the maximum frame size it can receive (FSC).
3. The ISO-DEP layer uses FSD/FSC to segment APDUs using the ISO-DEP chaining feature. On the receiver side, ISO-DEP re-assembles chained segments.
4. If the responder requires more time to respond than the default maximum response timing, it can extend the initiator waiting time by sending an appropriate S-block (waiting time extension).

### APDU Layer — Commissioning Initiation
1. The Commissioner (NFC Reader/Writer) issues a **SELECT** command with AID `A0 00 00 09 09 8A 77 E4 01` to initiate commissioning.
2. If the Commissionee is in commissioning mode, it answers with a successful R-APDU containing Version, Discriminator, Vendor ID, Product ID, and optional Extended Data, with SW1=0x90, SW2=0x00.
3. If the Commissionee is not in commissioning mode, it either ignores the command (no response) or returns an error R-APDU with SW1=0x69, SW2=0x85.

### APDU Layer — Matter Message Exchange
1. Once commissioning is successfully initiated, Matter messages are exchanged using the proprietary **TRANSPORT** command (INS=0x20).
2. P1 and P2 encode the total message length (P1 = most significant byte). Lc encodes the payload fragment length. Le encodes the maximum response length the reader/writer can receive.
3. If the full response fits within Le bytes, the R-APDU returns SW1=0x90, SW2=0x00.
4. If the full response does not fit within Le bytes, the R-APDU returns SW1=0x61 and SW2 encodes the number of bytes remaining; the Commissioner then issues a **GET RESPONSE** command.
5. The GET RESPONSE command (CLA=0x00, INS=0xC0, P1=0x00, P2=0x00) SHALL only be issued following reception of an incomplete successful R-APDU. If received out of sequence, the Commissionee returns SW1=0x69, SW2=0x85.
6. If a chained message exceeds the maximum supported message size, the Commissionee returns SW1=0x6A, SW2=0x84 ("Not enough memory space").

### APDU Chaining
- Both the NFC Reader/Writer and NFC Listener use short field coding (short length field) of APDUs.
- C-APDU chaining uses CLA=0x80 for unchained and CLA=0x90 for chained TRANSPORT commands.
- Via APDU chaining, the protocol can handle Matter messages up to 65535 bytes, subject to implementation memory constraints.

---

## 3. Normative Requirements

### 4.21 NFC Transport Layer (NTL)

> [4.789] "Devices using NTL SHALL also include NFC onboarding payload as defined in NFC Tag, ensuring the NFC interface provides a consistent commissioning solution."

> [4.790] "Additionally, devices supporting NTL SHALL support an alternate commissioning channel (e.g., BLE, Wi-Fi PAF, Ethernet). This guarantees that commissioning is always possible, even if NFC-based commissioning is unavailable on the Commissioner."

### 4.21.1 NFC Forum Requirements

> "products implementing NTL as a Commissioner SHALL comply with the NFC Forum requirements for an NFC Forum Reader/Writer Module supporting Type 4A Tag Operation,"

> "products implementing NTL as a Commissionee SHALL comply with the NFC Forum requirements for either NFC Forum Type 4A Tag Module or NFC Forum Type 4A Tag Platform Module (Card Emulation)."

### 4.21.2 NFC-A Technology

> [4.792] "Commissioners supporting NTL SHALL act as an NFC Forum Type 4A Tag Platform in Poll Mode as defined in NFC Forum Digital Specification."

> [4.793] "Commissionees supporting NTL SHALL act as an NFC Forum Type 4A Tag Platform in Listen Mode as defined in NFC Forum Digital Specification."

### 4.21.3 ISO-DEP

> [4.795] "The full ISO-DEP protocol SHALL be implemented in compliance with NFC Forum Digital Specification."

### 4.21.4 APDU Layer

> [4.805] "both the NFC Reader/Writer and NFC listener SHALL always use short field coding (aka short length field) of APDUs."

> [4.806] "Both commissionee and commissioner SHALL support C-APDU and R-APDU chaining specified in ISO/IEC 7816-4."

> [4.807] "In case the size of the Matter message to transmit in the TRANSPORT command APDU is bigger than the maximum size that can be transmitted by this APDU, the APDU chaining procedure specified in ISO/IEC 7816-4 SHALL be used, even though the TRANSPORT command belongs to proprietary class."

### 4.21.4.1 SELECT Command

> [4.808] "Matter commissioning SHALL be initiated by the NFC Reader/Writer by issuing the 7816-4 SELECT command with the Application Identifier (AID) 'A0 00 00 09 09 8A 77 E4 01'..."

> [4.809] "When in commissioning mode, a commissionee SHALL answer to a correct SELECT command with a successful response APDU..."

> [4.810] "Version is a uint8 that SHALL encode the NTL protocol version supported by the commissionee. The version SHALL be 0x01. Other values SHALL be reserved for future use. The commissioner SHALL use the corresponding NTL protocol to communicate with the commissionee."

> [4.810] "When choosing not to advertise both Vendor ID and Product ID, the device SHALL set both Vendor ID and Product ID fields to 0. When choosing not to advertise only the Product ID, the device SHALL set the Product ID field to 0. A device SHALL NOT set the Vendor ID to 0 when providing a non-zero Product ID."

> [4.811] "When not in commissioning mode, a commissionee SHALL either ignore the command (no response) or answer with an error response APDU..."

### 4.21.4.2 TRANSPORT Command

> [4.813] "Once commissioning has been successfully initiated with the SELECT command, Matter messages SHALL be exchanged using the proprietary TRANSPORT command."

> [4.813] "The Lc single-octet field SHALL encode the length in octets of the payload's Data field in compliance with ISO/IEC 7816-4."

> [4.813] "To optimize underlying ISO-DEP chaining, Lc SHOULD be less or equal than min(255,FSC-10)."

> [4.813] "P1 and P2 SHALL encode the number of octets of the full message to transmit. It is encoded as a 2-bytes integer with P1 being the most significant byte."

> [4.813] "The same value SHALL be used in all chained commands."

> [4.813] "The Data field SHALL contain a fragment of the message to transmit, possibly the only one."

> [4.813] "The Le single-octet field SHALL encode the maximum length in octets that the reader/writer can receive in the response APDU in compliance with ISO/IEC 7816-4."

> [4.813] "To optimize underlying ISO-DEP chaining, Le SHOULD encode the value equal to min(256,FSD-6)."

> [4.814] "In case the full message fits within the number of bytes encoded by Le in C-APDU, a successful response SHALL be indicated by the SW1 and SW2 values in Table 48." (SW1=0x90, SW2=0x00)

> [4.815] "In case the full message does not fit within the number of bytes encoded by Le in C-APDU, a successful but incomplete response SHALL be indicated by the SW1 value in Table 49, and SW2 SHALL encode the number of bytes of message to be sent in the next GET RESPONSE R-APDU." (SW1=0x61)

> [4.816] "In case the chained message exceeds the maximum supported message size, an error response conforming to Table 50 SHALL be issued, indicating 'Not enough memory space'." (SW1=0x6A, SW2=0x84)

### 4.21.4.3 GET RESPONSE Command

> [4.817] "This command SHALL be issued following the reception of an incomplete successful R-APDU, in compliance with ISO/IEC 7816-4."

> [4.817] "The Le single-octet field SHALL encode the maximum length in octets that the reader/writer can receive in the response APDU in compliance with ISO/IEC 7816-4."

> [4.817] "To optimize underlying ISO-DEP chaining, Le SHOULD encode the value equal to min(256,FSD-6)."

> [4.819] "In case GET RESPONSE is received, but not following a TRANSPORT successful Response APDU - Incomplete message, the commissionee SHALL answer with an error indicating the condition of use is not satisfied..." (SW1=0x69, SW2=0x85)

---

## 4. Message Formats & Data Structures

### SELECT Command (Table 44)

| Field | Value |
|-------|-------|
| CLA | 0x00 |
| INS | 0xA4 |
| P1 | 0x04 |
| P2 | 0x0C |
| Lc | 0x09 |
| Data | A0:00:00:09:09:8A:77:E4:01 |
| Le | 0x00 |

### SELECT Success Response (Table 45)

| Field | Size |
|-------|------|
| Version | 8 bits (SHALL be 0x01) |
| Reserved | 8 bits (cleared) |
| Format | 4 bits (cleared) |
| Discriminator | 12 bits |
| Vendor ID | 16 bits |
| Product ID | 16 bits |
| Extended Data | 0 or more bits (MAY be omitted) |
| SW1 | 0x90 |
| SW2 | 0x00 |

### SELECT Error Response — Not in Commissioning Mode (Table 46)

| Field | Value |
|-------|-------|
| Data | Version |
| SW1 | 0x69 |
| SW2 | 0x85 |

### TRANSPORT Command

| Field | Value/Description |
|-------|-------------------|
| CLA | 0x80 (unchained) / 0x90 (chained) |
| INS | 0x20 |
| P1 | MSB of full message length |
| P2 | LSB of full message length |
| Lc | Length of payload Data fragment (single-octet, short field coding) |
| Data | Fragment of the message to transmit |
| Le | Max response length (single-octet, short field coding) |

### TRANSPORT Response — Success / Complete (Table 48)

| Field | Value |
|-------|-------|
| Data | Message or last fragment |
| SW1 | 0x90 |
| SW2 | 0x00 |

### TRANSPORT Response — Success / Incomplete (Table 49)

| Field | Value |
|-------|-------|
| Data | Fragment of message |
| SW1 | 0x61 |
| SW2 | Number of bytes remaining in next GET RESPONSE |

### TRANSPORT Response — Not Enough Memory (Table 50)

| Field | Value |
|-------|-------|
| SW1 | 0x6A |
| SW2 | 0x84 |

### GET RESPONSE Command

| Field | Value |
|-------|-------|
| CLA | 0x00 |
| INS | 0xC0 |
| P1 | 0x00 |
| P2 | 0x00 |
| Le | Max response length (single-octet, short field coding) |

### GET RESPONSE — Out-of-Sequence Error (Table 54)

| Field | Value |
|-------|-------|
| SW1 | 0x69 |
| SW2 | 0x85 |

### GET RESPONSE Success Responses (Tables 52–53)

| Data | SW1 | SW2 |
|------|-----|-----|
| Message or last fragment | 0x90 | 0x00 |
| Fragment of message | 0x61 | (bytes remaining) |

---

## 5. Security Considerations

> [4.790] Devices supporting NTL SHALL support an alternate commissioning channel (e.g., BLE, Wi-Fi PAF, Ethernet), guaranteeing that commissioning is always possible even if NFC-based commissioning is unavailable on the Commissioner.

The spec text states that support for NTL and all features requiring its use is **provisional**.

No additional session-level security, authentication, key derivation, or threat-model content pertaining specifically to the NTL transport itself is covered in the provided spec sections. (The CHECK-IN protocol content included in the provided text — sections 4.22.x — covers a separate encrypted sessionless message mechanism and is not part of the NTL transport layer proper.)

---

## 6. Error Handling & Timing

### Error Status Codes

| Condition | SW1 | SW2 |
|-----------|-----|-----|
| Commissionee not in commissioning mode (SELECT response) | 0x69 | 0x85 |
| TRANSPORT chained message exceeds max supported size | 0x6A | 0x84 |
| GET RESPONSE received out of sequence (not following incomplete TRANSPORT response) | 0x69 | 0x85 |

### Retransmission / Reliability

ISO-DEP provides retransmission when a frame is not received or is incorrectly received, making it a reliable transport protocol. When a responder requires more time than the default maximum response timing, it extends the initiator waiting time by sending an appropriate S-block (waiting time extension).

### Message Size Constraints

Some smartphone NFC Reader/Writer implementations are limited to a maximum 256-byte APDU payload. To guarantee interoperability, both sides SHALL always use short field coding (short length field). The maximum Matter message size transferable via APDU chaining is 65535 bytes, but is further constrained by implementation memory limits and Message Size Requirements.

Lc SHOULD be ≤ min(255, FSC-10) to optimize underlying ISO-DEP chaining. Le SHOULD equal min(256, FSD-6) for the same reason.