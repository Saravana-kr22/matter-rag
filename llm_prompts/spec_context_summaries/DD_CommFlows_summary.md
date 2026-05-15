# Matter Spec Summary: DD Commissioning Flows

**Source sections matched:** 2  
**Source chars sent to LLM:** 24,708  
**Generated:** 2026-04-29 13:14:04  
**Summary words:** 3,880  

---

## 1. Overview

This section covers the two commissioning flows defined in the Matter protocol for onboarding a Commissionee onto an operational network under the control of a Commissioner. The two flows are:

- **Concurrent connection commissioning flow**: The Commissioner and Commissionee can maintain two simultaneous network connections — one to the operational network and one as the commissioning channel.
- **Non-concurrent connection commissioning flow**: The Commissioner and Commissionee cannot simultaneously maintain both the commissioning channel and a connection to the operational network being configured.

Key roles are the **Commissioner** (initiates and drives commissioning) and the **Commissionee** (device being commissioned). A third role, the **Administrator**, may finalize commissioning; it may be the Commissioner itself or a separate Node delegated Administer privilege. The commissioning channel carries PASE-encrypted traffic; the operational network connection uses CASE. The process is time-bounded by a fail-safe timer. Commissioning commands and attributes are carried via the Interaction Model and defined in the General Commissioning Cluster, Network Commissioning Cluster, Thread Network Diagnostics Cluster, and Wi-Fi Network Diagnostics Cluster.

---

## 2. Protocol Flow & State Machine

The commissioning flow proceeds through numbered steps. Optional steps apply when bracketed conditions are met. Unless indicated otherwise, a Commissioner SHALL complete each step (including waiting for responses) before advancing.

**Step 1 — Prerequisites**
Commissioner SHALL have regulatory and fabric information available, and SHOULD have accurate date, time, and timezone.

**Step 2 — Channel Establishment**
Commissioner and Commissionee SHALL establish a commissioning channel after discovery or direct physical tap.

**Steps 3–5 — Terms and Conditions (conditional)**
If both Commissioner and device support Terms and Conditions (TC) Acknowledgement:
- Commissioner SHALL obtain TC from `EnhancedSetupFlowTCUrl`.
- Commissioner SHALL present TC to user (unless already has prior responses).
- Commissioner SHALL receive user responses for use in step 9.

**Step 6 — PASE Session Establishment**
Commissioner and Commissionee SHALL establish encryption keys via PASE on the commissioning channel. All subsequent messages on the commissioning channel are encrypted using PASE-derived keys. Upon PASE completion, the Commissionee SHALL autonomously arm the Fail-safe timer for 60 seconds.

**Step 7 — Fail-Safe Re-arm**
Commissioner SHALL re-arm the Fail-safe timer to the desired commissioning timeout within 60 seconds of PASE completion, using the `ArmFailSafe` command. Commissioner MAY first read `BasicCommissioningInfo` to obtain guidance on the fail-safe value.

**Step 8 — Regulatory and Time Configuration**
Commissioner configures regulatory information and time cluster (order not critical):
- If Commissionee has a Network Commissioning cluster instance with WI or TH feature flags, Commissioner SHALL configure regulatory information using `SetRegulatoryConfig`.
- If Commissionee supports Time Synchronization Cluster server: Commissioner SHOULD configure UTC time (`SetUTCTime`), SHOULD set time zone (`SetTimeZone`) if TimeZone feature is supported, SHOULD set DST offsets (`SetDSTOffset`) if TimeZone feature is supported and `SetTimeZoneResponse` had `DSTOffsetRequired` = True, SHOULD set Default NTP server (`SetDefaultNTP`) if NTPClient feature is supported and `DefaultNTP` is null.

**Step 9 — TC Acknowledgement (conditional)**
If the Terms & Conditions Enhanced Setup Flow feature is supported by both device and Commissioner, and `TCAcknowledgementsRequired` is True, Commissioner SHALL present TC and propagate user responses via `SetTCAcknowledgements`.

**Step 10 — Device Attestation**
Commissioner SHALL establish authenticity of the Commissionee as a certified Matter device via the Device Attestation Procedure. If attestation fails, Commissioner MAY continue or terminate. Commissioner and Commissionee MAY both override failure in this step.

**Step 11 — CSR Request**
Commissioner SHALL request an operational CSR from the Commissionee using the `CSRRequest` command, which causes generation of a new operational key pair at the Commissionee.

**Step 12 — Operational Certificate Generation**
Commissioner SHALL generate or otherwise obtain an Operational Certificate containing an Operational ID after receiving the `CSRResponse`.

**Step 13 — Install Operational Credentials**
Commissioner SHALL install operational credentials using `AddTrustedRootCertificate` and `AddNOC` commands, and SHALL use `UpdateFabricLabel` to set a user-recognizable string. The `AdminVendorId` field of `AddNOC` SHALL be set to a value whose Vendor Schema in DCL contains the Commissioner manufacturer's name and information.

**Step 14 — Trusted Time Source (conditional)**
If Commissionee supports Time Synchronization Cluster and `TimeSyncClient` feature is supported, Commissioner SHOULD set a trusted time source via `SetTrustedTimeSource` if `TrustedTimeSource` is null and a trusted time source is available on the fabric. Commissioner SHOULD ensure the ACL grants Commissionee View privilege to the Time Synchronization cluster.

**Step 15 — Access Control Configuration (optional)**
Commissioner MAY configure the ACL on the Commissionee. Commissioner MAY read the Commissioning Access Restriction List to identify fabric restrictions. Administrator MAY invoke `ReviewFabricRestrictions` but SHALL wait until after commissioning completes before sending this command.

**Step 16–17 — Network Commissioning (conditional)**
If Commissionee both supports and requires it, Commissioner SHALL configure the operational network using commands such as `AddOrUpdateWiFiNetwork` or `AddOrUpdateThreadNetwork`. Commissioner MAY use `ScanNetworks` to learn visible networks. Commissioner SHOULD configure Per-Device Credentials if supported.

**Step 18 — Connect to Operational Network**
Commissioner SHALL trigger Commissionee to connect to the operational network via `ConnectNetwork` unless already connected. If `IsCommissioningWithoutPower` is true: fail-safe timer countdown SHALL be paused and connection SHALL be deferred until device is powered.

**Step 19 — Wait for Power (conditional)**
If `IsCommissioningWithoutPower` is true, Commissioner SHALL wait for indication that the Commissionee is capable of joining the operational network.

**Step 20 — Operational Discovery**
Commissionee SHALL use Operational Discovery to become discoverable on the operational network. An Administrator configured in the ACL SHALL use Operational Discovery to discover the Commissionee.

**Step 21 — CASE Session and CommissioningComplete**
Administrator SHALL open a CASE session with the Commissionee over the operational network. Administrator SHALL invoke `CommissioningComplete`. A success response ends commissioning. For NFC-based commissioning, Commissionee SHALL update the operational discovery service per Section 4.3.2.5 Subtypes.

**Step 22 — NFC Rediscovery (NTL only)**
After using NFC Transport Layer (NTL), Commissioner SHALL rediscover on the operational network the supported endpoints, clusters, attributes, and events of the Commissionee.

**Channel Termination**
- Concurrent connection flow: commissioning channel SHALL terminate after successful step 21.
- Non-concurrent connection flow: commissioning channel SHALL terminate after successful step 17.
- PASE-derived encryption keys SHALL be deleted when the commissioning channel terminates.
- PASE session SHALL be terminated by both Commissioner and Commissionee once `CommissioningComplete` is received by the Commissionee.

---

## 3. Normative Requirements

### 5.5 Commissioning Flows

- [5.260] "The two connections MAY either be on the same or on different networking interfaces."

- [5.264] "Commissioning SHALL be a time-bound process that completes before expiration of a fail-safe timer. The fail-safe timer SHALL be set at the beginning of commissioning. If the fail-safe timer expires prior to commissioning completion, the Commissioner and Commissionee SHALL terminate commissioning. Successful completion of commissioning SHALL disarm the fail-safe timer."

- [5.265] "A Commissionee that is ready to be commissioned SHALL accept the request to establish a PASE session with the first Commissioner that initiates the request. When a Commissioner is either in the process of establishing a PASE session with the Commissionee or has successfully established a session, the Commissionee SHALL NOT accept any more requests for new PASE sessions until one of the following events occurs: session establishment fails, the successfully established PASE session is terminated on the commissioning channel (see Section 4.11.1.4, "CloseSession" in Section 4.11.1.3, "Secure Channel Status Report Messages"), the PASE session is established through NFC Transport Layer (NTL) and Commissionee receives a valid SELECT command"

- [5.266] "If the fail-safe timer is armed, the fail-safe timer SHALL be considered expired and the cleanup steps detailed in Section 11.10.7.2, "ArmFailSafe" SHALL be executed. If the commissioning window is still open, the Commissionee SHALL continue listening for commissioning requests."

- [5.267] "a Commissionee SHALL expect a PASE session to be established within 60 seconds of receiving the initial request. This means the Commissionee SHALL expect to receive the PAKE3 message within 60 seconds after sending a PBKDFParamResponse in response to a PBKDFParamRequest message from the Commissioner to establish a PASE session. If the PASE session is not established within the expected time window the Commissionee SHALL terminate the current session establishment using the INVALID_PARAMETER status code as described in Section 4.11.1.3, "Secure Channel Status Report Messages"."

- [5.270] Step 1: "The Commissioner initiating the commissioning SHALL have regulatory and fabric information available, and SHOULD have accurate date, time and timezone."

- [5.270] Step 2: "Commissioner and Commissionee SHALL establish a commissioning channel between each other after discovery or direct physical tap."

- [5.270] Step 3: "If the Commissioner and device support Section 5.7.4.1, "Terms and Conditions (TC) Acknowledgement", then the Commissioner SHALL obtain the Terms and Conditions to present to the user from Section 11.23.6.22, "EnhancedSetupFlowTCUrl"."

- [5.270] Step 4: "If the Commissioner and device support Section 5.7.4.1, "Terms and Conditions (TC) Acknowledgement", the Commissioner SHALL present the Terms and Conditions to the user following Section 5.7.4.1, "Terms and Conditions (TC) Acknowledgement", unless the Commissioner already has user provided responses that can be used."

- [5.270] Step 5: "If the Commissioner and device support Section 5.7.4.1, "Terms and Conditions (TC) Acknowledgement", and Terms and Conditions were presented in step 4, then the Commissioner SHALL receive the user responses to the provided terms for use in step 9."

- [5.270] Step 6: "Commissioner and Commissionee SHALL establish encryption keys with PASE (see Section 4.14.1, "Passcode-Authenticated Session Establishment (PASE)") on the commissioning channel. All subsequent messages on the commissioning channel are encrypted using PASE-derived encryption keys. Upon completion of PASE session establishment, the Commissionee SHALL autonomously arm the Fail-safe timer for a timeout of 60 seconds."

- [5.270] Step 7: "Commissioner SHALL re-arm the Fail-safe timer on the Commissionee to the desired commissioning timeout within 60 seconds of the completion of PASE session establishment, using the ArmFailSafe command. A Commissioner MAY obtain device information including guidance on the fail-safe value from the Commissionee by reading BasicCommissioningInfo attribute prior to invoking the Section 11.10.7.2, "ArmFailSafe" command."

- [5.270] Step 8 (regulatory): "If the Commissionee has at least one instance of the Network Commissioning cluster on any endpoint with either the WI (i.e. Wi-Fi) or TH (i.e. Thread) feature flags set in its FeatureMap, Commissioner SHALL configure regulatory information in the Commissionee using the Section 11.10.7.4, "SetRegulatoryConfig" command."

- [5.270] Step 8 (time): "The Commissioner SHOULD configure UTC time using the Section 11.17.9.1, "SetUTCTime" command." / "The Commissioner SHOULD set the time zone using the Section 11.17.9.3, "SetTimeZone" command, if the Section 11.17.5.1, "TimeZone" feature is supported." / "The Commissioner SHOULD set the DST offsets using the Section 11.17.9.5, "SetDSTOffset" command if the Section 11.17.5.1, "TimeZone" feature is supported, and the Section 11.17.9.4, "SetTimeZoneResponse" from the Commissionee had the DSTOffsetRequired field set to True." / "The Commissioner SHOULD set a Default NTP server using the Section 11.17.9.6, "SetDefaultNTP" command if the Section 11.17.5.2, "NTPClient" feature is supported and the Section 11.17.8.5, "DefaultNTP" attribute is null. If the current value is non-null, Commissioners MAY opt to overwrite the current value."

- [5.270] Step 9: "if Section 11.10.6.9, "TCAcknowledgementsRequired" is True, the Commissioner SHALL present them as documented in Terms and Conditions Acknowledgement. The user's responses SHALL be propagated back to the node with Section 11.10.7.8, "SetTCAcknowledgements"."

- [5.270] Step 10: "Commissioner SHALL establish the authenticity of the Commissionee as a certified Matter device (see Section 6.2.3, "Device Attestation Procedure")." / "If the Commissionee fails the Device Attestation Procedure, for any reason, the Commissioner MAY choose to either continue to the Commissioning, or terminate it, depending on implementation-dependent policies." / "Upon failure of the procedure, the Commissioner SHOULD warn the user that the Commissionee is not a fully trusted device, and MAY give the user the choice to authorize or deny the commissioning." / "Such a warning SHOULD contain as much information as the commissioner can provide about the Commissionee, and SHOULD be adapted to the reason of the failure." / "Commissioners SHOULD accept a Certification Declaration with certification_type =1 (provisional) and MAY inform the customer." / "If a Commissioner denies commissioning for any reason, it SHOULD notify the user of the reason with sufficient details for the user to understand the reason."

- [5.270] Step 11: "Commissioner SHALL request operational CSR from Commissionee using the CSRRequest command."

- [5.270] Step 12: "Commissioner SHALL generate or otherwise obtain an Operational Certificate containing Operational ID after receiving the CSRResponse command from the Commissionee."

- [5.270] Step 13: "Commissioner SHALL install operational credentials on the Commissionee using the Section 11.18.6.13, "AddTrustedRootCertificate" and AddNOC commands, and SHALL use the Section 11.18.6.11, "UpdateFabricLabel" command to set a string that the user can recognize and relate to this Commissioner/Administrator. The AdminVendorId field of the AddNOC command SHALL be set to a value for which the Vendor Schema in DCL contains the name and other information of the Commissioner's manufacturer."

- [5.270] Step 14: "the Commissioner SHOULD set a trusted time source using the Section 11.17.9.2, "SetTrustedTimeSource" command if the Section 11.17.5.4, "TimeSyncClient" feature is supported, the Section 11.17.8.4, "TrustedTimeSource" attribute is null and there is an available trusted time source on the fabric. The Commissioner SHOULD ensure the ACL on the TrustedTimeSource is set to grant the Commissionee View privilege to the Time Synchronization cluster."

- [5.270] Step 15: "The Administrator SHALL wait until after commissioning completes before sending this command." (re: ReviewFabricRestrictions)

- [5.270] Step 16: "If the Commissionee both supports it and requires it, the Commissioner SHALL configure the operational network at the Commissionee using commands such as AddOrUpdateWiFiNetwork and AddOrUpdateThreadNetwork." / "the commissioner SHOULD configure the commissionee with Per-Device Credentials if supported by the commissionee and the operational Wi-Fi network."

- [5.270] Step 17: "The Commissioner SHALL trigger the Commissionee to connect to the operational network using ConnectNetwork command unless the Commissionee is already on the desired operational network. In case the device is not fully powered before step 19, the Commissionee SHALL signal this to the Commissioner via the IsCommissioningWithoutPower attribute, the fail-safe timer countdown SHALL be paused, and the connection to the operational network SHALL be deferred to start automatically as soon as the device is powered up and running."

- [5.270] Step 18: "If the IsCommissioningWithoutPower attribute is true, the Commissioner SHALL wait for an indication that the commissionee is in a power configuration that is capable of joining an operational network before proceeding to the next step."

- [5.270] Step 19: "The Commissionee SHALL use Section 4.3.2, "Operational Discovery" to be discoverable on the operational network. An Administrator configured in the ACL of the Commissionee by the Commissioner SHALL use Section 4.3.2, "Operational Discovery" to discover the Commissionee."

- [5.270] Step 20: "The Administrator SHALL open a CASE (see Section 4.14.2, "Certificate Authenticated Session Establishment (CASE)") session with the Commissionee over the operational network."

- [5.270] Step 21: "The Administrator having established a CASE session with the Commissionee over the operational network in the previous steps SHALL invoke the CommissioningComplete command. A success response after invocation of the CommissioningComplete command ends the commissioning process. In case of NFC-based commissioning, the Commissionee SHALL update the operational discovery service according to the requirement in Section 4.3.2.5, "Subtypes"."

- [5.270] Step 22 (NTL): "the commissioner SHALL rediscover on the operational network the supported endpoints, clusters, attributes and events of the commissionee."

- [5.270] "Unless indicated otherwise, a commissioner SHALL complete a step, including waiting for any responses to commands it sends in that step, before moving on to the next step."

- [5.273] "In concurrent connection commissioning flow the commissioning channel SHALL terminate after successful step 21 (CommissioningComplete command invocation). In non-concurrent connection commissioning flow the commissioning channel SHALL terminate after successful step 17 (trigger joining of operational network at Commissionee). The PASE-derived encryption keys SHALL be deleted when commissioning channel terminates. The PASE session SHALL be terminated by both Commissioner and Commissionee once the CommissioningComplete command is received by the Commissionee."

- [5.274] "In both concurrent connection commissioning flow and non-concurrent connection commissioning flow, the Commissioner MAY choose to continue commissioning and override the failure in step 10 (Commissionee attestation)."

### 5.5.1 Commissioning Flows Error Handling

- [5.276] "If a Commissionee requires network commissioning, the Commissioner SHOULD attempt to configure the primary network interface on the Root Node endpoint initially."

- [5.277] "If the initial attempt fails for networking-related reasons, then the Commissioner SHOULD attempt to configure secondary network interfaces via additional endpoints that have a server instance of the Network Commissioning cluster, if such endpoints exist."

- [5.278] "Before attempting to configure any such alternative interface, the commissioner SHALL revert network configuration changes made as part of the preceding unsuccessful configuration attempt, specifically the Section 11.9.7.6, "RemoveNetwork" command SHALL be used to remove any Thread or Wi-Fi network configurations added by such an unsuccessful attempt."

- [5.280] "Whenever the Fail-Safe timer is armed, Commissioners and Administrators SHALL NOT consider any cluster operation to have timed-out before waiting at least 30 seconds for a valid response from the cluster server."

- [5.281] "When set, this argument SHALL be used to update the value of the Breadcrumb Attribute as a side-effect of successful execution of those commands. On command failures, the Breadcrumb Attribute SHALL remain unchanged."

- [5.282] "In concurrent connection commissioning flow, the failure of any of the steps 2 through 15 SHALL result in the Commissioner and Commissionee returning to step 2 (device discovery and commissioning channel establishment) and repeating each step. The failure of any of the steps 16 through 21 in concurrent connection commissioning flow SHALL result in the Commissioner and Commissionee returning to step 16 (configuration of operational network information), and MAY result in an attempt to configure secondary network interfaces. In the case of failure of any of the steps 16 through 21 in concurrent connection commissioning flow, the Commissioner and Commissionee SHALL reuse the existing PASE-derived encryption keys over the commissioning channel and all steps up to and including step 15 are considered to have been successfully completed."

- [5.283] "In non-concurrent connection commissioning flow, the failure of any of the steps 2 through 21 SHALL result in the Commissioner and Commissionee returning to step 2 (device discovery and commissioning channel establishment) and repeating each step."

- [5.285] "In both concurrent connection commissioning flow and non-concurrent connection commissioning flow, the Commissionee SHALL exit Commissioning Mode after 20 failed attempts."

- [5.286] "Once a Commissionee has been successfully commissioned by a Commissioner into its fabric, the commissioned Node SHALL NOT accept any more PASE requests until any one of the following conditions is met: Device is factory-reset. Device enters commissioning mode."

---

## 4. Message Formats & Data Structures

The provided spec text does not define wire-level TLV encodings, frame field layouts, or numeric opcode/status-code values within these sections. References to message types and commands appear only by name:

- **PBKDFParamRequest** / **PBKDFParamResponse** / **PAKE3**: PASE handshake messages (referenced in timing rule at [5.267]; wire format defined in Section 4.14.1).
- **INVALID_PARAMETER**: status code used to terminate a PASE session establishment that exceeds the 60-second window ([5.267]; format defined in Section 4.11.1.3).
- **CloseSession**: status message defined in Section 4.11.1.4, used to terminate the PASE session on the commissioning channel ([5.265], [5.266]).
- **SELECT command**: NFC command that interrupts NTL-based commissioning ([5.265], [5.266], [5.268]).
- **Breadcrumb argument**: present on certain commissioning and administration request commands; updates `Breadcrumb` attribute on success, remains unchanged on failure ([5.281]).
- **`ExpiryLengthSeconds` field set to 0**: passed in `ArmFailSafe` to immediately expire the fail-safe ([5.284]).
- **`AdminVendorId` field of `AddNOC`**: SHALL be set to a value for which the Vendor Schema in DCL contains the Commissioner manufacturer's name ([5.270] step 13).
- **`DSTOffsetRequired` field in `SetTimeZoneResponse`**: drives whether `SetDSTOffset` is invoked ([5.270] step 8).

No additional wire-format or TLV tables are present in the provided spec text.

---

## 5. Security Considerations

- For additional security requirements related to commissioning flows, refer to Section 13.6, "Security Best Practices" ([5.258]). (Content of that section is not included in the provided spec text.)

- The commissioning channel is protected by PASE-derived encryption keys from step 6 onward: "All subsequent messages on the commissioning channel are encrypted using PASE-derived encryption keys" ([5.270] step 6).

- PASE-derived encryption keys SHALL be deleted when the commissioning channel terminates ([5.273]).

- The Commissionee locks out further PASE session requests once one is in progress or established, preventing parallel takeover attempts ([5.265]). The lock is released on session failure, CloseSession, or NFC SELECT.

- To prevent indefinite lockout, the Commissionee enforces a 60-second PASE establishment window, terminating with INVALID_PARAMETER if PAKE3 is not received in time ([5.267]).

- The Commissionee autonomously arms a 60-second fail-safe immediately upon PASE completion to guard against a Commissioner aborting without arming the fail-safe ([5.270] step 6).

- Device Attestation (step 10) establishes authenticity of the Commissionee as a certified Matter device. Commissioner SHOULD warn the user if attestation fails and SHOULD adapt the warning to the specific failure reason (expired certificate vs. revoked PAI, etc.) ([5.270] step 10).

- Operational credentials are installed over the PASE-encrypted commissioning channel; the CASE session for `CommissioningComplete` runs over the operational network, providing a second authentication layer ([5.270] steps 13, 20–21).

- Once commissioned, a Node SHALL NOT accept PASE requests until factory-reset or re-entry into commissioning mode, preventing re-commissioning attacks ([5.286]).

- In NFC-based commissioning, SELECT command interruption is under Commissioner control, allowing the Commissioner to filter accidental taps or fire based on an internal watchdog ([5.268]).

---

## 6. Error Handling & Timing

### Timing Requirements

- **60 seconds**: Commissionee autonomously arms fail-safe for 60 seconds upon PASE completion ([5.270] step 6).
- **60 seconds**: Commissioner SHALL re-arm the fail-safe within 60 seconds of PASE completion ([5.270] step 7).
- **60 seconds**: Commissionee SHALL expect PAKE3 to be received within 60 seconds of sending PBKDFParamResponse; otherwise SHALL terminate using INVALID_PARAMETER ([5.267]).
- **30 seconds minimum**: Whenever the Fail-Safe timer is armed, Commissioners and Administrators SHALL NOT consider any cluster operation to have timed-out before waiting at least 30 seconds for a valid response ([5.280]). Some commands MAY require longer per their cluster specification.

### Fail-Safe Expiry

- If the fail-safe timer expires before commissioning completion, Commissioner and Commissionee SHALL terminate commissioning ([5.264]).
- A CloseSession message or NFC SELECT interruption SHALL cause the fail-safe to be considered expired; cleanup steps in Section 11.10.7.2 SHALL be executed ([5.266]).
- Commissioners that need to restart from step 2 MAY immediately expire the fail-safe by invoking `ArmFailSafe` with `ExpiryLengthSeconds` = 0; otherwise they must wait for the current timer to expire before the Commissionee will accept PASE again ([5.284]).

### Retry and Restart Behavior

- **Concurrent connection flow, steps 2–15 failure**: Commissioner and Commissionee SHALL return to step 2 and repeat each step ([5.282]).
- **Concurrent connection flow, steps 16–21 failure**: Commissioner and Commissionee SHALL return to step 16; MAY attempt secondary network interfaces; SHALL reuse existing PASE-derived keys; steps up to and including 15 are considered complete ([5.282]).
- **Non-concurrent connection flow, any step 2–21 failure**: Commissioner and Commissionee SHALL return to step 2 ([5.283]).
- **20 failed attempts**: Commissionee SHALL exit Commissioning Mode ([5.285]).

### Network Configuration Rollback

- Before attempting a secondary network interface, Commissioner SHALL revert prior configuration changes; `RemoveNetwork` command SHALL be used to remove any Thread or Wi-Fi network configurations added by the unsuccessful attempt ([5.278]).

### Breadcrumb Error Semantics

- On command failures, the Breadcrumb Attribute SHALL remain unchanged; it is only updated on successful execution ([5.281]).

### Post-Commissioning PASE Lock

- After successful commissioning, the Node SHALL NOT accept PASE requests until factory-reset or re-entry into commissioning mode ([5.286]).