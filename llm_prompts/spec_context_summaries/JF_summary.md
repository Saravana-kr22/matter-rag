# Matter Spec Summary: JF Protocol

**Source sections matched:** 14  
**Source chars sent to LLM:** 28,938  
**Generated:** 2026-04-28 17:26:22  
**Summary words:** 4,649  

---

## 1. Overview

The Joint Fabric (JF) Protocol defines how multiple vendors or companies sharing the same CA hierarchy, governed under the same Trusted Root Certificate Authority (TRCA), create and manage a **Joint Fabric** using the **Joint Commissioning Method (JCM)** flow. The fabric anchored by this TRCA is called the **Anchor Fabric**.

Key concepts from the provided text:

- **Anchor Fabric**: The fabric anchored by the TRCA.
- **Anchor Administrator**: The single entity that holds special responsibilities including ICAC Cross Signing, hosting the Joint Fabric Datastore, and being the only entity authorized to issue Joint Fabric Datastore updates.
- **Ecosystem Administrator**: Any ecosystem administrator that can administer devices on the Joint Fabric, provided devices have ACL entries containing the Administrator CAT.
- **Administrator CAT / Anchor CAT**: CASE Authenticated Tags used in the Joint Fabric ACL architecture to control and simplify Administer privilege assignment.
- **Joint Fabric Datastore Cluster**: The mechanism through which Ecosystem Administrators are notified of device additions and removals.

Support for Joint Fabric is noted as provisional. Support for Joint Commissioning Method is also noted as provisional.

---

## 2. Protocol Details

### Architecture

The Joint Fabric architecture allows any Ecosystem Administrator to administer devices on the Joint Fabric, where the Joint Fabric Administrator's NOC and devices being administered have ACL entries containing the Administrator CAT, regardless of which Ecosystem Commissioner/Administrator commissioned the device. Other Ecosystem Administrators are notified of changes via the Joint Fabric Datastore Cluster.

Removal of a device from the Joint Fabric is similarly visible to all Ecosystem Administrators via Joint Fabric NodeList Attribute subscriptions. Bindings or subscriptions to a removed device are known within the context of the Joint Fabric Datastore Cluster and can be removed as part of the removal process.

### Joint Commissioning Method (JCM) Flow

JCM is the process through which an administrator of Ecosystem B receives a cross-signed ICAC from the Anchor Administrator of Ecosystem A. The high-level flow is:

1. **Ecosystem B Administrator** becomes commissionable for JCM via `OpenJointCommissioningWindow` (either instructed by another Ecosystem B Administrator over a CASE session, or by implementation-specific means). A new RANDOM passcode is computed and presented to Ecosystem A Commissioner using ECM rules.
2. **Ecosystem A Commissioner** starts commissioning steps through Device Attestation Procedure (Step 10), with standard error handling.
3. **Trust verification against Ecosystem B Administrator**: Ecosystem A Commissioner searches all endpoints' Descriptor cluster for the Joint Fabric Administrator device type and cluster, saves the endpoint as `JointEndPointB`, checks that the NOC of the Fabric indicated by `AdministratorFabricIndex` contains an Administrator CAT, executes Fabric Table Vendor ID Verification Procedure, obtains user agreement, and saves the `SubjectPublicKey` of the ICAC as `TrustedIcacPublicKeyB`.
4. **AddNOC step**: Ecosystem B Administrator receives a NOC with Administrator CAT inside Fabric A (version from GroupList attribute). The `CaseAdminSubject` field contains an Anchor CAT granting Administer privileges only to Ecosystem A Anchor Administrator.
5. **Trust verification against Ecosystem A Administrator**: Ecosystem A Administrator invokes `AnnounceJointFabricAdministrator`. Ecosystem B Administrator reads `AdministratorFabricIndex`, executes Fabric Table Vendor ID Verification Procedure, and checks that RootPublicKey and FabricID match.
6. **ICAC Cross Signing**: Ecosystem A Administrator cross-signs the ICAC using `TrustedIcacPublicKeyB`.
7. Ecosystem B Administrator saves the cross-signed ICAC as `JFCrossSignedICAC` and sets its `AdministratorFabricIndex` attribute.
8. **CommissioningComplete** (Step 21) is executed.

### Node Update Flow (Bringing Fabric B Nodes into Joint Fabric)

Ecosystem B Administrator may use Operational Credentials cluster commands to update each Node in Fabric B to join Fabric A: add the Fabric A Trusted Root Certificate, issue a new NOC via AddNOC (using `JFCrossSignedICAC`), and remove the old Trusted Root CA Certificate and associated Fabric B via `RemoveFabric`.

### ICAC Cross Signing

- Initiated by Anchor Administrator via `ICACCSRRequest` command.
- Joining Administrator creates an ICA CSR (per PKCS #10), signed using the Private Key of its pre-installed ICAC.
- ICA CSR is returned inside the `ICACCSR` parameter of the `ICACCSRResponse` command.
- Anchor Administrator validates the CSR, verifies the signature using `TrustedIcacPublicKeyB`, converts Subject Public Key Info, and signs the ICAC chaining to the Anchor RCAC.
- Signed ICAC is returned in the `ICAC` field of the `AddICAC` command.

### Anchor Administrator Selection and Transfer

- Anchor Administrator role is assigned during JCM creation or transferred via the **Transfer Anchor** procedure.
- Transfer requires user consent on both sides (Administrator A and Administrator B).
- Administrator B sends `TransferAnchorRequest` to Administrator A. Administrator A verifies NOC contains Administrator CAT, checks user consent, checks for pending Datastore entries, puts Datastore in read-only state, and stops DNS-SD advertising.
- Administrator B copies the Datastore, sets status to `Committed`, sets itself as new Anchor, updates NOCs on all Joint Fabric Administrators (with updated Administrator CAT version), issues itself a new NOC with updated Anchor CAT, and sends `TransferAnchorComplete` to Administrator A.
- Other Administrators subscribe to the new Datastore, verify Anchor CAT, request ICA Cross Signing, remove devices from the old Anchor CA fabric, and re-commission onto the Joint Fabric using the new ICAC.

### Administrator Removal

1. Anchor Administrator sends `RemoveFabric` to the outgoing Administrator (with user consent).
2. Joint Fabric Administrator removes the outgoing Administrator from the Joint Fabric Datastore.
3. Outgoing Administrator removes the Fabric and deletes all associated fabric-scoped data.
4. Anchor Administrator increments the Administrator CAT version and issues itself a new NOC.
5. Administrator B sets new NOC for all Joint Fabric Administrators.

---

## 3. Normative Requirements

### 12.2.2. Node ID Generation

- [12.6] "Any newly-allocated Node ID SHALL: be greater than 0x0000_0000_0000_0000, but less than 0xFFFF_FFEF_FFFF_FFFF, representing a value within the Operational NodeID range"
- [12.6] "be checked to ensure its uniqueness in the NodeList attribute of the Section 11.25, 'Joint Fabric Datastore Cluster'"
- [12.7] "The Node ID SHALL be regenerated if these constraints are not met."
- [12.8] "It is RECOMMENDED to use random allocation within the valid range to avoid having to regenerate the Node ID."

### 12.2.3. Anchor ICAC Requirements

- [12.9] "The Anchor ICAC SHALL be the ICAC corresponding to the Anchor Administrator."
- [12.9] "The Anchor ICAC SHALL contain the reserved org-unit-name attribute from the Table 87, 'Standard DN Object Identifiers' with value jf-anchor-icac in its Subject DN."
- [12.9] "The Anchor ICAC SHALL be issued only by the Anchor CA to an Anchor Administrator."

### 12.2.4.1. Administrator CAT

- [12.11] "All devices participating in Section 12.2, 'Joint Fabric' SHALL contain an ACL entry granting Administer privilege to CaseSubjectAdmin set to the Administrator CAT."
- [12.11] "During commissioning of any Node onto the Section 12.2, 'Joint Fabric' the CaseAdminSubject field SHALL be set to the Administrator CAT upon invoking the AddNOC command."
- [12.12] "Any Node advertising as a Section 12.2, 'Joint Fabric' Administrator SHALL contain the Administrator CAT in its NOC."
- [12.12] "A NOC containing the Administrator CAT MAY be issued by any Section 12.2, 'Joint Fabric' Administrator."
- [12.13] "Any client that discovers an Administrator Node with DNS-SD and connects to the Node via CASE SHALL check if the Administrator CAT is present in the NOC using the Peer CASE Authenticated Tags before taking any action on the Node."
- [12.13] "This SHALL be required in order to verify that the Node is authorized to act as an Administrator in the Section 12.2, 'Joint Fabric'."
- [12.15] "the NOC SHALL be issued only by an Administrator."
- [12.16] "User initiated and granted revocation of an Administrator to administer nodes SHALL be achieved by updating the Administrator CAT."
- [12.16] "The Joint Fabric Anchor Administrator SHALL increment the version number of the Administrator CAT to a value higher than its current value (e.g., from 0x0000 to 0x0001), update the existing credentials (NOC) for all Administrator Nodes that are NOT being revoked with the new version of the Administrator CAT, and update the ACL entry of all Nodes whose subject list contains the prior version of the Administrator CAT with the new version of the Administrator CAT."

### 12.2.4.2. Anchor CAT

- [12.17] "All Administrator devices participating in Section 12.2, 'Joint Fabric' SHALL contain an ACL entry granting Administer privilege with the CaseSubjectAdmin set to the Anchor CAT."
- [12.17] "During commissioning of any Administrator Node onto the Section 12.2, 'Joint Fabric' the CaseAdminSubject field SHALL be set to the Anchor CAT upon invoking the AddNOC command."
- [12.17] "Any Node advertising as a Section 12.2, 'Joint Fabric' Anchor or Datastore SHALL contain the Anchor CAT in its NOC."
- [12.17] "A NOC containing the Anchor CAT SHALL be issued only by the Section 12.2, 'Joint Fabric' Anchor ICAC."
- [12.18] "Any client that discovers an Anchor Node with DNS-SD and connects to the Node via CASE SHALL check if the Anchor CAT is present in the NOC using the Peer CASE Authenticated Tags and that the NOC chains up to the Anchor ICAC before taking any Section 12.2, 'Joint Fabric' actions on the Node."
- [12.18] "This SHALL be required in order to verify that the Node is authorized to act as an Anchor of the Section 12.2, 'Joint Fabric'."

### 12.2.5. Joint Commissioning Method (JCM)

- [12.20] "This method SHALL be implemented for Commissioners and Administrators that support Joint Fabric."
- [12.21] "While Ecosystem A MAY contain multiple administrator nodes, only the Anchor Administrator SHALL be able to execute JCM."
- [12.23] "all ecosystems participating in the Section 12.2, 'Joint Fabric' SHALL use an ICAC to sign NOCs."
- [12.24] "Ecosystem B Administrator SHALL become commissionable for JCM."
- [12.24] "A new RANDOM passcode SHALL be computed and Section 11.26.6.5, 'OpenJointCommissioningWindow' SHALL be called using the corresponding PAKE passcode verifier."
- [12.24] "On Ecosystem A side, User SHALL be made aware that it has the option of using the Section 5.1, 'Onboarding Payload' in order to start the JCM steps."
- [12.24] "Ecosystem A Commissioner SHALL start execution of the steps outlined in Figure 47, 'Commissioning flow diagram' with Ecosystem B Administrator."
- [12.24] "Ecosystem A Commissioner SHALL search all endpoints' Descriptor cluster of Ecosystem B Administrator for the Joint Fabric Administrator device type, which requires the Joint Fabric Administrator cluster."
- [12.24] "if Joint Fabric Administrator cluster is not found, process SHALL be terminated and the User SHALL be informed that Ecosystem B lacks the expected functionality"
- [12.24] "otherwise the endpoint that contains the Joint Fabric Administrator cluster SHALL be saved as JointEndPointB"
- [12.24] "Ecosystem A Commissioner SHALL check that the NOC used by the Fabric indicated by the AdministratorFabricIndex of the Joint Fabric Administrator Cluster on JointEndPointB contains an Administrator CAT."
- [12.24] "if verification fails, process SHALL be terminated and the User SHALL be informed that Ecosystem B Administrator is not trusted"
- [12.24] "Ecosystem A Commissioner SHALL execute Section 6.4.10, 'Fabric Table Vendor ID Verification Procedure' against the Fabric indicated by the AdministratorFabricIndex of the Joint Fabric Administrator Cluster on JointEndPointB"
- [12.24] "if verification fails, process SHALL be terminated and the User SHALL be informed that Ecosystem B Administrator is not trusted"
- [12.24] "On Ecosystem A side, User SHALL be asked if they agree to onboard the Fabric indicated by AdministratorFabricIndex."
- [12.24] "during the AddNOC step, Ecosystem B Administrator SHALL receive a NOC with Administrator CAT inside Fabric A."
- [12.24] "If the NOC doesn't contain an Administrator CAT then this command SHALL process an error by responding with a Section 11.18.6.10, 'NOCResponse' with a StatusCode of InvalidNOC as described in Section 11.18.6.7.2, 'Handling Errors'. The process SHALL be terminated and the User SHALL be informed."
- [12.24] "during the AddNOC step, the CaseAdminSubject field SHALL contain an Anchor CAT that grants Administer privileges only to Ecosystem A Anchor Administrator over Ecosystem B Administrator in Fabric A."
- [12.24] "If the CaseAdminSubject doesn't contain an Anchor CAT then this command SHALL process an error by responding with a StatusCode of InvalidAdminSubject as described in Section 11.18.6.7.2, 'Handling Errors'. The process SHALL be terminated and the User SHALL be informed."
- [12.24] "Ecosystem A Administrator SHALL invoke the Section 11.26.6.9, 'AnnounceJointFabricAdministrator' command of the Joint Fabric Administrator cluster belonging to JointEndPointB on Ecosystem B Administrator. The EndpointID parameter SHALL be set to the value of the endpoint that holds the Joint Fabric Administrator on Ecosystem B Administrator."
- [12.24] "Ecosystem B Administrator SHALL save the value of the EndpointID as JointEndPointA"
- [12.24] "Ecosystem B Administrator SHALL read the Section 11.26.5.1, 'AdministratorFabricIndex' attribute of the Joint Fabric Administrator cluster belonging to JointEndPointA on Ecosystem A Administrator and executes Section 6.4.10, 'Fabric Table Vendor ID Verification Procedure' against the Fabric indicated by AdministratorFabricIndex"
- [12.24] "Ecosystem B Administrator SHALL check that the RootPublicKey and FabricID of the accessing fabric (found in the FabricDescriptorStruct) match the RootPublicKey and FabricID of the Fabric indicated by AdministratorFabricIndex."
- [12.24] "if verification fails, the process SHALL be terminated and the User SHALL be informed that Ecosystem B Administrator is not trusted"
- [12.24] "Ecosystem A Administrator SHALL follow the ICAC cross-signing steps using TrustedIcacPublicKeyB as input parameter."
- [12.24] "in case of error, the process SHALL be terminated and the User SHALL be informed"
- [12.24] "Ecosystem B Administrator SHALL save the cross-signed ICAC … as JFCrossSignedICAC"
- [12.24] "Ecosystem B Administrator SHALL set the Section 11.26.5.1, 'AdministratorFabricIndex' attribute of its own Joint Fabric Administrator cluster to the index that Fabric A has inside the FabricDescriptorStruct"
- [12.24] "Step 21 (CommissioningComplete) of Section 5.5, 'Commissioning Flows' SHALL be executed."
- [12.24] "User MAY be notified that Ecosystem B Administrator is now onboarded as an Administrator in the Joint Fabric formed with Ecosystem A."
- [12.24] "At least the VendorID of Ecosystem A SHOULD be provided in this notification and the User MAY have the option of leaving the Joint Fabric"
- [12.25] "NOCValue SHALL contain a NOC issued by the JFCrossSignedICAC."
- [12.25] "Subject DN of the NOCValue SHALL encode a matter-fabric-id attribute whose value SHALL be identical with the value of the matter-fabric-id attribute from JFCrossSignedICAC"
- [12.25] "ICACValue parameter SHALL be set to the value of JFCrossSignedICAC"
- [12.25] "IPKValue parameter SHALL be set to the value of the IPK found in the GroupKeySetList hold by the Joint Fabric Datastore of Fabric A"
- [12.25] "CaseAdminSubject SHALL contain an Administrator CAT whose version is indicated by the corresponding entry from GroupList attribute hold by the Joint Fabric Datastore of Fabric B."
- [12.25] "AdminVendorId field SHALL be set to the value of the AdminVendorIdValue found in the local Fabric Table under the AdministratorFabricIndex."
- [12.26] "Ecosystem B SHALL invoke the required commands on the Joint Fabric Datastore cluster owned by the Anchor Administrator of Fabric B in order to remove those Nodes and on the Joint Fabric Datastore cluster owned by Ecosystem A Anchor Administrator in order to add those Nodes."

### 12.2.5.1. Scope of User Consent

- [12.27] "Before commissioning a Joint Fabric Administrator, the user SHALL be asked for consent to enable Joint Fabric functionality between ecosystems."
- [12.27] "Each ecosystem joining the Joint Fabric SHALL independently ask the user for consent before initiating JCM."

### 12.2.5.2. Discovery

- [12.28] "The user SHALL be able to enable JCM through an appropriate interface of the devices on that ecosystem."

### 12.2.5.3. Vendor ID Validation

- [12.29] "Vendor ID validation SHALL be achieved by using the Section 6.4.10, 'Fabric Table Vendor ID Verification Procedure'."

### 12.2.5.4. ICAC Cross Signing

- [12.30] "A joining device, acting also as an Administrator in Fabric B, SHALL obtain the ability to issue NOCs chaining up to the Anchor CA of Fabric A once it has become an Administrator in Fabric A by following the Joint Commissioning Method."
- [12.31] "To obtain this ability it SHALL receive an ICAC issued by the Anchor Administrator from Fabric A."
- [12.32] "A pre-requisite for the ICA Cross Signing process is the execution of FabricFabric Table Vendor ID Verification Procedure against the Fabric indicated by Section 11.26.5.1, 'AdministratorFabricIndex' of the joining Administrator."
- [12.32] "the public ICAC of that Fabric SHALL be passed as input parameter to this procedure as trusted SubjectPublicKey and it SHALL be encoded using the specific rules for ec-pub-key, pub-key-algo and ec-curve-id."
- [12.33] "The joining Administrator SHALL create a Certificate Signing Request (ICA CSR) as described in PKCS #10"
- [12.33] "ICAC CSR SHALL include a signature (see RFC 2986 section 4.2, SignatureAlgorithm) generated using the Private Key of its pre-installed ICAC."
- [12.33] "The Public Key associated with the Private Key used to sign the ICA CSR SHALL appear in the SubjectPublicKey of the ICA CSR."
- [12.33] "Once the ICAC CSR is created it SHALL be sent in a DER-encoded string inside the ICACCSR parameter of the Section 11.26.6.2, 'ICACCSRResponse' command."
- [12.34] "Signature SHALL be validated using the trusted SubjectPublicKey."
- [12.34] "Values for ec-pub-key, pub-key-algo, ec-curve-id SHALL match with the ones of the trusted SubjectPublicKey"
- [12.34] "Upon success, ICA's certificate SHALL be signed by the root CA of the Anchor Administrator, such that the ICAC chains to the RCAC of the Anchor Administrator."
- [12.34] "The subject DN SHALL encode a matter-fabric-id attribute. The attribute's value SHALL be identical to the Fabric ID of the Anchor Fabric."
- [12.34] "ICAC SHALL be returned inside the ICAC field of the Section 11.26.6.3, 'AddICAC' command."
- [12.34] "If any of the above validation checks fail, the server SHALL immediately terminate the procedure and inform the User."

### 12.2.6. Anchor Administrator Selection

- [12.35] "assignment of this role SHALL be based on the intent and consent of the user."
- [12.35] "This role SHALL be assigned during the creation of the Joint Fabric using the Section 12.2.5, 'Joint Commissioning Method (JCM)' or MAY be transferred to another Joint Fabric Administrator using the Transfer Anchor procedure, both requiring Section 11.20.3.4, 'Obtaining user consent for updating software'."
- [12.38] "User consent SHALL be mutual: on Administrator A side, User provides consent that transfer of the Anchor role to Administrator B is allowed. on Administrator B side, User provides consent that receipt of the Anchor role from Administrator A is allowed."
- [12.39] "Administrator B SHALL: send Section 11.26.6.6, 'TransferAnchorRequest' to Administrator A to set itself as Joint Fabric Anchor Administrator. obtain user consent prior to sending the Section 11.26.6.6, 'TransferAnchorRequest' command."
- [12.39] "Administrator A SHALL: check that the NOC used by Administrator B during the CASE session contains an Administrator CAT."
- [12.39] "check that user provided consent that allows the transfer of the Anchor role to a different Administrator. If not, then Section 11.26.6.7, 'TransferAnchorResponse' command with StatusCode set to TransferAnchorStatusNoUserConsent SHALL be sent and the procedure stopped here."
- [12.39] "check all the Datastore entries of type DatastoreStatusEntryStruct. If any of these entries has a value that equals to Pending or to DeletePending then Section 11.26.6.7, 'TransferAnchorResponse' command with StatusCode set to TransferAnchorStatusDatastoreBusy SHALL be sent and the procedure stopped here."
- [12.39] "put Joint Fabric Datastore in read only state by setting the Datastore StatusEntry to DeletePending."
- [12.39] "stop DNS-SD advertising of the Administrator, Anchor and Datastore capability inside the JF TXT key."
- [12.39] "All other Joint Fabric Administrators SHALL: stop commissioning of any new devices into the Joint Fabric once it detects that Datastore StatusEntry equals DeletePending."
- [12.39] "Administrator B SHALL: copy (through attribute read, BDX) Joint Fabric Datastore from Administrator A."
- [12.39] "set the Datastore StatusEntry to Committed."
- [12.39] "set the value of the Datastore AnchorNodeId attribute to the value of its Node ID."
- [12.39] "increase Section 12.2.4.1, 'Administrator CAT' version."
- [12.39] "NOCs SHALL contain an updated version of the Administrator CAT."
- [12.39] "issue itself a new NOC with the updated version of the Anchor CAT."
- [12.39] "set itself as the new Datastore provider and Anchor Administrator by advertising the Administrator, Anchor and Datastore capabilities inside the JF TXT key."
- [12.39] "send Section 11.26.6.8, 'TransferAnchorComplete' to Administrator A to announce that transition to Anchor Administrator is complete."
- [12.39] "All other Joint Fabric Administrators SHALL: subscribe to the new Datastore provider (Administrator B) having discovered the new Datastore capability via Service Discovery of the JF TXT key."
- [12.39] "check that the NOC used by the new Datastore provider (Administrator B) during the first CASE session contains an Section 12.2.4.2, 'Anchor CAT' and that the NOC chains up to the Anchor ICAC."
- [12.39] "request ICA Cross Signing from the new Joint Fabric Anchor Administrator discovered via Service Discovery (Administrator and Anchor flags are set in JF TXT key)."
- [12.39] "remove devices from the fabric governed by the old Anchor CA using the Section 11.18.6.12, 'RemoveFabric' command."
- [12.39] "start commissioning devices onto the Joint Fabric using the new ICAC for NOC issuance."

### 12.2.7. Administrator Removal

- [12.40] "the following steps SHALL be taken to remove an Intermediate NOC Certificate Authority from a Section 12.2, 'Joint Fabric'."
- [12.41] "The Section 11.18.6.12, 'RemoveFabric' section outlines a Warning that SHALL apply here for removing a Joint Fabric Administrator."

### 12.2.7.1. Security Consideration

- [12.42] "the Anchor Administrator SHOULD trigger a transition to a new Trusted Root Certificate as described in the Section 12.2.6, 'Anchor Administrator Selection' section."

---

## 4. Data Structures

### Node ID Constraints (from §12.2.2)

| Field | Value |
|---|---|
| Minimum Node ID (exclusive) | `0x0000_0000_0000_0000` |
| Maximum Node ID (exclusive) | `0xFFFF_FFEF_FFFF_FFFF` |

### Anchor ICAC Subject DN (from §12.2.3)

| Attribute | Value |
|---|---|
| org-unit-name (reserved) | `jf-anchor-icac` |
| Reference table | Table 87, "Standard DN Object Identifiers" |

### ICA CSR (from §12.2.5.4)

- Format: PKCS #10
- Encoding: DER-encoded string
- Signature algorithm: per RFC 2986 section 4.2, `SignatureAlgorithm`
- Carried in: `ICACCSR` parameter of the `ICACCSRResponse` command
- Certificate encoding fields: `ec-pub-key`, `pub-key-algo`, `ec-curve-id`

### NOC Subject DN (from §12.2.5, §12.2.5.4)

- Must encode `matter-fabric-id` attribute with value identical to the `matter-fabric-id` in `JFCrossSignedICAC`
- Subject DN for Anchor Fabric ICAC: must encode `matter-fabric-id` equal to the Fabric ID of the Anchor Fabric

### AddNOC Parameters (from §12.2.5)

| Parameter | Requirement |
|---|---|
| `NOCValue` | NOC issued by `JFCrossSignedICAC` |
| `ICACValue` | Set to the value of `JFCrossSignedICAC` |
| `IPKValue` | Set to the IPK from `GroupKeySetList` in Joint Fabric Datastore of Fabric A |
| `CaseAdminSubject` | Administrator CAT (version from `GroupList` in Joint Fabric Datastore of Fabric B) |
| `AdminVendorId` | Value of `AdminVendorIdValue` from local Fabric Table under `AdministratorFabricIndex` |

### Error / Status Codes (from §12.2.5, §12.2.6)

| Condition | Status Code |
|---|---|
| AddNOC: NOC missing Administrator CAT | `InvalidNOC` (via `NOCResponse`, per §11.18.6.7.2) |
| AddNOC: `CaseAdminSubject` missing Anchor CAT | `InvalidAdminSubject` (per §11.18.6.7.2) |
| Transfer Anchor: no user consent on Administrator A side | `TransferAnchorStatusNoUserConsent` (via `TransferAnchorResponse`) |
| Transfer Anchor: Datastore has Pending or DeletePending entries | `TransferAnchorStatusDatastoreBusy` (via `TransferAnchorResponse`) |

### Relevant Commands (from §12.2.5, §12.2.6, §12.2.7)

| Command | Cluster / Section |
|---|---|
| `OpenJointCommissioningWindow` | §11.26.6.5 |
| `ICACCSRRequest` | §11.26.6.1 |
| `ICACCSRResponse` | §11.26.6.2 |
| `AddICAC` | §11.26.6.3 |
| `AnnounceJointFabricAdministrator` | §11.26.6.9 |
| `TransferAnchorRequest` | §11.26.6.6 |
| `TransferAnchorResponse` | §11.26.6.7 |
| `TransferAnchorComplete` | §11.26.6.8 |
| `AddNOC` | §11.18 (Operational Credentials cluster) |
| `RemoveFabric` | §11.18.6.12 |
| `AddTrustedRootCertificate` | §11.18.6.13 |

### Relevant Attributes (from §12.2.5, §12.2.6)

| Attribute | Cluster / Section |
|---|---|
| `AdministratorFabricIndex` | Joint Fabric Administrator cluster, §11.26.5.1 |
| `GroupList` | Joint Fabric Datastore |
| `GroupKeySetList` | Joint Fabric Datastore |
| `AnchorNodeId` | Joint Fabric Datastore |
| `NodeList` | Joint Fabric Datastore Cluster, §11.25 |
| `TrustedRootCertificates` | Operational Credentials cluster, §11.18.5.5 |

---

## 5. Security Considerations

The following security-relevant requirements appear in the provided text:

- **Administrator CAT as proof of authority**: Any client that discovers an Administrator Node via DNS-SD and connects via CASE SHALL check if the Administrator CAT is present in the NOC using the Peer CASE Authenticated Tags before taking any action on the Node. [12.13]
- **Anchor CAT chain verification**: Any client that discovers an Anchor Node via DNS-SD and connects via CASE SHALL check if the Anchor CAT is present in the NOC using the Peer CASE Authenticated Tags and that the NOC chains up to the Anchor ICAC before taking any Joint Fabric actions on the Node. [12.18]
- **NOC issuance restriction**: A NOC containing the Anchor CAT SHALL be issued only by the Joint Fabric Anchor ICAC. [12.17]. The NOC containing the Administrator CAT SHALL be issued only by an Administrator. [12.15]
- **Anchor ICAC issuance restriction**: The Anchor ICAC SHALL be issued only by the Anchor CA to an Anchor Administrator. [12.9]
- **Vendor ID mutual validation**: The Joint Fabric requires that the fabrics participating in a Joint Fabric perform mutual Vendor ID validation using the Fabric Table Vendor ID Verification Procedure. [12.29]
- **ICA CSR signature validation**: Signature SHALL be validated using the trusted SubjectPublicKey. [12.34]
- **ICAC cross-signing key match**: Values for ec-pub-key, pub-key-algo, ec-curve-id SHALL match with the ones of the trusted SubjectPublicKey. [12.34]
- **User consent for Anchor role**: Assignment of the Anchor Administrator role SHALL be based on the intent and consent of the user; user consent SHALL be mutual on both Administrator A and Administrator B sides. [12.35, 12.38]
- **Administrator removal — ICAC revocation limitation**: Matter does not currently include any method for a Trusted Root Certificate to revoke an ICAC previously issued. The Anchor Administrator SHOULD trigger a transition to a new Trusted Root Certificate to ensure proper fail-proof removal of a Joint Fabric Administrator. [12.42]
- **Unauthorized Administer access mitigation**: Concern for unauthorized Administer access via Administrator CAT is mitigated by the fact that the Administrator CAT is a special subject distinguished name within the NOC and that the NOC SHALL be issued only by an Administrator. [12.15]

---

## 6. Error Handling

The following error handling rules appear in the provided text:

- **Node ID out of range or duplicate**: The Node ID SHALL be regenerated if the constraints (greater than `0x0000_0000_0000_0000`, less than `0xFFFF_FFEF_FFFF_FFFF`, unique in NodeList) are not met. [12.7]
- **Joint Fabric Administrator cluster not found**: If the Joint Fabric Administrator cluster is not found on Ecosystem B Administrator's endpoints, the process SHALL be terminated and the User SHALL be informed that Ecosystem B lacks the expected functionality. [12.24]
- **Administrator CAT not found in NOC (AddNOC)**: If the NOC doesn't contain an Administrator CAT then this command SHALL process an error by responding with a NOCResponse with a StatusCode of `InvalidNOC`. The process SHALL be terminated and the User SHALL be informed. [12.24]
- **Anchor CAT not found in CaseAdminSubject (AddNOC)**: If the CaseAdminSubject doesn't contain an Anchor CAT then this command SHALL process an error by responding with a StatusCode of `InvalidAdminSubject`. The process SHALL be terminated and the User SHALL be informed. [12.24]
- **Vendor ID verification failure**: If verification fails, the process SHALL be terminated and the User SHALL be informed that Ecosystem B Administrator is not trusted. [12.24]
- **RootPublicKey / FabricID mismatch**: If verification fails, the process SHALL be terminated and the User SHALL be informed that Ecosystem B Administrator is not trusted. [12.24]
- **ICAC cross-signing error**: In case of error, the process SHALL be terminated and the User SHALL be informed. [12.24]
- **ICA CSR validation failure**: If any of the above validation checks fail, the server SHALL immediately terminate the procedure and inform the User. [12.34]
- **Transfer Anchor — no user consent**: Section 11.26.6.7, "TransferAnchorResponse" command with StatusCode set to `TransferAnchorStatusNoUserConsent` SHALL be sent and the procedure stopped. [12.39]
- **Transfer Anchor — Datastore busy (Pending or DeletePending entries)**: Section 11.26.6.7, "TransferAnchorResponse" command with StatusCode set to `TransferAnchorStatusDatastoreBusy` SHALL be sent and the procedure stopped. [12.39]
- **Administrator B NOC missing Administrator CAT (during Transfer Anchor)**: Administrator A SHALL check that the NOC used by Administrator B during the CASE session contains an Administrator CAT. (Failure handling not further specified in the provided text beyond the check itself.) [12.39]