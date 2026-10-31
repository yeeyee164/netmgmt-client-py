# Application Services Layer Review: Architectural Concerns

## Document purpose

This document records broader design risks and future-facing concerns
for the Application Services Layer in `yeeyee164/netmgmt-client-py`.

-   **Audience:** maintainers, architects, and AI coding agents
-   **Scope:** separation of responsibilities, extensibility, security,
    and readiness for GUI/API consumers
-   **Status:** design guidance, not a declaration that all listed
    capabilities are currently implemented

------------------------------------------------------------------------

## AC-001: `DeviceOperationService` has multiple responsibilities

**Priority:** P2

### Concern

The operation service appears to coordinate endpoint resolution,
DTO-to-internal operation mapping, session/security/schema
configuration, worker selection, unary and batch execution, result
mapping, and optional output handling.

### Why it matters

A service that owns all of these concerns can become difficult to test
and change. Adding new protocols or operation types may cause unrelated
behavior to change in the same module.

### Direction

Do not split the service into many abstractions prematurely. As
complexity increases, consider extracting focused collaborators:

-   `OperationRequestMapper`: application DTO to internal semantic
    operation
-   `SessionConfigFactory`: build connection/session/execution
    configuration
-   `ExecutionCoordinator`: select and invoke the execution worker
-   `ExecutionResultMapper`: convert internal outcomes to stable
    application results

Keep orchestration in the application service. Extract a collaborator
when it has a clear responsibility, stable contract, and independently
testable behavior.

### Decision trigger

Extract a component when it has multiple callers, independently
meaningful rules, or tests that are difficult to write because too many
concerns are coupled together.

------------------------------------------------------------------------

## AC-002: CLI-oriented output handling leaks into the application layer

**Priority:** P1 for GUI/API integration

### Concern

The operation service can optionally use an `OutputHandler`, which is
oriented toward CLI or file output.

### Why it matters

A web API should not need terminal-oriented formatting or output side
effects to execute a network operation. Otherwise, GUI integration
becomes coupled to CLI behavior and structured results may vary
depending on whether an output handler is supplied.

### Direction

The application service should return structured result DTOs independent
of the presentation channel. The CLI adapter should format those results
for the terminal or files; the web adapter should serialize them as JSON
or stream events.

A presentation adapter may still use an output sink, but it should not
define the core operation's success semantics.

### Acceptance criteria

-   [ ] The same request yields the same structured execution outcome
    regardless of CLI output configuration.
-   [ ] The application service can be tested without a terminal or CLI
    output handler.
-   [ ] CLI formatting remains in the CLI/presentation layer.
-   [ ] Web serialization is implemented in the web/API adapter.

------------------------------------------------------------------------

## AC-003: Device registry persistence and secret handling are separate concerns

**Priority:** P2 initially; security controls are required before
exposing a network API

### Concern

`DeviceRegistryService` is currently an in-memory registry. This can be
sufficient for an initial local prototype, but it does not provide
persistence across restarts or a complete credential-management
boundary.

### Why it matters

A GUI user may expect saved device profiles to survive restarts. Storing
passwords or private-key material directly in ordinary profile DTOs also
increases the chance of secrets being logged, serialized, or returned
accidentally.

### Direction

Keep the registry service as the application-facing abstraction. Add a
repository/storage implementation when persistence is required.
Introduce a credential-provider or secret-store abstraction when
credentials need to be stored or retrieved securely.

Separate:

-   non-secret connection profile data;
-   secret references or credential handles;
-   actual secret material, which should be resolved only when needed.

### Security requirements before web exposure

-   [ ] Never include secret material in API responses.
-   [ ] Redact secrets from logs and exception messages.
-   [ ] Authorize profile reads and mutations.
-   [ ] Verify TLS certificates and SSH host keys according to the
    selected protocol.
-   [ ] Protect persistent secrets using an appropriate secret store or
    OS-level mechanism.
-   [ ] Avoid logging raw protocol payloads if they may contain
    credentials or sensitive configuration.

------------------------------------------------------------------------

## AC-004: Schema catalog needs to support browsing and form generation, not only validation

**Priority:** P2

### Concern

`SchemaCatalogService` provides a foundation for schema validation,
cached modules, database statistics, and profile module listing. A
schema-driven GUI will need more than a yes/no validation result.

### Why it matters

A useful YANG-based UI needs to discover schema nodes, types,
constraints, descriptions, choices, list keys, defaults, and
operation-specific editability. Without this information, the GUI will
either duplicate schema knowledge or rely on users to enter raw paths
and values.

### Direction

Evolve the schema service through explicit use cases, such as:

-   list available modules and revisions;
-   browse schema paths and nodes;
-   retrieve node type and constraints;
-   expose descriptions, defaults, units, ranges, patterns, choices, and
    list keys;
-   validate a proposed operation and return structured path-specific
    diagnostics;
-   distinguish read paths from writable configuration paths where the
    schema/model supports that distinction.

Do not force every UI-specific representation into the schema engine.
Return protocol/schema facts from the application service and let the
presentation layer decide how to render them.

### Acceptance criteria for future GUI work

-   [ ] A client can browse schema nodes without accessing internal
    libyang objects directly.
-   [ ] Validation errors identify the relevant path and constraint.
-   [ ] Module/revision information is explicit.
-   [ ] Missing or unsupported schemas produce a structured result.
-   [ ] Concurrent schema requests and cache/database access are tested.

------------------------------------------------------------------------

## AC-005: Protocol independence must preserve protocol-specific semantics

**Priority:** P1 when accepting requests from a public API

### Concern

The application layer should present a coherent interface, but gNMI and
NETCONF do not have identical operation models.

Examples include:

-   gNMI path and encoding behavior;
-   gNMI update/delete semantics;
-   NETCONF datastore selection and filters;
-   NETCONF lock/unlock and commit/discard-changes semantics;
-   confirmed-commit parameters and lifecycle;
-   different capability and error representations.

### Why it matters

An overly generic DTO can accept a request that has no valid meaning for
the selected protocol. A generic-looking interface must not imply false
feature parity.

### Direction

Use shared semantic models where the meaning is genuinely shared, and
explicit protocol-specific options or validation where behavior differs.
Reject unsupported combinations before connecting to a device.

Document capability-dependent operations, especially transactional
operations whose availability and behavior depend on the server.

### Acceptance criteria

-   [ ] Every operation has a documented protocol support matrix.
-   [ ] Unsupported combinations fail early with a stable error code.
-   [ ] Protocol-specific options are validated by the selected protocol
    adapter.
-   [ ] Shared DTOs do not erase meaningful differences in datastore,
    filter, path, encoding, or transaction behavior.

------------------------------------------------------------------------

## AC-006: Long-running subscriptions require an explicit resource model

**Priority:** P1 for web GUI use

### Concern

Subscriptions are long-lived resources, unlike ordinary unary requests.
They need ownership, state, cancellation, event delivery, and cleanup
semantics.

### Why it matters

A GUI can refresh, disconnect, reconnect, open multiple tabs, or stop
consuming events. Without explicit lifecycle and ownership rules, the
service can leak worker threads, queues, and network sessions.

### Direction

Treat a subscription as an application resource with:

-   opaque ID;
-   owner/authorization context;
-   explicit lifecycle state;
-   creation time and terminal time;
-   stop/cancel operation;
-   structured terminal error;
-   bounded event delivery;
-   cleanup/expiry policy;
-   limits on concurrent subscriptions.

Do not equate a queue or thread with the full subscription contract.

------------------------------------------------------------------------

## AC-007: A web API introduces security and operational boundaries

**Priority:** P1 before exposing mutation operations

### Concern

A local CLI and a remotely accessible web API have different trust
boundaries. The API may expose powerful network operations to
authenticated users, browsers, and automated clients.

### Required design questions

-   How are users authenticated?
-   Which users can read or mutate which device profiles?
-   Which users can execute configuration changes?
-   How are credentials and private keys referenced?
-   Are device endpoints restricted to prevent unintended access to
    arbitrary hosts?
-   How are long-running jobs and subscriptions owned and cleaned up?
-   How are concurrency, payload size, and request rate limited?
-   How are destructive operations confirmed and audited?

### Direction

Before enabling writes, define authentication and authorization, secret
redaction, transport security, operation limits, audit events, and
safeguards for mutations. For configuration changes, consider schema
validation, preview/diff, explicit confirmation, and
protocol-appropriate transaction semantics.

------------------------------------------------------------------------

## AC-008: Result DTOs should be stable, structured, and presentation-neutral

**Priority:** P1 for API integration

### Concern

If results are primarily strings or depend on the output handler, CLI
and GUI consumers will need to parse presentation text or duplicate
result interpretation.

### Direction

Define stable result fields where appropriate, for example:

-   request or operation ID;
-   target/device reference;
-   protocol;
-   operation type;
-   status;
-   start/end timestamps and duration;
-   structured data payload;
-   structured error code/message/details;
-   warnings and partial-result information.

Keep protocol-specific raw data available where useful, but do not
require the UI to parse terminal-formatted text.

------------------------------------------------------------------------

## Suggested sequence

1.  Stabilize request validation and execution result contracts.
2.  Remove CLI output concerns from the core application-service path.
3.  Define subscription ownership, lifecycle, and backpressure.
4.  Add persistence and secret-provider abstractions when product
    requirements demand them.
5.  Expand schema browsing APIs when implementing schema-driven forms.
6.  Add web authentication, authorization, audit, and operation
    safeguards before remote access or configuration writes.
