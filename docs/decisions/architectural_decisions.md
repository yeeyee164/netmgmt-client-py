# Application Services Layer: Proposed Architectural Decisions

## Document purpose

This document turns the review into explicit decisions that maintainers
and AI coding agents can follow when implementing or extending the
Application Services Layer in `yeeyee164/netmgmt-client-py`.

These are **proposed decisions based on the review**, not evidence that
each decision has already been adopted or implemented. Maintainers
should update each decision's status when it is accepted, superseded, or
rejected.

## Decision status vocabulary

-   **Proposed:** recommended, awaiting maintainer agreement.
-   **Accepted:** explicitly approved by maintainers.
-   **Implemented:** code and tests are in place.
-   **Superseded:** replaced by a later decision.
-   **Rejected:** intentionally not adopted, with rationale recorded.

------------------------------------------------------------------------

## ADR-001: Keep the Application Services Layer as the UI boundary

**Status:** Proposed\
**Decision:** Keep the Application Services Layer between presentation
adapters and the existing protocol/execution engine.

### Context

A web GUI should not directly construct internal session configuration,
workers, or protocol operation objects. A CLI and a future web API
should be able to invoke the same application use cases.

### Decision

Use the following dependency direction:

``` text
Presentation adapters
  ├── CLI / YAML
  └── Web API / WebSocket / SSE
          |
          v
Application Services
  ├── Device registry use cases
  ├── Device operation use cases
  ├── Subscription lifecycle
  └── Schema catalog use cases
          |
          v
Existing engine
  ├── config/
  ├── managers/
  ├── core/
  └── specs/
          |
          v
Network devices
```

Presentation code may depend on application DTOs and service contracts.
The application layer may depend on internal engine abstractions. The
engine must not depend on a particular GUI framework.

### Consequences

-   CLI and web adapters can reuse application use cases.
-   The UI does not need to understand worker construction or low-level
    session configuration.
-   Application DTOs and service contracts become important
    compatibility surfaces and need tests.

### Acceptance criteria

-   [ ] The web/API adapter does not construct internal workers
    directly.
-   [ ] CLI and API call the same application use cases where behavior
    is shared.
-   [ ] Core protocol/execution modules do not import web framework
    modules.

------------------------------------------------------------------------

## ADR-002: Application services return structured results, not presentation output

**Status:** Proposed
**Decision:** Treat structured result DTOs as the primary output
contract. Keep terminal formatting and file output in presentation
adapters.

### Context

An optional CLI-oriented `OutputHandler` in the application execution
path can couple service behavior to terminal/file output and complicate
GUI integration.

### Decision

-   Application services return structured result objects.
-   The CLI adapter formats results for terminal or file output.
-   The web adapter serializes results into JSON or event messages.
-   Presentation-specific sinks must not alter the semantic
    success/failure result of an operation.

### Consequences

-   Results can be tested without a terminal.
-   GUI clients do not parse human-oriented strings.
-   Existing CLI behavior may need a small adapter/refactor to preserve
    output format.

### Acceptance criteria

-   [ ] Result semantics are identical with or without CLI output
    configuration.
-   [ ] Result DTOs have stable status and error fields.
-   [ ] CLI formatting is owned by the CLI adapter.

------------------------------------------------------------------------

## ADR-003: Apply request repetition in exactly one layer

**Status:** Implemented
**Decision:** Choose one authoritative repetition mechanism for
`request.times`.

### Context

Batch request expansion and `ExecutionConfig.times` may both represent
repetition, risking multiplicative execution.

### Decision

Before changing code, trace the complete path from DTO through config
construction to worker execution. Then choose one mechanism:

-   **Option A:** expand a request into multiple single-run requests and
    set each generated execution count to one; or
-   **Option B:** preserve one request and let the execution engine
    honor `ExecutionConfig.times`.

Do not implement both for the same request.

### Consequences

-   Execution counts become predictable.
-   Batch and unary behavior can be aligned.
-   Tests must assert actual worker invocation counts, not just the
    number of generated DTOs.

### Acceptance criteria

-   [x] `times=1` means one execution.
-   [x] `times=N` means exactly N executions within documented limits.
-   [x] Batch and unary semantics are documented.
-   [x] Tests prevent future double application.

------------------------------------------------------------------------

## ADR-004: Model subscriptions as explicit application resources

**Status:** Implemented
**Decision:** Give subscriptions explicit identity, lifecycle,
ownership, backpressure, and cleanup semantics.

### Context

A subscription can outlive the HTTP request or browser page that created
it. A thread flag and an event queue are not sufficient to define its
full lifecycle.

### Decision

Each subscription should have:

-   a collision-resistant opaque ID, preferably a full UUID;
-   explicit states such as `STARTING`, `RUNNING`, `STOPPING`,
    `STOPPED`, and `FAILED`;
-   a documented owner/authorization context;
-   a stop/cancel operation;
-   structured terminal error information;
-   bounded event buffering and a defined overflow policy;
-   cleanup and expiry rules;
-   configurable limits on active subscriptions.

### Consequences

-   Web clients can observe lifecycle changes consistently.
-   Resource leaks and queue growth are easier to detect.
-   The implementation may need a subscription manager/state model
    beyond a simple worker registry.

### Acceptance criteria

-   [x] State transitions are tested.
-   [x] Queue saturation follows a documented policy.
-   [x] Completed subscriptions release worker/session resources.
-   [ ] Subscription access is authorized independently of ID secrecy.
-   [ ] Active-subscription limits are enforced.

------------------------------------------------------------------------

## ADR-005: Validate all external requests before network I/O

**Status:** Proposed
**Decision:** Treat DTO construction and type hints as insufficient
validation for API-facing input.

### Context

The application layer will likely be called by a web API with untrusted
input. Protocol-specific constraints and bounds need to be checked
before a connection is opened.

### Decision

Validate timeout, repetition count, operation shape, payload types,
protocol/operation compatibility, filters/selectors, datastore names,
and protocol-specific options at the application boundary. Pydantic may
be used at the API boundary while internal dataclasses remain in place.

Validation should return stable, structured errors and must not require
a live device connection.

### Consequences

-   Invalid requests fail fast and predictably.
-   The API can map validation failures to suitable HTTP responses.
-   Shared semantic DTOs still need protocol-specific validation rules.

### Acceptance criteria

-   [ ] Invalid input fails before network I/O.
-   [ ] Error codes are stable and documented.
-   [ ] Resource limits are configurable.
-   [ ] Protocol support/compatibility is covered by tests.

------------------------------------------------------------------------

## ADR-006: Preserve shared semantics without pretending protocols are identical

**Status:** Proposed
**Decision:** Share only concepts that have equivalent meaning; retain
explicit protocol-specific behavior where required.

### Context

gNMI and NETCONF differ in path representation, data encoding, filters,
datastores, mutation semantics, and transaction operations.

### Decision

-   Keep genuinely common operation intent in shared application DTOs.
-   Put protocol-specific options and validation in explicit
    protocol-specific structures or adapters.
-   Maintain a protocol/operation support matrix.
-   Reject unsupported combinations before connecting to a device.
-   Treat server capabilities as relevant to capability-dependent
    operations.

### Consequences

-   The application API remains coherent without hiding protocol
    differences.
-   Some request types will contain explicit protocol-specific fields.
-   Tests need protocol-specific cases in addition to shared behavior
    tests.

### Acceptance criteria

-   [ ] Unsupported protocol/operation combinations fail early.
-   [ ] NETCONF transaction semantics are not represented as if they
    were universally supported by gNMI.
-   [ ] Protocol-specific filters, paths, and payloads are validated by
    the correct adapter.

------------------------------------------------------------------------

## ADR-007: Keep device registry storage replaceable

**Status:** Proposed
**Decision:** Keep `DeviceRegistryService` as the application-facing
abstraction and allow its backing storage to evolve.

### Context

An in-memory registry is adequate for a prototype but loses profiles on
process restart. Credential storage also has different security
requirements from ordinary profile metadata.

### Decision

Do not introduce a database solely for architectural fashion. When
persistence is required, add a repository/storage implementation behind
the registry service. When secret persistence is required, use a
separate credential-provider or secret-store abstraction rather than
treating secrets as ordinary profile fields.

### Consequences

-   The service API can remain stable while storage changes.
-   Persistence can be introduced when requirements justify it.
-   Secret resolution and profile persistence can be tested
    independently.

### Acceptance criteria

-   [ ] Service consumers do not depend on a concrete in-memory
    dictionary.
-   [ ] Secret material is never returned in ordinary profile DTOs.
-   [ ] Persistence and credential-provider behavior have independent
    tests when introduced.

------------------------------------------------------------------------

## ADR-008: Evolve schema service toward machine-readable schema browsing

**Status:** Proposed
**Decision:** Expose schema information through application-level DTOs
suitable for UI form generation, not raw internal schema-engine objects.

### Context

A YANG-driven GUI needs to browse nodes and constraints as well as
validate operation requests.

### Decision

Evolve `SchemaCatalogService` in increments to expose modules/revisions,
schema nodes, types, constraints, descriptions, defaults, choices, list
keys, and structured validation diagnostics. Keep UI rendering decisions
in the presentation layer.

### Consequences

-   A future UI can generate forms from schema metadata.
-   The application layer avoids coupling its public contract to
    libyang's internal object model.
-   Schema DTOs need versioning and compatibility tests.

### Acceptance criteria

-   [ ] Schema browsing does not expose raw libyang objects.
-   [ ] Node metadata and validation diagnostics are structured.
-   [ ] Missing schemas and unsupported features have defined outcomes.
-   [ ] Cache/database access is tested under concurrent requests.

------------------------------------------------------------------------

## ADR-009: Treat web exposure as a new security boundary

**Status:** Proposed
**Decision:** Do not expose mutation operations through a web API until
authentication, authorization, secret handling, and operational limits
are defined.

### Context

A browser/API client can access powerful device-management operations
and may be reachable by users other than the local CLI operator.

### Decision

Before exposing writes, define:

-   authentication and per-device/per-operation authorization;
-   secret redaction and credential resolution;
-   TLS and device identity verification;
-   audit events for configuration changes;
-   request-size, concurrency, and rate limits;
-   safeguards such as validation, preview/diff, explicit confirmation,
    and protocol-appropriate transactions.

### Consequences

-   Remote access requires deliberate security work.
-   Mutation APIs may be introduced after read-only capabilities.
-   Auditability and operational controls become part of the application
    contract.

### Acceptance criteria

-   [ ] Unauthorized users cannot read or mutate protected device
    profiles.
-   [ ] Secrets are excluded from API responses and logs.
-   [ ] Mutations have audit records and defined confirmation/safety
    behavior.
-   [ ] Device endpoint restrictions and connection verification are
    documented.

------------------------------------------------------------------------

## ADR-010: Introduce a web API incrementally

**Status:** Proposed
**Decision:** Build a small read-only API first, then add schema
browsing, subscriptions, and mutation workflows in stages.

### Suggested sequence

1.  **Stabilize contracts:** validation, result DTOs, error mapping,
    repetition semantics.
2.  **Read-only API:** device/profile listing, capabilities, read
    operations, job status.
3.  **GUI shell:** connect to the API without embedding protocol logic
    in the browser.
4.  **Schema integration:** module/node browsing and schema-driven
    request forms.
5.  **Subscriptions:** bounded event delivery and reconnect/lifecycle
    behavior.
6.  **Mutation operations:** authorization, validation,
    preview/confirmation, audit, and transaction handling.

### Consequences

-   The first GUI can validate the architecture without requiring every
    feature.
-   Security-sensitive writes are not rushed into the initial release.
-   API contracts can be tested before a frontend depends on them.

### Acceptance criteria

-   [ ] The initial API has a documented contract and automated tests.
-   [ ] The frontend does not duplicate protocol/execution logic.
-   [ ] Each phase has explicit acceptance criteria before the next
    phase begins.

------------------------------------------------------------------------

## Instructions for AI coding agents

When modifying this area:

1.  Read this document and `01_findings.md` before changing
    application-service behavior.
2.  Read `02_architectural_concerns.md` when a change affects module
    boundaries, persistence, schemas, subscriptions, or security.
3.  Preserve the dependency direction in ADR-001.
4.  Do not silently change DTO fields or result semantics without
    updating tests and relevant documentation.
5.  Trace request execution end-to-end before changing repetition or
    worker invocation.
6.  Do not add a new abstraction unless it has a clear responsibility
    and improves testability or change isolation.
7.  Never assume protocol parity. Add or update the protocol/operation
    support matrix when adding an operation.
8.  For each implemented decision, update its status and record the
    relevant commit/PR and tests.
9.  If a decision is intentionally rejected, record the rationale and
    any replacement decision rather than deleting the concern without
    explanation.
10. Do not claim tests passed unless they were actually run and their
    results are known.
