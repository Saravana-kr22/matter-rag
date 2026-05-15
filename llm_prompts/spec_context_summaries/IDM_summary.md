# Matter Spec Summary: IDM Protocol

**Source sections matched:** 68  
**Source chars sent to LLM:** 67,717  
**Generated:** 2026-04-28 17:26:22  
**Summary words:** 7,430  

---

## 1. Overview

The Interaction Model (IDM) Specification defines a layer that abstracts interactions from other layers, including security, transport, message format, and encoding. Its purpose is to define interactions, transactions, and actions between nodes in the Matter ecosystem.

The specification is part of a package of Data Model specifications that are agnostic to underlying layers (encoding, message, network, transport). This package as a whole is independently maintained and may be referenced by inclusion in vertical protocol stack specifications.

The package includes:
- **Data Model** — defines first-order elements and namespace for endpoints, clusters, attributes, data types, etc.
- **Interaction Model** — defines interactions, transactions, and actions between nodes.
- **System Model** — defines relationships managed between endpoints and clusters.
- **Cluster Library** — reference library of cluster specifications.
- **Device Library** — reference library of device type definitions.

The original baseline comes from the Zigbee Cluster Library (ZCL) Chapter 2 relating to ZCL commands and interactions. Gaps addressed include: multi-element message support, synchronized reporting, reduced message types, complex data type support in all messages, events, and interception attack mitigation.

Key roles:
- **Initiator**: the node that starts the first action of an interaction/transaction.
- **Target**: the destination of the first action (either a node or group).
- **Subscriber**: a node that creates a subscription in a Subscribe interaction.
- **Publisher**: the target node in a Subscribe interaction that sends reports.

Key path concepts defined (Wildcardable, Concrete Path, Existent Path, Group Path, Wildcard Path, Attribute Path, Request Path, Command Path, Event Path).

---

## 2. Protocol Details

### Interaction, Transaction, and Action Hierarchy

- An **interaction** is a sequence of one or more transactions between nodes, occurring in the context of an accessing fabric or no fabric.
- A **transaction** is a sequence of one or more actions; actions are defined as first or following.
- An **action** is a single logical communication from a source node to one or more destination nodes, conveyed by one or more messages.

The first action of a transaction is initiated by a single node. An action's target destination is either a single node (unicast) or a group of nodes (groupcast).

### Interaction Types

| Interaction | Transactions | Description |
|---|---|---|
| Read Interaction | Read | Request for cluster attributes and/or event data |
| Subscribe Interaction | Subscribe, Report | Subscribes to cluster attributes and/or event data |
| Write Interaction | Write | Modifies cluster attributes |
| Invoke Interaction | Invoke | Invokes cluster commands |

### Action Types

| Action | Description | Outgoing Message |
|---|---|---|
| Status Response Action | Success or error response | Unicast |
| Read Request Action | Request for attribute data and/or events | Unicast |
| Report Data Action | Responds to Read Request or Subscribe Request | Unicast |
| Subscribe Request Action | Request for subscription to attribute data and/or events | Unicast |
| Subscribe Response Action | Response to Subscribe Request | Unicast |
| Write Request Action | Request to modify cluster attribute data | Unicast / Groupcast |
| Write Response Action | Responds to Write Request | Unicast |
| Invoke Request Action | Executes a cluster command | Unicast / Groupcast |
| Invoke Response Action | Responds to Invoke Request with cluster-defined responses | Unicast |
| Timed Request Action | Indicates another action will take place within a Timed interval | Unicast |

### Transaction Types

| Transaction | Description |
|---|---|
| Read Transaction | Request for cluster attribute and/or event data |
| Subscribe Transaction | Creates a subscription to cluster attributes and/or events |
| Report Transaction | Maintains a subscription for the Subscribe interaction |
| Write Transaction | Modifies cluster attributes |
| Invoke Transaction | Invokes cluster commands |

### Path Mechanics

- A **concrete path** has no group IDs or wildcards and indicates a single element instance (event, command, attribute, struct field, or list entry).
- An **existent path** is a concrete path indicating a single existing instance on the node.
- A **group path** targets endpoints that are members of a group using a group ID; resolves into zero or more paths.
- A **wildcard path** has a wildcard endpoint and/or cluster indication; resolves into zero or more paths.
- A **request path** is either a concrete, group, or wildcard path.
- **Request Path Expansion** expands a request path into a list of existent paths. Group paths are first replaced with per-endpoint paths; wildcard paths are expanded by permuting wildcarded elements with existent elements.

### Read Interaction Flow

```
Read Request (Initiator → Target)
Report Data   (Initiator ← Target)
```

### Subscribe Interaction Flow

```
Subscribe Request  (Initiator → Target)
Report Data        (Initiator ← Target)   [primes subscription]
Status Response    (Initiator → Target)
Subscribe Response (Initiator ← Target)   [activates subscription]

[then periodically:]
Report Data        (Initiator ← Target)
Status Response    (Initiator → Target)
```

The Subscribe interaction begins with one Subscribe transaction followed by a periodic sequence of Report transactions. Each Report transaction reports delta changes in subscription data since the last Report transaction, except for attributes with the Changes Omitted (C) quality.

### Write Interaction Flows

**Timed Write Transaction:**
```
Timed Request (Initiator → Target)
Status Response (Initiator ← Target)
Write Request   (Initiator → Target)
Write Response  (Initiator ← Target)
```

**Untimed Write Transaction:**
```
Write Request  (Initiator → Target)
Write Response (Initiator ← Target)
```

### Message / Action Layer Interface

The message layer below this interaction layer encodes an action into one or more messages and delivers them to the destination. Action information is passed through some interface (not defined in the IDM spec) to/from the message layer. The protocol layers below this layer MAY have constraints that only support a subset of the functionality described here.

---

## 3. Normative Requirements

### 8.1.2. Scope & Purpose

- "This package, as a whole, **SHALL** be independently maintained as agnostic and decoupled from lower layers."

### 8.2.1.1. Concrete Path

- "A concrete path **SHALL NOT** have group IDs or wildcards."
- "A concrete path **SHALL** indicate a single element instance that is either: an event with the path ending in an event ID / a command with the path ending in a command ID / an attribute with the path ending in an attribute ID / a struct field with the path ending in a field ID / a list entry with the path ending in a list entry index."

### 8.2.1.3. Group Path

- "A group path **SHALL** resolve into zero or more paths."
- "A group path **SHALL** include a group ID that indicates zero or more endpoints that are members of the group."
- "A group path **MAY** include a wildcard cluster indication and therefore also be a Wildcard Path."

### 8.2.1.4. Wildcard Path

- "A wildcard path **SHALL** resolve into zero or more paths."
- "A wildcard path **SHALL** indicate zero or more element instances."
- "A wildcard path **MAY** include a group ID and therefore also be a Group Path."

### 8.2.1.5. Request Path

- "A request path **SHALL** be either a concrete path, a group path or a wildcard path."

### 8.2.1.6. Request Path Expansion

- "If the path is a Group Path, it **SHALL** be replaced with a list of paths, one for each endpoint that is a member of the group on the target node."
- "All concrete paths that are not existent paths in the list generated by the above-mentioned group-to-endpoint path expansion **SHALL** be removed."
- "Each path in the list that is a Wildcard Path **SHALL** be expanded into a complete list of existent paths."
- "When this expansion is performed for an Attribute read or subscription use case, any paths that must be omitted due to the processing of the Attribute Wildcard Path Flags **SHALL** be excluded from the resulting list."
- "For other interactions, the Attribute Wildcard Path Flags **SHALL** be ignored."

### 8.2.1.7. Attribute Path — WildcardPathFlags

- "When expanding a wildcard path indicated in an AttributePathIB into one or more concrete paths, paths **SHALL** be omitted from the result set if the WildcardFilterConfigurationVersion is greater than or equal to the current ConfigurationVersion in the Basic Information cluster and they satisfy any of the conditions below..." (conditions include: WildcardSkipRootNode bit targets endpoint 0; WildcardSkipGlobalAttributes bit for GeneratedCommandList/0xFFF8, AcceptedCommandList/0xFFF9, AttributeList/0xFFFB; WildcardSkipAttributeList bit for AttributeList/0xFFFB; WildcardSkipCommandLists bit for GeneratedCommandList or AcceptedCommandList; WildcardSkipCustomElements bit for MEI-prefixed elements; WildcardSkipFixedAttributes bit for Fixed (F) quality attributes; WildcardSkipChangesOmittedAttributes bit for Changes Omitted (C) quality attributes; WildcardSkipDiagnosticsClusters bit for Diagnostics (K) quality clusters.)
- "**WildcardPathFlags** **SHALL** **ONLY** be used for either Read or Subscribe interactions."
- "...the client **SHALL** tolerate the inclusion of reports for paths that would otherwise be omitted by servers compliant with this feature."

### 8.2.1.8. Command Path

- The endpoint field "is wildcardable, though this may be disallowed in the various uses of the Command Path in different actions and contexts."

### 8.2.1.9. Event Path

- "An event path **SHALL NOT** be a group path."

### 8.2.3. Transaction

- "The first action of a transaction **SHALL** be initiated by a single node."
- "An action in a transaction **SHALL** have a target destination that is either a single node, called a unicast action or a group of nodes, called groupcast action."

### 8.2.3.1. Transaction ID

- "All following actions in a transaction **SHALL** have the same transaction ID as the first action."
- "A groupcast action **SHALL** end a transaction and any subsequent action in the interaction **SHALL NOT** use the same transaction ID."

### 8.2.5.2. Outgoing Action

- "Each generated action **SHALL** provide the action information above to the message layer."
- "If the action is the first action of a transaction, the TransactionID **SHALL** be a value that uniquely identifies the transaction on the source of the action."
- "If the action is a following action, the TransactionID **SHALL** be the same as the TransactionID in the first action of the transaction."
- "If the action is a unicast following action the DestinationNode **SHALL** be the SourceNode of the previous action in the transaction."
- "The generated action information **SHALL** be submitted to the message layer."
- "Upon receipt of this action information, the message layer **SHALL** construct and convey one or more messages for this action to the target."
- "If the message layer encounters an error that prevents the complete construction, encoding and/or conveyance of the action, then the message layer **SHALL** inform this layer of the error."
- "If the action is not completely conveyed, the action, with the associated transaction and interaction, **SHALL** terminate."
- "If the failed action is NOT a Status Response action, this layer **SHOULD**, if possible, submit a Status Response action to the message layer, with a status code of FAILURE and the same TransactionID."

### 8.2.5.3. Incoming Action

- "If the message layer receives a valid message for an action, it **SHALL** be delivered to this layer with the action information above."
- "If this layer receives a message for an action that is not expected semantically, has invalid action information, or has an error not described in this specification, a Status Response action with an INVALID_ACTION Status Code **SHALL** be generated as defined in Status Response Action, and the associated transaction and interaction **SHALL** terminate."
- "When informed of an error from a message layer, the action, with the associated transaction and interaction, **SHALL** terminate."
- "If the action is not able to be executed due to insufficient resources, a Status Response **SHALL** be sent to the initiator with a status code of either: PATHS_EXHAUSTED if there are not enough resources to support the number of paths in the action information, and the number of paths in the action exceeds the number of paths that is guaranteed to be supported for the action (see Interaction Model Limits), BUSY in all other recoverable resource exhausted situations (e.g. if too many Read interactions are already in progress), or RESOURCE_EXHAUSTED for any other resource insufficiency, and the interaction **SHALL** be terminated."

### 8.3.1.2. Outgoing Status Response Action

- "This action **SHALL** be unicast."
- "This action **SHALL NOT** be generated in response to a groupcast."
- "This action **SHALL** be generated as specified in interactions defined here."
- "If this action is generated with an error Status, the current transaction and interaction **SHALL** be terminated."
- "This action **SHALL** only be generated with an error Status when an error occurs as a result of the immediate previous received action in the current transaction."
- "This action's DestinationNode field **SHALL** be the immediate previous received action's SourceNode."
- "This action's TransactionID field **SHALL** be the immediate previous received action's TransactionID."
- "If there is no well-defined Status Code for an error or exception, the Status Code of FAILURE **SHALL** be used."

### 8.3.1.3. Incoming Status Response Action

- "Upon receipt of this action with a success Status Code, this layer **SHALL** consume the status and continue the current transaction and interaction."
- "Upon receipt of this action with an error Status, this layer **SHALL** terminate the current transaction and interaction."
- "Upon receipt of this action with an error Status, this layer **SHALL** submit the error to the layer above."

### 8.4.2.2. Outgoing Read Request Action

- "This action **SHALL** be unicast."
- "This action **SHALL** be generated as the first action in a Read transaction."
- "A valid AttributePathIB for attribute data **SHALL** be one in the table Valid Read Attribute Paths."
- "A valid EventPathIB for an event **SHALL** be one in the table Valid Event Paths."
- "A path indicated in AttributeRequests or EventRequests **SHALL NOT** target a group."

### 8.4.2.3. Incoming Read Request Action

- "Upon receipt of this action, this layer **SHALL** generate a Report Data action to the subscriber, as defined in Incoming Read Request and Subscribe Request Action Processing."
- "If the Report Data was generated successfully, it **SHALL** be submitted to the message layer."

### 8.4.3.2. Incoming Read Request and Subscribe Request Action Processing

- "Each path indicated by the Report Data action **SHALL** be a Concrete Path."
- "Each request path in the AttributeRequests field **SHALL** be processed as follows..."
- "If the path does not conform to Valid Read Attribute Paths then: a Status Response with the INVALID_ACTION Status Code **SHALL** be generated as defined in Status Response Action, a Report Data action **SHALL NOT** be generated, and this interaction and process **SHALL** terminate."
- "Execute the ACL Access Granting Algorithm against the concrete path, assuming the required_privilege for the element is View..."
- "If the outcome is AccessDenied, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ACCESS Status Code."
- "Else if the outcome is AccessRestricted, an AttributeStatusIB **SHALL** be generated with the ACCESS_RESTRICTED Status Code."
- "If the path indicates a node that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_NODE Status Code."
- "Else if the path indicates an endpoint that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ENDPOINT Status Code."
- "Else if the path indicates a cluster that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_CLUSTER Status Code."
- "Else if the path indicates an attribute or attribute data field that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ATTRIBUTE Status Code with the Path field indicating the first unsupported data field (not the entire attribute data path)."
- "Else if the path indicates an attribute that is not readable, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_READ Status Code."
- "Execute the ACL Access Granting Algorithm against the concrete path a second time, using the actual required_privilege for the attribute referenced in the path."
- "If an AttributeStatusIB was generated, the path **SHALL** be discarded."
- "If the path indicates attribute data that is not readable, then the path **SHALL** be discarded."
- "If the outcome is either AccessDenied or AccessRestricted, then the path **SHALL** be discarded."
- "If no error-free existent paths remain, then AttributeRequests are considered empty."
- "If the DataVersionFilters field indicates DataVersionFilterIB entries with a Path field that matches the path, where all matching entries have a DataVersion field that matches the data version of the cluster instance in the path, then the path **SHALL** be ignored."
- "Else an AttributeDataIB **SHALL** be generated with the Data and Path as indicated by the path being processed."
- "If the FabricFiltered parameter is true, or the attribute indicated by the path is fabric-sensitive, any such list **SHALL** be generated as a fabric-filtered list of entries."
- "Else, any such list **SHALL** be generated as an unfiltered list of entries, with each entry indicated as a fabric-sensitive struct."
- "Each AttributeDataIB or AttributeStatusIB generated from processing AttributeRequests **SHALL** be added to the AttributeReports action field in the Report Data action."
- (For EventRequests, parallel rules apply with EventStatusIB and UNSUPPORTED_EVENT for unsupported events.)
- "Each event record currently queued in the node, in order from lowest to highest event number, **SHALL** generate an EventDataIB except for any of the following: [filter conditions]."
- "Each information block generated from processing EventRequests **SHALL** be added to the EventReports action field in the Report Data action."
- "If this action is a Subscribe Request action, If both AttributeRequests and EventRequests are empty: a Status Response Action with the INVALID_ACTION Status Code **SHALL** be sent to the initiator, a Report Data action **SHALL NOT** be generated, and the interaction and process **SHALL** terminate."
- "Else if either MinIntervalFloor or MaxIntervalCeiling is missing, or MinIntervalFloor is greater than MaxIntervalCeiling: a Status Response Action with the INVALID_ACTION Status Code **SHALL** be sent to the initiator, a Report Data action **SHALL NOT** be generated, and the interaction and process **SHALL** terminate."
- "Else a SubscriptionId which uniquely identifies this subscription on the publisher **SHALL** be indicated in the Report Data action."
- "Else the SubscriptionId **SHALL** be omitted."

### 8.4.3.3. Outgoing Report Data Action

- "This action **SHALL** be unicast."
- "This action **MAY** have an empty list of AttributeReports and/or EventReports."
- "This action **SHALL NOT** include any nested attribute data field or nested event data field that is defined as fabric-sensitive, if the associated fabric for that field does not match the accessing fabric for the interaction."
- "SuppressResponse **MAY** be set to TRUE for a Report Data action that initiates a Report transaction that conveys an empty list of AttributeReports and EventReports, otherwise: SuppressResponse **SHALL** be set to TRUE for a Report Data action that is part of a Read transaction. SuppressResponse **SHALL** be set to FALSE for a Report Data action that is part of a Subscribe transaction."

### 8.4.3.4. Incoming Report Data Action

- "Upon receipt of this action, if SuppressResponse is TRUE, a response **SHALL NOT** be generated."
- "Otherwise a Status Response Action **SHALL** be generated with a status code of SUCCESS to continue the interaction, INVALID_SUBSCRIPTION if the action is part of a Subscribe interaction and the SubscriptionID is invalid, FAILURE to terminate the interaction."
- "The Status Response Action **SHALL** be submitted to the message layer to deliver to the source of this action."

### 8.5. Subscribe Interaction

- "The Subscribe interaction **SHALL** start with one Subscribe transaction followed by a periodic sequence of Report transactions (see Report Transaction)."
- "A Report transaction **SHALL** be initiated by a Report Data action as part of an active subscription for a Subscribe interaction."
- "All Report Data actions in a Subscribe interaction **SHALL** have the same SubscriptionId parameter value that uniquely identifies the interaction among all subscriptions on the publisher."
- "Each Report transaction in a subscription **SHALL** report the path for each delta change in the subscription data, including the attribute data that has changed and/or the event that has occurred, since the last Report transaction, with the exception of attribute data with the Changes Omitted (C) quality."
- "Each Report transaction initiated by the publisher **SHALL** complete successfully before another Report transaction is initiated by the publisher."
- "Each Report transaction **SHALL NOT** be initiated by the publisher until the minimum interval has expired since the last Report transaction in the subscription."
- "Attribute changes **SHALL** be delivered as soon as possible, taking into account the minimum interval."
- "Events **SHALL** always be queued and buffered."
- "Each Report containing events **SHALL** deliver queued events without reordering the queue."
- "Queued events **MAY** be opportunistically delivered whenever some other activity triggers a Report transaction."
- "Absent any such triggers, queued events **SHALL** be delivered in a Report transaction generated at the maximum interval."
- "When the IsUrgent flag is TRUE for a subscription's event path in the EventPathIB, the queueing of such an event **SHALL** trigger a Report transaction for the subscription, subject to all Report transaction rules."
- "If the subscriber does not receive a Report transaction within the maximum interval from the last Report Data, the subscriber **SHALL** terminate the Subscribe interaction."
- "If a node receives a Report Data action with an inactive SubscriptionId, a Status Response action **SHALL** be sent with an INVALID_SUBSCRIPTION Status Code."
- "If, in response to a Report Data action, the publisher receives a Status Response action with a status code that is not SUCCESS, the publisher **SHALL** terminate the Subscribe interaction."
- "If the publisher does not receive a Status Response action in response to a Report Data action with SuppressResponse set to FALSE, the publisher **MAY** terminate the Subscribe interaction or **SHALL** re-synchronize the subscription in the next Report Data transaction by: Including all subscription data to re-prime the subscription, or Including all deltas since the last successful Report Data transaction."
- "The subscriber **MAY** terminate the subscription and interaction by responding with a Status Response action with an INVALID_SUBSCRIPTION Status Code."
- "The publisher **MAY** terminate the subscription and interaction by not generating a Report transaction within the maximum interval."
- "When a Subscribe interaction is terminated on the publisher or subscriber, the subscription, identified by a SubscriptionId, **SHALL** also be terminated."

### 8.5.2.2. Outgoing Subscribe Request Action

- "This action **SHALL** initiate a Subscribe interaction."
- "A Subscribe Request action **SHALL** be unicast from the subscriber to the publisher."
- "This action **SHALL** be generated to initiate a Subscribe interaction."
- "This action **SHALL** include a requested ceiling (highest) maximum interval value as MaxIntervalCeiling."
- "This action **SHALL** include a requested floor (lowest) minimum interval value as MinIntervalFloor."
- "If the publisher is an intermittently connected device, the MinIntervalFloor **SHOULD** be 0."
- "At least one attribute or event **SHALL** be indicated in the action."
- "A valid AttributePathIB **SHALL** be one in the table Valid Read Attribute Paths."
- "A valid EventPathIB **SHALL** be one in the table Valid Event Paths."
- "A path indicated in AttributeRequests or EventRequests **SHALL NOT** target a group."

### 8.5.2.3. Incoming Subscribe Request Action

- "If KeepSubscriptions is FALSE, all existing or pending subscriptions on the publisher for this subscriber **SHALL** be terminated."
- "This layer **SHALL** process the Subscribe Request action as defined in Incoming Read Request and Subscribe Request Action Processing."

### 8.5.3.2. Outgoing Subscribe Response Action

- "Upon receipt of a successful Status Response action from the subscriber for the Report Data action that primes the subscription, this action **SHALL** be generated and submitted to the message layer to send to the subscriber."
- "This action **SHALL** be unicast."
- "The SubscriptionId value **SHALL** be the same as the one used in Report Data generated to prime this subscription."
- "The publisher **SHALL** compute an appropriate value for the MaxInterval field in the action. This **SHALL** respect the following constraint: MinIntervalFloor ≤ MaxInterval ≤ MAX(SUBSCRIPTION_MAX_INTERVAL_PUBLISHER_LIMIT, MaxIntervalCeiling)."
- "Upon sending a Subscribe Response action, the subscription, as indicated by the SubscriptionId, **SHALL** become active on the publisher with a min interval equal to the requested MinIntervalFloor and a max interval equal to the MaxInterval field in the response."

### 8.5.3.3. Incoming Subscribe Response Action

- "Upon receipt of a Subscribe Response action, the subscription, as indicated by the SubscriptionId, **SHALL** become active to the subscriber."

### 8.5.3.4. Subscription Activation

- "The paths to the subscription data **SHALL** only be error free existent paths generated from processing the Subscribe Request."
- "Subsequent ReportData actions, as part of the subscription, **SHALL** include the latest: EventNo associated with each node generating new events. DataVersion associated with each cluster where there are data changes."
- "The FabricFiltered parameter from the Subscribe Request **SHALL** remain in effect for all data reported during the interaction."
- "Upon subscription activation, the minimum and maximum interval parameters **SHALL** take effect to determine the timing and expectation of subsequent Report transactions."

### 8.7.1. Write Transaction

- "A Write interaction **SHALL** consist of one of the transactions shown below."
- "If there is a preceding successful Timed Request action, the following Write Request action **SHALL** be received before the end of the Timeout interval."
- "If there is a preceding successful Timed Request action, the Timeout interval **SHALL** start when the Status Response action acknowledging the Timed Request action with a success code is sent."
- "If there is a preceding successful Timed Request action, the Write Request action **SHALL** be unicast."
- "If there is not a preceding successful Timed Request action, the Write Request action **MAY** be groupcast."
- "A client **MAY** choose to use a Timed Write transaction even if the attribute does not have the Timed Interaction quality."
- "The server **SHALL** support a Timed Write transaction for all writeable attributes."

### 8.7.2.2. Outgoing Write Request Action

- "This action **SHALL** be generated as the first action in a Write transaction, or following a Timed Request action and successful Status Response action."
- "If this action is part of a Timed Write transaction, TimedRequest **SHALL** be TRUE, else FALSE."
- "If not part of a Timed Write transaction, this action **MAY** be groupcast."
- "If this action is groupcast, SuppressResponse **SHALL** be TRUE."

### 8.7.2.3. Incoming Write Request Action

- "If this action is not able to be executed because the maximum supported number of Write interactions is already in progress, then a Status Response action with the BUSY Status Code **SHALL** be submitted to the message layer and this interaction **SHALL** terminate."
- "If this action is part of a Timed Write transaction, and the Timeout has expired from the preceding Timed Request action, then a Status Response action with the TIMEOUT Status Code **SHALL** be submitted to the message layer and this interaction **SHALL** terminate."
- "If this action is part of a Timed Write transaction, and this action has TimedRequest set to FALSE, then a Status Response action with the TIMED_REQUEST_MISMATCH Status Code **SHALL** be submitted to the message layer and this interaction **SHALL** terminate."
- "If this action is marked with TimedRequest as TRUE but this action is not part of a Timed Write transaction (i.e. there was no corresponding Timed Request action prior to it matching the same TransactionID), then a Status Response action with the TIMED_REQUEST_MISMATCH Status Code **SHALL** be submitted to the message layer and this interaction **SHALL** terminate."
- "If this action was unicast and SuppressResponse is FALSE, a Write Response action **SHALL** be generated and submitted to the message layer to send to the initiator, otherwise no Write Response **SHALL** be sent."

### 8.7.3.2. Outgoing Write Response Action

- "This action **SHALL** be unicast."
- "If the path does not conform to Valid Write Attribute Paths then: a Status Response with the INVALID_ACTION Status Code **SHALL** be generated as defined in Status Response Action, a Write Response action **SHALL NOT** be generated, and this interaction and process **SHALL** terminate."
- "If the outcome is AccessDenied, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ACCESS Status Code."
- "Else if the outcome is AccessRestricted, an AttributeStatusIB **SHALL** be generated with the ACCESS_RESTRICTED Status Code."
- "If the path indicates a node that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_NODE Status Code."
- "Else if the path indicates an endpoint that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ENDPOINT Status Code."
- "Else if the path indicates a cluster that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_CLUSTER Status Code."
- "Else if the path indicates an attribute or attribute data field that is unsupported, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ATTRIBUTE Status Code with the Path field indicating only the path to the first unsupported data field (not the entire attribute data path)."
- "Else if the path indicates an attribute that is not writable, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_WRITE Status Code."
- "Execute the ACL Access Granting Algorithm against the concrete path a second time, using the actual required_privilege for the attribute referenced in the path."
- "If the outcome is AccessDenied, a AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ACCESS Status Code."
- "Else if the outcome is AccessRestricted, a AttributeStatusIB **SHALL** be generated with the ACCESS_RESTRICTED Status Code."
- "If the path indicates specific attribute data that requires a Timed Write transaction to write and this action is not part of a Timed Write transaction, an AttributeStatusIB **SHALL** be generated with the NEEDS_TIMED_INTERACTION Status Code."
- "Else if the attribute in the path indicates a fabric-scoped list and there is no accessing fabric, an AttributeStatusIB **SHALL** be generated with the UNSUPPORTED_ACCESS Status Code, with the Path field indicating only the path to the attribute."
- "Else if the DataVersion field of the AttributeDataIB is present and does not match the d..." *(spec text truncated at this point)*

### 8.10. Status Codes

- "These **MAY** be used by interaction model processing of actions and as common status codes for cluster specifications."
- "All values not defined here **SHALL** be reserved (per general conventions)."
- "Cluster specifications that wish to communicate a status not defined in this table **MAY** use a cluster-specific status code as described in Status Codes."

---

## 4. Data Structures

### Common Action Information Fields

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| InteractionModelRevision | uint8 | M | the revision number of the implemented Interaction Model specification under which the sending node was certified |
| Action | action-id | M | the action |
| TransactionID | trans-id | M | the transaction ID |
| FabricIndex | fabric-idx | M | the accessing fabric index, based on the session used to deliver the action |
| SourceNode | node-id | M | the node ID of the node that generates the action |
| DestinationNode | node-id | O.a | the node ID of the destination where the action is sent |
| DestinationGroup | group-id | O.a | the group ID of the destination where the action is sent |
| action specific | variable | M | specific action information described in each action section |

### Status Response Action Information

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| Status | status | M | a status code (see Status Codes) |

### Read Request Action Information

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| AttributeRequests | list[AttributePathIB] | M | a list of zero or more request paths to cluster attribute data |
| DataVersionFilters | list[DataVersionFilterIB] | AttributeRequests | a list of zero or more cluster instance data versions |
| EventRequests | list[EventPathIB] | M | a list of zero or more request paths to cluster events |
| EventFilters | list[EventFilterIB] | EventRequests | a list of zero or more minimum event numbers per specific node |
| FabricFiltered | bool | M | limits the data read within fabric-scoped lists to the accessing fabric |
| IncludeAttributionData | bool | O | if true, attribution information will be sent in report data |

### Report Data Action Information

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| SuppressResponse | bool | M | do not send a response to this action |
| SubscriptionId | uint32 | O | a SubscriptionId only used in a Subscribe interaction |
| AttributeReports | list[AttributeReportIB] | O | a list of zero or more attribute data reports |
| EventReports | list[EventReportIB] | O | a list of zero or more event reports |

### Subscribe Request Action Information

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| KeepSubscriptions | bool | M | false to terminate existing subscriptions from initiator |
| MinIntervalFloor | uint16 | M | the requested minimum interval boundary floor in seconds |
| MaxIntervalCeiling | uint16 | M | the requested maximum interval boundary ceiling in seconds |
| AttributeRequests | list[AttributePathIB] | O | a list of zero or more request paths to cluster attribute data |
| DataVersionFilters | list[DataVersionFilterIB] | AttributeRequests | a list of zero or more cluster instance data versions |
| EventRequests | list[EventPathIB] | O | a list of zero or more request paths to cluster events |
| EventFilters | list[EventFilterIB] | EventRequests | a list of zero or more minimum event numbers per specific node |
| FabricFiltered | bool | M | limits the data read within fabric-scoped lists to the accessing fabric |
| IncludeAttributionData | bool | O | if true, attribution information will be sent in report data |

### Subscribe Response Action Information

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| SubscriptionId | uint32 | M | identifies the subscription |
| MaxInterval | uint16 | M | the final maximum interval for the subscription in seconds |

### Write Request Action Information

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| SuppressResponse | bool | M | do not send a response to this action |
| TimedRequest | bool | M | flag action as part of a timed write transaction |
| WriteRequests | list[AttributeDataIB] | M | a list of one or more path and data tuples |

### Write Response Action Information

| Action Field | Type | Conformance | Description |
|---|---|---|---|
| WriteResponses | list[AttributeStatusIB] | O | a list of zero or more concrete paths indicating errors or successes |

### Status Code Table

| Value | Status Code | Summary |
|---|---|---|
| 0x00 | SUCCESS | Operation was successful. |
| 0x01 | FAILURE | Operation was not successful. |
| 0x7D | INVALID_SUBSCRIPTION | Subscription ID is not active. |
| 0x7E | UNSUPPORTED_ACCESS / NOT_AUTHORIZED | The sender of the action or command does not have authorization or access. NOT_AUTHORIZED is an obsolete name of this error code. |
| 0x7F | UNSUPPORTED_ENDPOINT | The endpoint indicated is unsupported on the node. |
| 0x80 | INVALID_ACTION | The action is malformed, has missing fields, or fields with invalid values. Action not carried out. |
| 0x81 | UNSUPPORTED_COMMAND / UNSUP_COMMAND | The indicated command ID is not supported on the cluster instance. Command not carried out. UNSUP_COMMAND is an obsolete name for this error code. |
| 0x82 | reserved | Deprecated: use UNSUPPORTED_COMMAND |
| 0x83 | reserved | Deprecated: use UNSUPPORTED_COMMAND |
| 0x84 | reserved | Deprecated: use UNSUPPORTED_COMMAND |
| 0x85 | INVALID_COMMAND / INVALID_FIELD | The cluster command is malformed, has missing fields, or fields with invalid values. Command not carried out. INVALID_FIELD is an obsolete name for this error code. |
| 0x86 | UNSUPPORTED_ATTRIBUTE | The indicated attribute ID, field ID or list entry does not exist for an attribute path. |
| 0x87 | CONSTRAINT_ERROR / INVALID_VALUE | Out of range error or set to a reserved value. Attribute keeps its old value. INVALID_VALUE is an obsolete name for this error code. |
| 0x88 | UNSUPPORTED_WRITE / READ_ONLY | Attempt to write a read-only attribute. READ_ONLY is an obsolete name for this error code. |
| 0x89 | RESOURCE_EXHAUSTED / INSUFFICIENT_SPACE | An action or operation failed due to insufficient available resources. INSUFFICIENT_SPACE is an obsolete name for this error code. |
| 0x8A | reserved | Legacy cluster specification error status code: use SUCCESS |
| 0x8B | NOT_FOUND | The indicated data field or entry could not be found. |
| 0x8C | UNREPORTABLE_ATTRIBUTE | Reports cannot be issued for this attribute. |
| 0x8D | INVALID_DATA_TYPE | The data type indicated is undefined or invalid for the indicated data field. Command or action not carried out. |
| 0x8E | reserved | Legacy cluster specification error status code: use UNSUPPORTED_ATTRIBUTE. |
| 0x8F | UNSUPPORTED_READ | Attempt to read a write-only attribute. |
| 0x90 | reserved | Deprecated: use FAILURE |
| 0x91 | reserved | Deprecated: use FAILURE |
| 0x92 | DATA_VERSION_MISMATCH | Cluster instance data version did not match request path |
| 0x93 | reserved | Legacy cluster specification error status code: use FAILURE |
| 0x94 | TIMEOUT | The transaction was aborted due to time being exceeded. |
| 0x95–0x9A | reserved | ZCL OTA Upgrade cluster specific error status codes |
| 0x9B | UNSUPPORTED_NODE | The node ID indicated is not supported on the node. |
| 0x9C | BUSY | The receiver is busy processing another action that prevents the execution of the incoming action. |
| 0x9D | ACCESS_RESTRICTED | The access to the action or command by the sender is permitted by the ACL but restricted by the ARL. |
| 0xC0–0xC2 | reserved | Deprecated: use FAILURE |
| 0xC3 | UNSUPPORTED_CLUSTER | The cluster indicated is not supported on the endpoint. |
| 0xC4 | reserved | Deprecated: use SUCCESS |
| 0xC5 | NO_UPSTREAM_SUBSCRIPTION | Used by proxies to convey to clients the lack of an upstream subscription to a source. |
| 0xC6 | NEEDS_TIMED_INTERACTION | A Untimed Write or Untimed Invoke interaction was used for an attribute or command that requires a Timed Write or Timed Invoke. |
| 0xC7 | UNSUPPORTED_EVENT | The indicated event ID is not supported on the cluster instance. |
| 0xC8 | PATHS_EXHAUSTED | The receiver has insufficient resources to support the specified number of paths in the request |
| 0xC9 | TIMED_REQUEST_MISMATCH | A request with TimedRequest field set to TRUE was issued outside a Timed transaction or a request with TimedRequest set to FALSE was issued inside a Timed transaction. |
| 0xCA | FAILSAFE_REQUIRED | A request requiring a Fail-safe context was invoked without the Fail-Safe context. |
| 0xCB | INVALID_IN_STATE | The received request cannot be handled due to the current operational state of the device |
| 0xCC | NO_COMMAND_RESPONSE | A CommandDataIB is missing a response in the InvokeResponses of an Invoke Response action. |
| 0xCD | TERMS_AND_CONDITIONS_CHANGED | The node requires updated TC acceptance. The user MAY be directed to visit the EnhancedSetupFlowMaintenanceUrl to complete this. |
| 0xCE | MAINTENANCE_REQUIRED | The node requires the user to visit the EnhancedSetupFlowMaintenanceUrl for instructions on further action. |
| 0xCF | DYNAMIC_CONSTRAINT_ERROR | The value for the data type was not accepted due to runtime validation issues. Command or action not carried out. |
| 0xD0 | ALREADY_EXISTS | Attempt to create an entity that already exists or create an entity with an identifier that is already in use. Command or action not carried out. |
| 0xD1 | INVALID_TRANSPORT_TYPE | Attempt to process on a transport type not valid for this element. Command or action not carried out. |

### Attribute Path Component Grammar

```
<path-component> ::= <attribute-id> *<nested-component>
<nested-component> ::= <field-id> | <entry-index>
```

*`<nested-component>` occurs zero or more times as defined in a cluster specification.*

### Command Path Component Grammar

```
<path-component> ::= <command-id>
```

### Event Path Component Grammar

```
<path-component> ::= <event-id>
```

### Attribute Wildcard Path Flag Trigger Attribute IDs

- GeneratedCommandList: `0xFFF8`
- AcceptedCommandList: `0xFFF9`
- AttributeList: `0xFFFB`

---

## 5. Security Considerations

The provided spec text addresses access control as part of interaction processing rather than session establishment. Key security-relevant requirements from the provided text:

- The **ACL Access Granting Algorithm** is executed against each concrete path during Read, Subscribe, and Write processing. For Read/Subscribe, it is run twice: first assuming View privilege to determine base access, then using the actual required_privilege for the attribute/event.
- **AccessDenied** outcomes generate UNSUPPORTED_ACCESS status codes and cause paths to be discarded.
- **AccessRestricted** outcomes generate ACCESS_RESTRICTED status codes and cause paths to be discarded.
- Fabric-sensitive data: "This action **SHALL NOT** include any nested attribute data field or nested event data field that is defined as fabric-sensitive, if the associated fabric for that field does not match the accessing fabric for the interaction."
- Fabric-scoped lists: if `FabricFiltered` is true or the attribute is fabric-sensitive, lists are generated as fabric-filtered. If a fabric-scoped list has no accessing fabric on a write, UNSUPPORTED_ACCESS is returned.
- An interaction occurs "in the context of an accessing fabric, or no fabric." How a fabric context is established is not defined in the provided text.
- Write to a fabric-scoped list with no accessing fabric generates an UNSUPPORTED_ACCESS AttributeStatusIB.
- Timed interactions provide protection for attributes that require them; writing such attributes without a Timed Write transaction generates NEEDS_TIMED_INTERACTION.
- The `ACCESS_RESTRICTED` status (0x9D): "The access to the action or command by the sender is permitted by the ACL but restricted by the ARL."

*(Session establishment transport constraints, authentication mechanisms, and the threat model are not covered in the provided spec sections.)*

---

## 6. Error Handling

### General Error Dispatch

- Any action with invalid semantics, invalid action information, or unspecified errors generates a Status Response action with **INVALID_ACTION** (0x80), terminating the transaction and interaction.
- Message layer errors that prevent complete receipt terminate the action, transaction, and interaction.
- Resource exhaustion generates:
  - **PATHS_EXHAUSTED** (0xC8) — insufficient resources for path count exceeding the guaranteed limit.
  - **BUSY** (0x9C) — other recoverable resource exhaustion (e.g., too many concurrent Read interactions).
  - **RESOURCE_EXHAUSTED** (0x89) — other resource insufficiency.
- If no well-defined status code applies, **FAILURE** (0x01) is used.

### Status Response Errors

- A Status Response with an error status terminates the current transaction and interaction, and the error is submitted to the layer above.
- A Status Response SHALL NOT be generated in response to a groupcast.
- If a failed action is not itself a Status Response, a Status Response with FAILURE SHOULD be generated.

### Read/Subscribe Processing Errors

- INVALID_ACTION — path does not conform to Valid Read Attribute Paths or Valid Event Paths; no Report Data generated; interaction terminates.
- UNSUPPORTED_ACCESS — ACL check denied at View privilege level or at actual privilege level.
- ACCESS_RESTRICTED — ARL restricts access.
- UNSUPPORTED_NODE (0x9B) — path indicates unsupported node.
- UNSUPPORTED_ENDPOINT (0x7F) — path indicates unsupported endpoint.
- UNSUPPORTED_CLUSTER (0xC3) — path indicates unsupported cluster.
- UNSUPPORTED_ATTRIBUTE (0x86) — attribute or attribute data field not found.
- UNSUPPORTED_READ (0x8F) — attribute is write-only / not readable.
- UNSUPPORTED_EVENT (0xC7) — cluster event is unsupported.
- INVALID_SUBSCRIPTION (0x7D) — SubscriptionID is not active in a Report Data action.
- For Subscribe: INVALID_ACTION if both AttributeRequests and EventRequests are empty, or if MinIntervalFloor > MaxIntervalCeiling, or if either floor/ceiling is missing.

### Write Processing Errors

- INVALID_ACTION — path does not conform to Valid Write Attribute Paths; no Write Response generated; interaction terminates.
- UNSUPPORTED_ACCESS, ACCESS_RESTRICTED — ACL/ARL checks.
- UNSUPPORTED_NODE, UNSUPPORTED_ENDPOINT, UNSUPPORTED_CLUSTER, UNSUPPORTED_ATTRIBUTE — element existence checks.
- UNSUPPORTED_WRITE (0x88) — attribute is not writable.
- NEEDS_TIMED_INTERACTION (0xC6) — attribute requires Timed Write but Untimed Write was used.
- UNSUPPORTED_ACCESS — fabric-scoped list write with no accessing fabric.
- DATA_VERSION_MISMATCH (0x92) — DataVersion field present but does not match *(spec text truncated before full description)*.
- BUSY (0x9C) — maximum number of concurrent Write interactions already in progress.
- TIMEOUT (0x94) — Timed Write transaction Timeout has expired.
- TIMED_REQUEST_MISMATCH (0xC9) — TimedRequest flag mismatch with actual Timed transaction state.
  - Note: "Devices certified prior to 1.4 MAY return UNSUPPORTED_ACCESS for this condition."

### Subscribe Lifecycle Errors

- If publisher receives a non-SUCCESS Status Response to a Report Data, the publisher **SHALL** terminate the Subscribe interaction.
- If subscriber does not receive a Report transaction within the maximum interval, the subscriber **SHALL** terminate the Subscribe interaction.
- If a Report Data arrives with an inactive SubscriptionId, a Status Response with **INVALID_SUBSCRIPTION** **SHALL** be sent.
- The subscriber **MAY** terminate by responding with INVALID_SUBSCRIPTION.
- The publisher **MAY** terminate by not generating a Report within the maximum interval.