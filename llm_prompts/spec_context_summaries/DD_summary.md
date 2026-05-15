# Matter Spec Summary: DD Protocol

**Source sections matched:** 94  
**Source chars sent to LLM:** 73,730  
**Generated:** 2026-04-28 17:26:22  
**Summary words:** 8,153  

---

## 1. Overview

The provided spec text covers commissioning-related clusters and flows within the Matter Service and Device Management specification. Three primary clusters are described:

- **General Commissioning Cluster** (Cluster ID `0x0030`, PICS: `CGEN`): Manages basic commissioning lifecycle, including fail-safe timer management, regulatory configuration, and commissioning completion. It also hosts the Enhanced Setup Flow Terms & Conditions (TC) feature and the Network Recovery feature.
- **Network Commissioning Cluster** (Cluster ID `0x0031`, PICS: `CNET`): Associates a Node with, or manages, one or more network interfaces. Supported interface types are Wi-Fi (IEEE 802.11-2020), Ethernet (802.3), and Thread (802.15.4). Each cluster instance applies to a single network interface instance.
- **Administrator Commissioning Cluster** (Cluster ID `0x003C`, PICS: `CADMIN`): Used to trigger a Node to allow a new Administrator to commission it. Defines two commissioning methods: Basic Commissioning (BCM, optional) and Enhanced Commissioning (ECM, mandatory).

Supporting material includes DCL DeviceModel schema fields for commissioning mode hints, Joint Fabric Administrator commissioning window commands, and the Commissioner Control Cluster approval flow.

All three clusters are classified as **Utility** role with **Node** scope.

---

## 2. Protocol Details

### General Commissioning — Fail-Safe Mechanism

Commissioning is protected by a **Fail-Safe timer**. When first armed via `ArmFailSafe`, a **Fail Safe Context** is created on the receiver tracking:
- The fail-safe timer duration.
- The state of all Network Commissioning Networks attribute configurations, to allow recovery of connectivity after Fail-Safe expiry.
- Whether an `AddNOC` or `UpdateNOC` command has taken place.
- A fabric-index for fabric-scoping of the context, starting at the accessing fabric index for the `ArmFailSafe` command, updated with the Fabric Index from a successful `AddNOC` or `UpdateNOC`.
- The operational credentials associated with any Fabric whose configuration is affected by `UpdateNOC`.
- Optionally: the previous state of non-fabric-scoped data mutated during the fail-safe period.

A second **Cumulative Fail Safe Context (CFSC) timer** is created at context creation, expiring at `MaxCumulativeFailsafeSeconds`. The CFSC timer SHALL NOT be extended or modified on subsequent `ArmFailSafe` invocations. Upon CFSC expiry, cleanup equivalent to fail-safe timer expiration is executed. Termination of the session prior to CFSC expiry deletes the CFSC timer.

`AddNOC` can only be invoked once per contiguous non-expiring fail-safe timer period, and only if no `UpdateNOC` was previously processed in the same period. `UpdateNOC` can only be invoked once per contiguous non-expiring fail-safe timer period, only over CASE, and only if no `AddNOC` was previously processed.

Commissioning completes when the `CommissioningComplete` command is successfully invoked over a CASE session by the fabric associated with the current Fail-Safe context.

### General Commissioning — Commissioning Methods

- **Concurrent connection flow**: supported if `SupportsConcurrentConnection` attribute is `true`.
- **Non-concurrent connection flow**: the only mode if `SupportsConcurrentConnection` is `false`.

### General Commissioning — Terms & Conditions (TC Feature)

The TC feature (`TermsAndConditions`, bit 0) supports Enhanced Setup Flow Terms & Conditions acknowledgement. Relevant attributes: `TCAcceptedVersion`, `TCMinRequiredVersion`, `TCAcknowledgements`, `TCAcknowledgementsRequired`, `TCUpdateDeadline`. The `SetTCAcknowledgements` command is used to record user responses.

### General Commissioning — Network Recovery Flow

The Network Recovery feature (`NetworkRecovery`, bit 1) uses the `RecoveryIdentifier` attribute (random 64-bit value, reset on factory reset) for device advertisement, and `NetworkRecoveryReason` attribute to record the reason the flow was triggered.

### Administrator Commissioning — Commissioning Window Lifecycle

Only one commissioning window can be active at a time. Windows are opened via `OpenCommissioningWindow` (ECM) or `OpenBasicCommissioningWindow` (BCM), and closed via `RevokeCommissioning` or upon expiry/commissioning completion. `WindowStatus` tracks the current state. On initial commissioning, `WindowStatus` is `WindowNotOpen`.

When an ICD receives `OpenCommissioningWindow` or `OpenBasicCommissioningWindow`, it enters Active Mode and remains there as long as a commissioning window is open or a fail-safe timer is armed.

### Network Commissioning — Interface Types and Identity

Each Network Commissioning Cluster instance manages one network interface. Networks are uniquely identified by `NetworkID`:
- SSID for Wi-Fi
- Extended PAN ID (XPAN ID) for Thread
- Network interface instance name for Ethernet

### Commissioner Control — Approval Flow

A client sends `RequestCommissioningApproval` (requires CASE session), the server responds with SUCCESS and later generates a `CommissioningRequestResult` event. Upon receipt of that event, the client may proceed to commission the node via `CommissionNode` (not fully described in provided text) and the server issues `ReverseOpenCommissioningWindow`.

---

## 3. Normative Requirements

### 11.10 General Commissioning Cluster — General

- [11.436] "This cluster SHALL support the FeatureMap bitmap attribute as defined below."
- [11.478] "For all client-to-server commands in this cluster, if the client deems that it has timed-out in receiving the corresponding response command to any request, the corresponding step in the commissioning flow SHALL be considered to have failed, with the error handled as described in Section 5.5.1, 'Commissioning Flows Error Handling'."

### 11.10.4.1 Enhanced Setup Flow Terms & Conditions Feature

- [11.437] "Support for this feature is limited to nodes that use Commissioning Custom Flow."

### 11.10.5.2 RegulatoryLocationTypeEnum

- [11.442] "the maximum value of the enumeration SHALL be less than 15."

### 11.10.5.4 BasicCommissioningInfo — FailSafeExpiryLengthSeconds Field

- [11.445] "This field SHALL contain a conservative initial duration (in seconds) to set in the FailSafe for the commissioning flow to complete successfully."
- [11.445] "This value, if used in the Section 11.10.7.2, 'ArmFailSafe' command's ExpiryLengthSeconds field SHOULD allow a Commissioner to proceed with a nominal commissioning without having to-rearm the fail-safe, with some margin."

### 11.10.5.4 BasicCommissioningInfo — MaxCumulativeFailsafeSeconds Field

- [11.446] "This field SHALL contain a conservative value in seconds denoting the maximum total duration for which a fail safe timer can be re-armed."
- [11.447] "The value of this field SHALL be greater than or equal to the FailSafeExpiryLengthSeconds."
- [11.447] "it is RECOMMENDED that the value of this field be aligned with the initial Section 5.4.2.3, 'Announcement Duration' and default to 900 seconds."

### 11.10.6.1 Breadcrumb Attribute

- [11.448] "This attribute allows for the storage of a client-provided small payload which Administrators and Commissioners MAY write and then subsequently read, to keep track of their own progress."
- [11.448] "This MAY be used by the Commissioner to avoid repeating already-executed actions upon re-establishing a commissioning link after an error."
- [11.449] "On start/restart of the server, such as when a device is power-cycled, this attribute SHALL be reset to zero."

### 11.10.6.4 LocationCapability Attribute

- [11.456] "For Nodes without radio network interfaces (e.g. Ethernet-only devices), the value IndoorOutdoor SHALL always be used."

### 11.10.6.5 SupportsConcurrentConnection Attribute

- [11.458] "This attribute SHALL indicate whether this device supports 'concurrent connection flow' commissioning mode."

### 11.10.6.6 TCAcceptedVersion Attribute

- [11.459] "This attribute SHALL indicate the last version of the T&Cs for which the device received user acknowledgements. On factory reset this field SHALL be reset to 0."
- [11.460] "the manufacturer-provided means for obtaining user consent SHALL ensure that this attribute is set to a value which is greater than or equal to TCMinRequiredVersion before returning the user back to the originating Commissioner."

### 11.10.6.7 TCMinRequiredVersion Attribute

- [11.461] "This attribute SHALL indicate the minimum version of the texts presented by the Enhanced Setup Flow that need to be accepted by the user for this device. This attribute MAY change as the result of an OTA update."
- [11.462] "If an event such as a software update causes TCAcceptedVersion to become less than TCMinRequiredVersion, then the device SHALL update TCAcknowledgementsRequired to True so that an administrator can detect that a newer version of the texts needs to be presented to the user."

### 11.10.6.8 TCAcknowledgements Attribute

- [11.463] "This attribute SHALL indicate the user's response to the presented terms."
- [11.464] "Whenever a user provides responses to newly presented terms and conditions, this attribute SHALL be updated with the latest responses."
- [11.464] "On a factory reset this field SHALL be reset with all bits set to 0."

### 11.10.6.9 TCAcknowledgementsRequired Attribute

- [11.465] "This attribute SHALL indicate whether SetTCAcknowledgements is currently required to be called with the inclusion of mandatory terms accepted."
- [11.466] "This attribute MAY be present and False in the case where no terms and conditions are currently mandatory to accept for CommissioningComplete command to succeed."
- [11.467] "This attribute MAY appear, or become True after commissioning (e.g. due to a firmware update) to indicate that new Terms & Conditions are available that the user must accept."
- [11.468] "Upon Factory Data Reset, this attribute SHALL be set to a value of True."
- [11.469] "the manufacturer-provided means for obtaining user consent SHALL ensure that this attribute is set to False before returning the user back to the original Commissioner."

### 11.10.6.10 TCUpdateDeadline Attribute

- [11.470] "This attribute SHALL indicate the System Time in seconds when any functionality limitations will begin due to a lack of acceptance of updated Terms and Conditions."
- [11.471] "A null value indicates that there is no pending deadline for updated TC acceptance."

### 11.10.6.11 RecoveryIdentifier Attribute

- [11.472] "This attribute SHALL contain the identifier to be included in the advertisements used during the Network Recovery Flow."
- [11.473] "The attribute SHALL contain a random 64-bit value, that value SHALL be reset on factory reset and SHALL remain unchanged until a next factory reset."
- [11.473] "This value SHOULD be obtained through Crypto_DRBG(len = 64)."

### 11.10.6.12 NetworkRecoveryReason Attribute

- [11.474] "This attribute SHALL contain the primary reason that triggered the Network Recovery flow and its associated advertisements. This attribute SHALL be null when the Node is not undergoing a Network Recovery flow."

### 11.10.6.13 IsCommissioningWithoutPower Attribute

- [11.475] "The server SHALL set this attribute to true if and only if is currently operating on the commissioning channel but cannot operate on the operational channel because it is not powered."

### 11.10.7.2 ArmFailSafe Command

- [11.481] "Success or failure of this command SHALL be communicated by the ArmFailSafeResponse command, unless some data model validations caused a failure status code to be issued during the processing of the command."
- [11.482] "If the fail-safe timer is not currently armed, the commissioning window is open, and the command was received over a CASE session, the command SHALL leave the current fail-safe state unchanged and immediately respond with an ArmFailSafeResponse containing an ErrorCode value of BusyWithOtherAdmin."
- [11.483] "If ExpiryLengthSeconds is 0 and the fail-safe timer was already armed and the accessing fabric matches the Fabric currently associated with the fail-safe context, then the fail-safe timer SHALL be immediately expired."
- [11.483] "If ExpiryLengthSeconds is 0 and the fail-safe timer was not armed, then this command invocation SHALL lead to a success response with no side-effects against the fail-safe context."
- [11.483] "If ExpiryLengthSeconds is non-zero and the fail-safe timer was not currently armed, then the fail-safe timer SHALL be armed for that duration."
- [11.483] "If ExpiryLengthSeconds is non-zero and the fail-safe timer was currently armed, and the accessing Fabric matches the fail-safe context's associated Fabric, then the fail-safe timer SHALL be re-armed to expire in ExpiryLengthSeconds."
- [11.483] "Otherwise, the command SHALL leave the current fail-safe state unchanged and immediately respond with ArmFailSafeResponse containing an ErrorCode value of BusyWithOtherAdmin."
- [11.484] "The value of the Breadcrumb field SHALL be written to the Breadcrumb on successful execution of the command."
- [11.485] "If the receiver restarts unexpectedly (e.g., power interruption, software crash, or other reset) the receiver SHALL behave as if the fail-safe timer expired and perform the sequence of clean-up steps listed below."
- [11.486] "On successful execution of the command, the ErrorCode field of the ArmFailSafeResponse SHALL be set to OK."

### 11.10.7.2.1 Fail Safe Context

- [11.487] "When first arming the fail-safe timer, a 'Fail Safe Context' SHALL be created on the receiver."
- [11.489] "On creation of the Fail Safe Context a second timer SHALL be created to expire at MaxCumulativeFailsafeSeconds."
- [11.489] "This Cumulative Fail Safe Context timer (CFSC timer) serves to limit the lifetime of any particular Fail Safe Context; it SHALL NOT be extended or modified on subsequent invocations of ArmFailSafe associated with this Fail Safe Context."
- [11.489] "Upon expiry of the CFSC timer, the receiver SHALL execute cleanup behavior equivalent to that of fail-safe timer expiration."
- [11.489] "Termination of the session prior to the expiration of that timer for any reason (including a successful end of commissioning or an expiry of a fail-safe timer) SHALL also delete the CFSC timer."

### 11.10.7.2.2 Behavior on Expiry of Fail-Safe Timer

- [11.490] "If the fail-safe timer expires before the Section 11.10.7.6, 'CommissioningComplete' command is successfully invoked, the following sequence of clean-up steps SHALL be executed, in order, by the receiver:"
  - "Terminate any open PASE secure session by clearing any associated Section 4.13.3.1, 'Secure Session Context' at the Server."
  - "Revoke the temporary administrative privileges granted to any open PASE session."
  - "If an AddNOC or UpdateNOC command has been successfully invoked, terminate all CASE sessions associated with the Fabric whose Fabric Index is recorded in the Fail-Safe context."
  - "Reset the configuration of all Network Commissioning Section 11.9.6.2, 'Networks' attribute to their state prior to the Fail-Safe being armed."
  - "If an UpdateNOC command had been successfully invoked, revert the state of operational key pair, NOC and ICAC for that Fabric to the state prior to the Fail-Safe timer being armed."
  - "If an AddNOC command had been successfully invoked, achieve the equivalent effect of invoking the RemoveFabric command against the fabric-index stored in the Fail-Safe Context for the Fabric Index that was the subject of the AddNOC command. This SHALL remove all associations to that Fabric including all fabric-scoped data, and MAY possibly factory-reset the device depending on current device state. This SHALL only apply to Fabrics added during the fail-safe period as the result of the AddNOC command."
  - "If the CSRRequest command had been successfully invoked, but no AddNOC or UpdateNOC command had been successfully invoked, then the new operational key pair temporarily generated for the purposes of NOC addition or update SHALL be removed."
  - "Remove any RCACs added by the AddTrustedRootCertificate command that are not currently referenced by any entry in the Fabrics attribute."
  - "Reset the Section 11.10.6.1, 'Breadcrumb' attribute to zero."
  - "Optionally: if no factory-reset resulted from the previous steps, it is RECOMMENDED that the Node rollback the state of all non fabric-scoped data present in the Fail-Safe context."

### 11.10.7.3 ArmFailSafeResponse Command

- [11.492] "This field SHALL contain the result of the operation, based on the behavior specified in the functional description of the ArmFailSafe command."
- [11.479] "Some response commands have a DebugText argument which SHOULD NOT be presented directly in user interfaces."

### 11.10.7.4 SetRegulatoryConfig Command

- [11.495] "This SHALL add or update the regulatory configuration in the RegulatoryConfig Attribute to the value provided in the NewRegulatoryConfig field."
- [11.496] "Success or failure of this command SHALL be communicated by the SetRegulatoryConfigResponse command, unless some data model validations caused a failure status code to be issued during the processing of the command."
- [11.497] "The CountryCode field SHALL conforms to ISO 3166-1 alpha-2 and SHALL be used to set the Location attribute reflected by the Basic Information Cluster."
- [11.498] "setting regulatory information outside a valid country or location SHALL still set the Location attribute reflected by the Basic Information Cluster configuration, but the SetRegulatoryConfigResponse replied SHALL have the ErrorCode field set to ValueOutsideRange error."
- [11.499] "If the LocationCapability attribute is not Indoor/Outdoor and the NewRegulatoryConfig value received does not match either the Indoor or Outdoor fixed value in LocationCapability, then the SetRegulatoryConfigResponse replied SHALL have the ErrorCode field set to ValueOutsideRange error and the RegulatoryConfig attribute and associated internal radio configuration SHALL remain unchanged."
- [11.500] "If the LocationCapability attribute is set to Indoor/Outdoor, then the RegulatoryConfig attribute SHALL be set to match the NewRegulatoryConfig field."
- [11.501] "On successful execution of the command, the ErrorCode field of the SetRegulatoryConfigResponse SHALL be set to OK."
- [11.502] "The Breadcrumb field SHALL be used to atomically set the Breadcrumb attribute on success of this command, when SetRegulatoryConfigResponse has the ErrorCode field set to OK. If the command fails, the Breadcrumb attribute SHALL be left unchanged."

### 11.10.7.6 CommissioningComplete Command

- [11.507] "Success or failure of this command SHALL be communicated by the CommissioningCompleteResponse command, unless some data model validations caused a failure status code to be issued during the processing of the command."
- [11.509] "An ErrorCode of NoFailSafe SHALL be responded to the invoker if the CommissioningComplete command was received when no Fail-Safe context exists."
- [11.510] "If Terms and Conditions are required, then an ErrorCode of TCAcknowledgementsNotReceived SHALL be responded to the invoker if the user acknowledgements to the required Terms and Conditions have not been provided."
- [11.511] "An ErrorCode of InvalidAuthentication SHALL be responded to the invoker if the CommissioningComplete command was received outside a CASE session (e.g., over Group messaging, or PASE session after AddNOC), or if the accessing fabric is not the one associated with the ongoing Fail-Safe context."
- [11.512] "This command SHALL only result in success with an ErrorCode value of OK in the CommissioningCompleteResponse if received over a CASE session and the accessing fabric index matches the Fabric Index associated with the current Fail-Safe context."
- [11.513] "On successful execution of the CommissioningComplete command, where the CommissioningCompleteResponse has an ErrorCode of OK, the following actions SHALL be undertaken on the Server:"
  - "The Fail-Safe timer associated with the current Fail-Safe context SHALL be disarmed."
  - "The commissioning window at the Server SHALL be closed."
  - "Any temporary administrative privileges automatically granted to any open PASE session SHALL be revoked."
  - "The Secure Session Context of any PASE session still established at the Server SHALL be cleared."
  - "The Breadcrumb attribute SHALL be reset to zero."
- [11.514] "After receipt of a CommissioningCompleteResponse with an ErrorCode value of OK, a client cannot expect any previously established PASE session to still be usable, due to the server having cleared such sessions."

### 11.10.7.8 SetTCAcknowledgements Command

- [11.519] "This field SHALL contain the version of the Enhanced Setup Flow Terms & Conditions that were presented to the user."
- [11.520] "This field SHALL contain the user responses to the Enhanced Setup Flow Terms & Conditions as a map where each bit set in the bitmap corresponds to an accepted term in the file located at Section 11.23.6.22, 'EnhancedSetupFlowTCUrl'."
- [11.521] "This command SHALL copy the user responses and accepted version to the presented Enhanced Setup Flow Terms & Conditions from the values provided in the TCUserResponse and TCVersion fields to the TCAcknowledgements Attribute and the TCAcceptedVersion Attribute fields respectively."
- [11.522] "This command SHALL result in success with an ErrorCode value of OK in the SetTCAcknowledgementsResponse if all required terms were accepted by the user."
- [11.523] "If the TCVersion field is less than the TCMinRequiredVersion, then the ErrorCode of TCMinVersionNotMet SHALL be returned and TCAcknowledgements SHALL remain unchanged."
- [11.524] "If TCVersion is greater than or equal to TCMinRequiredVersion, but the TCUserResponse value indicates that not all required terms were accepted by the user, then the ErrorCode of RequiredTCNotAccepted SHALL be returned and TCAcknowledgements SHALL remain unchanged."

### 11.17 Time Synchronization at Commissioning

- [11.914] "During commissioning the Commissioner SHOULD set the UTCTime, and set up the TrustedTimeSource, DefaultNTP, TimeZone and DSTOffsets as required."
- [11.914] "the commissioner MAY opt to not set the time so the node SHOULD NOT depend on having time during commissioning."

### 11.19 Administrator Commissioning Cluster — General

- [11.1101] "Enhanced Commissioning which SHALL be supported and is described in Section 5.6.3, 'Enhanced Commissioning Method (ECM)'."
- [11.1103] "If the Administrator Commissioning Cluster server instance is present on an endpoint with the Root Node device type in the Descriptor cluster DeviceTypeList, then: The Commissioning Window SHALL be opened or closed on the node that the Root Node endpoint is on. The attributes SHALL indicate the state of the node that the Root Node endpoint is on."
- [11.1104] "If the Administrator Commissioning Cluster server instance is present on an endpoint with the Bridged Node device type in the Descriptor cluster DeviceTypeList, then: The Commissioning Window SHALL be opened or closed on the node represented by the Bridged Node. The attributes SHALL indicate the state of the node that is represented by the Bridged Node."
- [11.1106] "This cluster SHALL support the FeatureMap bitmap attribute as defined below."
- [11.1116] "Only one commissioning window can be active at a time. If a Node receives another open commissioning command when an Open Commissioning Window is already active, it SHALL return a failure response."

### 11.19.7 Administrator Commissioning Attributes

- [11.1109] "This attribute SHALL indicate whether a new Commissioning window has been opened by an Administrator."
- [11.1110] "This attribute SHALL revert to WindowNotOpen upon expiry of a commissioning window."
- [11.1110] "this attribute SHALL be set to WindowNotOpen on initial commissioning."
- [11.1111] "When the WindowStatus attribute is not set to WindowNotOpen, this attribute SHALL indicate the FabricIndex associated with the Fabric scoping of the Administrator that opened the window."
- [11.1112] "If, during an open commissioning window, the fabric for the Administrator that opened the window is removed, then this attribute SHALL be set to null."
- [11.1113] "When the WindowStatus attribute is set to WindowNotOpen, this attribute SHALL be set to null."
- [11.1114] "When the WindowStatus attribute is not set to WindowNotOpen, this attribute SHALL indicate the Vendor ID associated with the Fabric scoping of the Administrator that opened the window."
- [11.1114] "This field SHALL match the VendorID field of the Fabrics attribute list entry associated with the Administrator having opened the window, at the time of window opening."
- [11.1114] "If the fabric for the Administrator that opened the window is removed from the node while the commissioning window is still open, this attribute SHALL NOT be updated."
- [11.1115] "When the WindowStatus attribute is set to WindowNotOpen, this attribute SHALL be set to null."

### 11.19.8.1 OpenCommissioningWindow Command

- [11.1117] "The current Administrator SHALL specify a timeout value for the duration of the OpenCommissioningWindow command."
- [11.1118] "When the OpenCommissioningWindow command expires or commissioning completes, the Node SHALL remove the Passcode by deleting the PAKE passcode verifier as well as stop publishing the DNS-SD record."
- [11.1120] "On completion, the command SHALL return a cluster specific status code from the Section 11.19.6, 'Status Codes' below reflecting success or reasons for failure of the operation."
- [11.1120] "The new Administrator SHALL discover the Node on the IP network using DNS-based Service Discovery (DNS-SD) for commissioning."
- [11.1121] "If any format or validity errors related to the PAKEPasscodeVerifier, Iterations or Salt arguments arise, this command SHALL fail with a cluster specific status code of PAKEParameterError."
- [11.1122] "If a commissioning window is already currently open, this command SHALL fail with a cluster specific status code of Busy."
- [11.1123] "If the fail-safe timer is currently armed, this command SHALL fail with a cluster specific status code of Busy."
- [11.1124] "In case of any other parameter error, this command SHALL fail with a status code of COMMAND_INVALID."
- [11.1125] "This field SHALL specify the time in seconds during which commissioning session establishment is allowed by the Node. This timeout value SHALL follow guidance as specified in the initial Section 5.4.2.3, 'Announcement Duration'."
- [11.1125] "a commissioning session SHOULD NOT abort prematurely upon expiration of this timeout."
- [11.1126] "This field SHALL specify an ephemeral PAKE passcode verifier ... The field is concatenation of two values (w0 || L) SHALL be (CRYPTO_GROUP_SIZE_BYTES + CRYPTO_PUBLIC_KEY_SIZE_BYTES) -octets long."
- [11.1126] "It SHALL be derived from an ephemeral passcode. It SHALL be deleted by the Node at the end of commissioning or expiration of the OpenCommissioningWindow command, and SHALL be deleted by the existing Administrator after sending it to the Node(s)."
- [11.1127] "This field SHALL be used by the Node as the long discriminator for DNS-SD advertisement."
- [11.1128] "This field SHALL be used by the Node as the PAKE iteration count ... The permitted range of values SHALL match the range specified in Section 3.9."
- [11.1129] "This field SHALL be used by the Node as the PAKE Salt ... The constraints on the value SHALL match those specified in Section 3.9."
- [11.1130] "When a Node receives the Section 11.19.8.1, 'OpenCommissioningWindow' command, it SHALL begin advertising on DNS-SD."
- [11.1131] "When the command is received by a ICD, it SHALL enter into active mode. The ICD SHALL remain in Active Mode as long as one of these conditions is met: A commissioning window is open. There is an armed fail-safe timer."

### 11.19.8.2 OpenBasicCommissioningWindow Command

- [11.1132] "The current Administrator SHALL specify a timeout value for the duration of the OpenBasicCommissioningWindow command."
- [11.1133] "If a commissioning window is already currently open, this command SHALL fail with a cluster specific status code of Busy."
- [11.1134] "If the fail-safe timer is currently armed, this command SHALL fail with a cluster specific status code of Busy."
- [11.1135] "In case of any other parameter error, this command SHALL fail with a status code of COMMAND_INVALID."
- [11.1136] "The new Administrator SHALL discover the Node on the IP network using DNS-based Service Discovery (DNS-SD) for commissioning."
- [11.1138] "This field SHALL specify the time in seconds during which commissioning session establishment is allowed by the Node. This timeout SHALL follow guidance as specified in the initial Section 5.4.2.3, 'Announcement Duration'."
- [11.1139] "When a Node receives the Section 11.19.8.2, 'OpenBasicCommissioningWindow' command, it SHALL begin advertising on DNS-SD ... When the command is received by a ICD, it SHALL enter into active mode. The ICD SHALL remain in Active Mode as long as one of these conditions is met: A commissioning window is open. There is an armed fail-safe timer."

### 11.19.8.3 RevokeCommissioning Command

The Node SHALL perform the following actions regardless of current commissioning window state:
- "The Node SHALL (for ECM) delete the temporary PAKEPasscodeVerifier and associated data"
- "The Node SHALL terminate any open PASE sessions or PASE sessions in the process of being established"
- "The Node SHALL immediately expire any fail-safe held by an open PASE session and perform the cleanup steps outlined in Section 11.10.7.2.2, 'Behavior on expiry of Fail-Safe timer'"

If the commissioning window was open at the time of receipt:
- "The Node SHALL stop accepting new incoming PASE session establishment messages"
- "The Node SHALL stop publishing the DNS-SD records associated with the advertising it was doing due to the open commissioning window."
- "The Node SHALL expire the commissioning window and set the WindowStatus attribute to WindowNotOpen"

### 11.23 DCL DeviceModel Schema — Commissioning Fields

- [11.1596] "This field SHALL identify a hint for the steps that MAY be used to put a device that has not yet been commissioned into commissioning mode without factory resetting it."
- [11.1596] "Devices that implement Extended Discovery SHALL reflect this value in the Pairing Hint field of Commissionable Node Discovery when they have not yet been commissioned."
- [11.1597] "This field SHALL be populated with the appropriate Pairing Instruction for those values of CommissioningModeInitialStepsHint, for which the Pairing Hint Table indicates a Pairing Instruction (PI) dependency."
- [11.1598] "This field SHALL identify a hint for the steps that MAY be used to put a device that has already been commissioned into commissioning mode without factory resetting it."
- [11.1598] "At least bit 2 SHALL be set, to indicate that a current Administrator can be used to put a device that has already been commissioned into commissioning mode."
- [11.1598] "Devices that implement Extended Discovery SHALL reflect this value in the Pairing Hint field of Commissionable Node Discovery when they have already been commissioned."
- [11.1599] "This field SHALL be populated with the appropriate Pairing Instruction for those values of CommissioningModeSecondaryStepsHint, for which the Pairing Hint Table indicates a Pairing Instruction (PI) dependency."
- [11.1600] "This field SHALL identify a vendor-specific commissioning-fallback URL for the device model."
- [11.1600] "The syntax of this field SHALL follow the syntax as specified in RFC 1738 and SHALL use the https scheme. The maximum length of this field is 256 ASCII characters."
- [11.1601] "During the lifetime of the product, the specified URL SHOULD resolve to a maintained web page."
- [11.1594] "This field SHALL identify the device's commissioning flow with encoding as described in Custom Flow."
- [11.1595] "This field SHALL identify a vendor-specific commissioning URL for the device model when the CommissioningCustomFlow field is set to '2', and MAY be set for other values of CommissioningCustomFlow."
- [11.1595] "The syntax of this field SHALL follow the syntax as specified in RFC 1738 and SHALL use the https scheme. The maximum length of this field is 256 ASCII characters."

### 11.26 Joint Fabric Administrator — OpenJointCommissioningWindow Command

- [11.1941] "This command SHALL fail with a InvalidAdministratorFabricIndex status code sent back to the initiator if the AdministratorFabricIndex attribute has the value of null."

### 11.27 Commissioner Control Cluster

- [11.1956] "If the command is not executed via a CASE session, the command SHALL fail with a status code of UNSUPPORTED_ACCESS."
- [11.1957] "The server MAY request approval from the user, but it is not required."
- [11.1958] "The server SHALL always return SUCCESS to a correctly formatted RequestCommissioningApproval command, and then generate a CommissioningRequestResult event associated with the command's accessing fabric once the result is ready."
- [11.1959] "Clients SHOULD avoid using the same RequestID."
- [11.1959] "If the RequestID and client NodeID of a RequestCommissioningApproval match a previously received RequestCommissioningApproval and the server has not returned an error or completed commissioning of a device for the prior request, then the server SHOULD return FAILURE."
- [11.1973] "When received within the timeout specified by ResponseTimeoutSeconds in the CommissionNode command, the client SHALL open a commissioning window on a node which matches the VendorID and ProductID provided in the associated RequestCommissioningApproval command."
- [11.1974] "When commissioning this node, the server SHALL check that the VendorID and ProductID fields provided in the RequestCommissioningApproval command match the VendorID and ProductID attributes of the Basic Information Cluster which have already been verified during the Device Attestation Procedure. If they do not match, the server SHALL NOT complete commissioning and SHOULD indicate an error to the user."
- [11.1976] "This event SHALL be generated by the server following a RequestCommissioningApproval command which the server responded to with SUCCESS."

### 11.9 Network Commissioning Cluster

- [11.250] "This cluster SHALL support the FeatureMap bitmap attribute as defined below."
- [11.263] "This field SHALL indicate the key identifier of the Network Identity for this network connection; otherwise this field SHALL be null. If this field is non-null, the ClientIdentifier field SHALL also be non-null."
- [11.264] "This field SHALL indicate the key identifier of the Network Client Identity for this network connection; otherwise this field SHALL be null. If this field is non-null, the NetworkIdentifier field SHALL also be non-null."

---

## 4. Data Structures

### CommissioningErrorEnum (enum8) — Cluster 0x0030

| Value | Name | Summary | Conformance |
|-------|------|---------|-------------|
| 0 | OK | No error | M |
| 1 | ValueOutsideRange | Attempting to set regulatory configuration to a region or indoor/outdoor mode for which the server does not have proper configuration. | M |
| 2 | InvalidAuthentication | Executed CommissioningComplete outside CASE session. | M |
| 3 | NoFailSafe | Executed CommissioningComplete when there was no active Fail-Safe context. | M |
| 4 | BusyWithOtherAdmin | Attempting to arm fail-safe or execute CommissioningComplete from a fabric different than the one associated with the current fail-safe context. | M |
| 5 | RequiredTCNotAccepted | One or more required TC features from the Enhanced Setup Flow were not accepted. | TC |
| 6 | TCAcknowledgementsNotReceived | No or insufficient acknowledgements from the user for the TC features were received. | TC |
| 7 | TCMinVersionNotMet | The version of the TC features acknowledged by the user did not meet the minimum required version. | TC |

### RegulatoryLocationTypeEnum (enum8)

| Value | Name | Summary | Conformance |
|-------|------|---------|-------------|
| 0 | Indoor | Indoor only | M |
| 1 | Outdoor | Outdoor only | M |
| 2 | IndoorOutdoor | Indoor/Outdoor | M |

### NetworkRecoveryReasonEnum (enum8, max value < 15)

| Value | Name | Summary | Conformance |
|-------|------|---------|-------------|
| 0 | Unspecified | Unspecified / unknown reason of network failure | M |
| 1 | Auth | Credentials for the configured operational network are not valid | M |
| 2 | Visibility | Configured network cannot be found | M |
| 15–255 | DoNotUse | | M |

### BasicCommissioningInfo (struct)

| ID | Name | Type | Conformance |
|----|------|------|-------------|
| 0 | FailSafeExpiryLengthSeconds | uint16 | M |
| 1 | MaxCumulativeFailsafeSeconds | uint16 | M |

### General Commissioning Cluster Attributes (Cluster 0x0030)

| ID | Name | Type | Quality | Access | Conformance |
|----|------|------|---------|--------|-------------|
| 0x0000 | Breadcrumb | uint64 | | RW VA | M |
| 0x0001 | BasicCommissioningInfo | BasicCommissioningInfo | F | R V | M |
| 0x0002 | RegulatoryConfig | RegulatoryLocationTypeEnum | | R V | M |
| 0x0003 | LocationCapability | RegulatoryLocationTypeEnum | F | R V | M |
| 0x0004 | SupportsConcurrentConnection | bool | F | R V | M |
| 0x0005 | TCAcceptedVersion | uint16 | N | R A | TC |
| 0x0006 | TCMinRequiredVersion | uint16 | N | R A | TC |
| 0x0007 | TCAcknowledgements | map16 | N | R A | TC |
| 0x0008 | TCAcknowledgementsRequired | bool | N | R A | TC |
| 0x0009 | TCUpdateDeadline | uint32 | N X | R A | TC |
| 0x000A | RecoveryIdentifier | octstr (8) | N | R M | P, NR |
| 0x000B | NetworkRecoveryReason | NetworkRecoveryReasonEnum | X | R M | P, NR |
| 0x000C | IsCommissioningWithoutPower | bool | | R V | P, O |

### General Commissioning Cluster Commands (Cluster 0x0030)

| ID | Name | Direction | Response | Access | Conformance |
|----|------|-----------|----------|--------|-------------|
| 0x00 | ArmFailSafe | client ⇒ server | ArmFailSafeResponse | A | M |
| 0x01 | ArmFailSafeResponse | client ⇐ server | N | | M |
| 0x02 | SetRegulatoryConfig | client ⇒ server | SetRegulatoryConfigResponse | A | M |
| 0x03 | SetRegulatoryConfigResponse | client ⇐ server | N | | M |
| 0x04 | CommissioningComplete | client ⇒ server | CommissioningCompleteResponse | A F | M |
| 0x05 | CommissioningCompleteResponse | client ⇐ server | N | | M |
| 0x06 | SetTCAcknowledgements | client ⇒ server | SetTCAcknowledgementsResponse | A | TC |
| 0x07 | SetTCAcknowledgementsResponse | client ⇐ server | N | | TC |

### ArmFailSafe Command Fields

| ID | Name | Type | Fallback | Conformance |
|----|------|------|----------|-------------|
| 0 | ExpiryLengthSeconds | uint16 | 900 | M |
| 1 | Breadcrumb | uint64 | | M |

### ArmFailSafeResponse Command Fields

| ID | Name | Type | Constraint | Fallback | Conformance |
|----|------|------|-----------|----------|-------------|
| 0 | ErrorCode | CommissioningErrorEnum | | OK | M |
| 1 | DebugText | string | max 128 | "" | M |

### SetRegulatoryConfig Command Fields

| ID | Name | Type | Constraint | Conformance |
|----|------|------|-----------|-------------|
| 0 | NewRegulatoryConfig | RegulatoryLocationTypeEnum | | M |
| 1 | CountryCode | string | 2 | M |
| 2 | Breadcrumb | uint64 | | M |

### SetRegulatoryConfigResponse Command Fields

| ID | Name | Type | Fallback | Conformance |
|----|------|------|----------|-------------|
| 0 | ErrorCode | CommissioningErrorEnum | OK | M |
| 1 | DebugText | string | "" | M |

### CommissioningCompleteResponse Command Fields

| ID | Name | Type | Fallback | Conformance |
|----|------|------|----------|-------------|
| 0 | ErrorCode | CommissioningErrorEnum | OK | M |
| 1 | DebugText | string | "" | M |

### SetTCAcknowledgements Command Fields

| ID | Name | Type | Conformance |
|----|------|------|-------------|
| 0 | TCVersion | uint16 | M |
| 1 | TCUserResponse | map16 | M |

### SetTCAcknowledgementsResponse Command Fields

| ID | Name | Type | Fallback | Conformance |
|----|------|------|----------|-------------|
| 0 | ErrorCode | CommissioningErrorEnum | OK | M |

### CommissioningWindowStatusEnum (enum8) — Cluster 0x003C

| Value | Name | Summary | Conformance |
|-------|------|---------|-------------|
| 0 | WindowNotOpen | Commissioning window not open | M |
| 1 | EnhancedWindowOpen | An Enhanced Commissioning Method window is open | M |
| 2 | BasicWindowOpen | A Basic Commissioning Method window is open | BC |

### Administrator Commissioning Status Codes (StatusCodeEnum, enum8)

| Value | Name | Summary | Conformance |
|-------|------|---------|-------------|
| 0x02 | Busy | Could not be completed because another commissioning is in progress | M |
| 0x03 | PAKEParameterError | Provided PAKE parameters were incorrectly formatted or otherwise invalid | M |
| 0x04 | WindowNotOpen | No commissioning window was currently open | M |

### Administrator Commissioning Cluster Attributes (Cluster 0x003C)

| ID | Name | Type | Quality | Access | Conformance |
|----|------|------|---------|--------|-------------|
| 0x0000 | WindowStatus | CommissioningWindowStatusEnum | | R V | M |
| 0x0001 | AdminFabricIndex | fabric-idx | X | R V | M |
| 0x0002 | AdminVendorId | vendor-id | X | R V | M |

### Administrator Commissioning Cluster Commands (Cluster 0x003C)

| ID | Name | Direction | Response | Access | Conformance |
|----|------|-----------|----------|--------|-------------|
| 0x00 | OpenCommissioningWindow | client ⇒ server | Y | A T | M |
| 0x01 | OpenBasicCommissioningWindow | client ⇒ server | Y | A T | BC |
| 0x02 | RevokeCommissioning | client ⇒ server | Y | A T | M |

### OpenCommissioningWindow Command Fields

| ID | Name | Type | Constraint | Conformance |
|----|------|------|-----------|-------------|
| 0 | CommissioningTimeout | uint16 | desc | M |
| 1 | PAKEPasscodeVerifier | octstr | 97 | M |
| 2 | Discriminator | uint16 | 0 to 4095 | M |
| 3 | Iterations | uint32 | 1000 to 100000 | M |
| 4 | Salt | octstr | 16 to 32 | M |

### OpenBasicCommissioningWindow Command Fields

| ID | Name | Type | Constraint | Conformance |
|----|------|------|-----------|-------------|
| 0 | CommissioningTimeout | uint16 | desc | M |

### WiFiSecurityBitmap (map8) — Cluster 0x0031

| Bit | Name | Summary | Conformance |
|-----|------|---------|-------------|
| 0 | Unencrypted | Supports unencrypted Wi-Fi | M |
| 1 | WEP | Supports Wi-Fi using WEP security | M |
| 2 | WPA-PERSONAL | Supports Wi-Fi using WPA-Personal security | M |
| 3 | WPA2-PERSONAL | Supports Wi-Fi using WPA2-Personal security | M |
| 4 | WPA3-PERSONAL | Supports Wi-Fi using WPA3-Personal security | M |
| 5 | WPA3-Matter-PDC | Supports Wi-Fi using Per-Device Credentials | P, M |

### ThreadCapabilitiesBitmap (map16)

| Bit | Name | Summary | Conformance |
|-----|------|---------|-------------|
| 0 | IsBorderRouterCapable | Thread Border Router functionality is present | O |
| 1 | IsRouterCapable | Router mode is supported | O |
| 2 | IsSleepyEndDeviceCapable | Sleepy end-device mode is supported | O |
| 3 | IsFullThreadDevice | Device is a full Thread device | O |
| 4 | IsSynchronizedSleepyEndDeviceCapable | Synchronized sleepy end-device mode is supported | O |

### WiFiBandEnum (enum8)

| Value | Name | Summary | Conformance |
|-------|------|---------|-------------|
| 0 | 2G4 | 2.4GHz - 2.401GHz to 2.495GHz (802.11b/g/n/ax) | O.b+ |
| 1 | 3G65 | 3.65GHz - 3.655GHz to 3.695GHz (802.11y) | O.b+ |
| 2 | 5G | 5GHz - 5.150GHz to 5.895GHz (802.11a/n/ac/ax) | O.b+ |
| 3 | 6G | 6GHz - 5.925GHz to 7.125GHz (802.11ax / Wi-Fi 6E) | O.b+ |
| 4 | 60G | 60GHz - 57.24GHz to 70.20GHz (802.11ad/ay) | O.b+ |
| 5 | 1G | Sub-1GHz - 755MHz to 931MHz (802.11ah) | O.b+ |

### NetworkCommissioningStatusEnum (enum8)

| Value | Name | Summary | Conformance |
|-------|------|---------|-------------|
| 0 | Success | OK, no error | M |
| 1 | OutOfRange | Value Outside Range | M |
| 2 | BoundsExceeded | A collection would exceed its size limit | M |
| 3 | NetworkIDNotFound | The NetworkID is not among the collection of added networks | M |
| 4 | DuplicateNetworkID | The NetworkID is already among the collection of added networks | M |
| 5 | NetworkNotFound | Cannot find AP: SSID Not found | M |
| 6 | RegulatoryError | Cannot find AP: Mismatch on band/channels/regulatory domain / 2.4GHz vs 5GHz | M |
| 7 | AuthFailure | Cannot associate due to authentication failure | M |
| 8 | UnsupportedSecurity | Cannot associate due to unsupported security mode | M |
| 9 | OtherConnectionFailure | Other association failure | M |
| 10 | IPV6Failed | Failure to generate an IPv6 address | M |
| 11 | IPBindFailed | Failure to bind Wi-Fi IP interfaces | M |
| 12 | UnknownError | Unknown error | M |

### NetworkInfoStruct

| ID | Name | Type | Constraint | Quality | Fallback | Conformance |
|----|------|------|-----------|---------|----------|-------------|
| 0 | NetworkID | octstr | 1 to 32 | | | M |
| 1 | Connected | bool | | | | M |
| 2 | NetworkIdentifier | octstr | 20 | X | null | P, PDC |
| 3 | ClientIdentifier | octstr | 20 | X | null | P, PDC |

### Network Commissioning Cluster Features (Cluster 0x0031)

| Bit | Code | Feature | Conformance | Summary |
|-----|------|---------|-------------|---------|
| 0 | WI | WiFiNetworkInterface | O.a | Wi-Fi related features |
| 1 | TH | ThreadNetworkInterface | O.a | Thread related features |
| 2 | ET | EthernetNetworkInterface | O.a | Ethernet related features |
| 3 | PDC | PerDeviceCredentials | P, WI | Wi-Fi Per-Device Credentials |

### CommissioningRequestResult Event Fields (Commissioner Control Cluster)

| ID | Name | Type | Constraint | Conformance |
|----|------|------|-----------|-------------|
| 0 | RequestID | uint64 | all | M |
| 1 | ClientNodeID | node-id | all | M |
| 2 | StatusCode | status | desc | M |

### RequestCommissioningApproval Command Fields

| ID | Name | Type | Constraint | Conformance |
|----|------|------|-----------|-------------|
| 0 | RequestID | uint64 | | M |
| 1 | VendorID | vendor-id | | M |
| 2 | ProductID | uint16 | | M |
| 3 | Label | string | max 64 | O |

### ReverseOpenCommissioningWindow / OpenJointCommissioningWindow Command Fields

| ID | Name | Type | Constraint | Conformance |
|----|------|------|-----------|-------------|
| 0 | CommissioningTimeout | uint16 | desc | M |
| 1 | PAKEPasscodeVerifier | octstr | 97 | M |
| 2 | Discriminator | uint16 | max 4095 | M |
| 3 | Iterations | uint32 | 1000 to 100000 | M |
| 4 | Salt | octstr | 16 to 32 | M |

---

## 5. Security Considerations

### CASE Session Requirement for CommissioningComplete

[11.511] `CommissioningComplete` is fabric-scoped and cannot be issued over a session that does not have an associated fabric (i.e., over PASE session prior to an `AddNOC` command). It is only permitted over CASE and must be issued by a node associated with the ongoing Fail-Safe context.

[11.512] The command SHALL only result in success if received over a CASE session and the accessing fabric index matches the Fabric Index associated with the current Fail-Safe context.

### PASE Session Handling

[11.482] If the fail-safe timer is not currently armed, the commissioning window is open, and `ArmFailSafe` is received over a CASE session, the command SHALL leave the current fail-safe state unchanged and immediately respond with `BusyWithOtherAdmin`. This protects PASE-connected commissioners during the commissioning window.

On successful `CommissioningComplete`, [11.513] the Secure Session Context of any PASE session still established at the Server SHALL be cleared.

On fail-safe expiry [11.490], the receiver SHALL terminate any open PASE secure session by clearing any associated Secure Session Context and revoke temporary administrative privileges granted to any open PASE session.

### PAKE Passcode Verifier Lifecycle

[11.1126] The PAKEPasscodeVerifier SHALL be derived from an ephemeral passcode. It SHALL be deleted by the Node at the end of commissioning or expiration of `OpenCommissioningWindow`, and SHALL be deleted by the existing Administrator after sending it to the Node(s).

On `RevokeCommissioning`, the Node SHALL (for ECM) delete the temporary PAKEPasscodeVerifier and associated data.

### RequestCommissioningApproval — CASE Requirement

[11.1956] If the `RequestCommissioningApproval` command is not executed via a CASE session, the command SHALL fail with a status code of `UNSUPPORTED_ACCESS`.

### VendorID/ProductID Verification during Commissioner Control

[11.1974] When commissioning a node via `ReverseOpenCommissioningWindow`, the server SHALL check that the VendorID and ProductID fields provided in `RequestCommissioningApproval` match the VendorID and ProductID attributes of the Basic Information Cluster which have already been verified during the Device Attestation Procedure. If they do not match, the server SHALL NOT complete commissioning.

### RecoveryIdentifier Randomness

[11.473] The `RecoveryIdentifier` attribute SHALL contain a random 64-bit value. It is important that this value be selected at random from a 64-bit number space to ensure a high likelihood of uniqueness from values selected by other Nodes. This value SHOULD be obtained through `Crypto_DRBG(len = 64)`.

### Commissioning Window Access Control

[11.1116] Only one commissioning window can be active at a time. [11.1123] If the fail-safe timer is currently armed, `OpenCommissioningWindow` SHALL fail with `Busy`, since it is likely that concurrent commissioning operations from multiple separate Commissioners are about to take place. The same applies to `OpenBasicCommissioningWindow` [11.1134].

---

## 6. Error Handling

### General Commissioning Error Codes (CommissioningErrorEnum)

- **OK (0)**: No error — successful operation.
- **ValueOutsideRange (1)**: Regulatory configuration set to a region or indoor/outdoor mode for which the server does not have proper configuration.
- **InvalidAuthentication (2)**: `CommissioningComplete` executed outside CASE session, or accessing fabric does not match Fail-Safe context fabric.
- **NoFailSafe (3)**: `CommissioningComplete` executed when there was no active Fail-Safe context.
- **BusyWithOtherAdmin (4)**: Attempt to arm fail-safe or execute `CommissioningComplete` from a fabric different than the one associated with the current fail-safe context; or `ArmFailSafe` received over CASE when commissioning window is open and fail-safe is not armed.
- **RequiredTCNotAccepted (5)**: One or more required TC features from the Enhanced Setup Flow were not accepted. (TC conformance)
- **TCAcknowledgementsNotReceived (6)**: No or insufficient acknowledgements from the user for the TC features were received. (TC conformance)
- **TCMinVersionNotMet (7)**: The version of the TC features acknowledged by the user did not meet the minimum required version. (TC conformance)

### SetRegulatoryConfig Error Conditions

[11.498] Setting regulatory information outside a valid country or location still sets the Location attribute but the response SHALL have `ErrorCode` set to `ValueOutsideRange`.

[11.499] If `LocationCapability` is not Indoor/Outdoor and the `NewRegulatoryConfig` value does not match the fixed value in `LocationCapability`, then the response SHALL have `ErrorCode` set to `ValueOutsideRange` and the `RegulatoryConfig` attribute and associated internal radio configuration SHALL remain unchanged.

[11.502] If the command fails, the Breadcrumb attribute SHALL be left unchanged.

### SetTCAcknowledgements Error Conditions

[11.523] If `TCVersion` is less than `TCMinRequiredVersion`, then the ErrorCode `TCMinVersionNotMet` SHALL be returned and `TCAcknowledgements` SHALL remain unchanged.

[11.524] If `TCVersion` is greater than or equal to `TCMinRequiredVersion` but `TCUserResponse` indicates not all required terms were accepted, then `RequiredTCNotAccepted` SHALL be returned and `TCAcknowledgements` SHALL remain unchanged.

### Administrator Commissioning Status Codes

- **Busy (0x02)**: Another commissioning is in progress — returned when a commissioning window is already open or fail-safe timer is armed.
- **PAKEParameterError (0x03)**: Provided PAKE parameters were incorrectly formatted or otherwise invalid — returned on format/validity errors in `PAKEPasscodeVerifier`, `Iterations`, or `Salt`.
- **WindowNotOpen (0x04)**: No commissioning window was currently open — returned by `RevokeCommissioning` if the commissioning window was not open at time of receipt.

For any other parameter error, `OpenCommissioningWindow` and `OpenBasicCommissioningWindow` SHALL fail with `COMMAND_INVALID` [11.1124, 11.1135].

### NetworkCommissioningStatusEnum Error Codes

Errors 0–12 inclusive, ranging from `Success` through network-specific failures (`NetworkIDNotFound`, `DuplicateNetworkID`, `NetworkNotFound`, `RegulatoryError`, `AuthFailure`, `UnsupportedSecurity`, `OtherConnectionFailure`, `IPV6Failed`, `IPBindFailed`) to `UnknownError`. All are Mandatory conformance.

### Fail-Safe Timer Expiry — Automatic Cleanup

[11.490] If the fail-safe timer expires before `CommissioningComplete` is successfully invoked, a mandatory ordered sequence of cleanup steps SHALL be executed (see Section 2 above for enumerated steps). [11.485] If the receiver restarts unexpectedly (e.g., power interruption, software crash, or other reset), the receiver SHALL behave as if the fail-safe timer expired and perform the same cleanup sequence.

### DebugText in Responses

[11.479] Response commands contain a `DebugText` argument which SHOULD NOT be presented directly in user interfaces. Its purpose is to help developers in troubleshooting errors. The value MAY go into logs or crash reports.