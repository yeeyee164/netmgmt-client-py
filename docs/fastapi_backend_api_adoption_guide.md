# FastAPI Backend API Adoption & Implementation Guide

> **Revision note**: This revision incorporates the architecture discussion about repository alignment, synchronous protocol handlers, and managed logical sessions. It also reconciles the examples with the Application Service Layer contracts currently present in `main`. Items described as **proposed** are new work, not existing repository capabilities.

## 1. Purpose & Architectural Overview

This guide defines the engineering approach for introducing a **FastAPI** presentation tier (`api/`) into `netmgmt-client-py`. The API is a peer presentation adapter alongside the existing CLI. Both adapters translate their external input into framework-independent Application Service Layer (ASL) DTOs and invoke application services.

The API integration must not require the protocol handlers to become asynchronous. The current NETCONF (`ncclient`) and gNMI (synchronous gRPC) execution paths can remain synchronous. FastAPI routes must call blocking application operations through a bounded worker-thread boundary so they do not block the ASGI event loop.

A separate **managed logical session** abstraction is proposed for interactions that span multiple requests. It is distinct from the device registry and from the protocol's own session identifier. It is particularly important for NETCONF workflows that must retain the same SSH/NETCONF session across calls.

```text
   [ CLI Terminal ]       [ FastAPI API ]       [ Future gRPC API ]
          |                     |                       |
          v                     v                       v
   [ cli/ adapter ]      [ api/ adapter ]        [ gRPC adapter ]
          |                     |                       |
          +---------------------+-----------------------+
                                |
                                v
             Application Service Layer (`application/`)
     +------------------+-------------------+--------------------+
     | DeviceRegistry   | DeviceOperation   | Subscription       |
     | Service          | Service           | Service            |
     +------------------+-------------------+--------------------+
                                |
                   +------------+------------+
                   |                         |
                   v                         v
       [ Proposed DeviceSessionManager ]  [ SchemaCatalogService ]
       [ logical session / lease owner ]  [ YANG catalog facade ]
                   |                         |
                   v                         v
       [ Managed protocol resources ]   [ core/schema_manager.py ]
          /                 \
         v                   v
    [ NETCONF ]           [ gNMI ]
    SSH session            gRPC channel / RPC / subscription stream
```

The diagram shows a **proposed** integration point for managed sessions. `DeviceSessionManager` does not currently exist and must not be treated as an already available class.

### 1.1 Architectural responsibilities

| Component | Responsibility |
|---|---|
| `cli/` | CLI parsing, normalization, presentation, and conversion to ASL DTOs |
| `api/` | HTTP/WebSocket schemas, validation, authentication, response mapping, and API routing |
| `application/` | Framework-independent orchestration and application-level contracts |
| `DeviceRegistryService` | Device inventory and endpoint-profile resolution |
| `DeviceOperationService` | Synchronous execution of unary operations and operation pipelines |
| `SubscriptionService` | Long-running telemetry subscription registry, worker threads, queues, and controls |
| `SchemaCatalogService` | Application facade over YANG schema metadata and validation |
| `DeviceSessionManager` (**proposed**) | Logical session ownership, leases, protocol-resource lifecycle, and expiration |
| `managers/` and `specs/` | Existing worker orchestration and protocol-specific implementations |
| `core/` | YANG schema engine and persistence implementation |

## 2. Non-Negotiable Architectural Invariants

1. **Presentation-layer isolation**
   - Modules inside `api/` may import from `api/` and the public `application/` interfaces.
   - Modules inside `api/` must not import directly from `managers/`, `specs/`, `core/`, `config/`, `modules/output.py`, or `cli/`.
   - The API must not instantiate protocol clients or workers directly.

2. **Reuse or extend ASL contracts; do not duplicate them**
   - Existing DTOs and services are the default contracts.
   - Introduce a new class only when it represents a responsibility that is genuinely absent.
   - A name used in an implementation draft is a conceptual target, not a requirement to create a duplicate class with that name.

3. **HTTP schemas are not ASL DTOs**
   - `api/schemas/` contains Pydantic models for HTTP input/output and OpenAPI generation.
   - `api/adapters.py` explicitly converts between Pydantic models and existing ASL dataclasses.
   - Do not pass `model_dump()` dictionaries to application services where a DTO instance is expected.

4. **Protocol selection must be explicit or profile-resolved**
   - Ad-hoc endpoints must declare `protocol: "gnmi" | "netconf"`.
   - A registered `device_id` may use the protocol already stored in its device profile.
   - Never infer the protocol from a TCP port number.

5. **Keep existing protocol handlers synchronous for the initial API release**
   - Do not rewrite NETCONF or gNMI handlers as `async def` merely to support FastAPI.
   - Run synchronous application calls in worker threads using one deliberate concurrency strategy.
   - A request timeout or cancellation of an await does not, by itself, terminate a blocking network call. Enforce connection/RPC timeouts in the protocol layer as well.

6. **Concurrency must respect stateful protocol resources**
   - Bound simultaneous blocking operations at the application-instance level.
   - Serialize operations that share one stateful NETCONF session unless the protocol implementation explicitly supports safe concurrency.
   - Do not use one module-global `asyncio.Semaphore` shared across application instances or event loops.

7. **Logical sessions must be separate from device inventory**
   - `DeviceRegistryService` manages device identities and endpoint profiles.
   - A proposed `DeviceSessionManager` manages client interaction/session lifecycle.
   - A logical session ID is an application-issued opaque reference. It is **not** the server-assigned NETCONF `<session-id>`.

8. **Subscription lifetime is independent of a WebSocket connection**
   - A WebSocket disconnect detaches that consumer; it must not automatically stop the underlying telemetry subscription.
   - Subscription creation, status, polling, and termination are explicit lifecycle operations.

9. **Use dependency injection and application lifespan**
   - Initialize application services in FastAPI lifespan and expose them through dependencies.
   - Tests must be able to override these dependencies without opening real SSH or gRPC connections.

10. **Security is part of the first API milestone**
    - Add authentication and authorization before exposing device operations.
    - Do not treat CORS as authentication.
    - Restrict arbitrary targets, credential use, and filesystem paths for certificates/private keys.
    - Redact credentials and sensitive protocol diagnostics from API responses and logs.

## 3. Step 0: Reconcile the Guide with Existing Repository Contracts

Before implementing routes, inspect the actual `application/` interfaces on the target branch and record the exact DTO fields, method signatures, and exception behavior. The following table reflects the current `main` contracts reviewed for this revision. Recheck them if the branch changes before implementation.

### 3.1 Existing contracts to reuse

| Responsibility | Existing contract | Implementation guidance |
|---|---|---|
| Ad-hoc endpoint | `AdhocEndpointDTO` | Reuse. Its current fields include `target`, `protocol`, `username`, `password`, `tls_ca`, `tls_cert`, `tls_key`, `skip_verify`, `insecure`, `tls_server_name`, `tls_version`, `ssh_key`, `gnmi_use_tunnel`, and `gnmi_target_name`. |
| One operation in a request | `OperationStepDTO` | Reuse. It uses `op_type`, `op_id`, `paths`, `filter_expression`, `filter_type`, `prefix`, `datastore`, `changes`, `xml_payload`, `telemetry`, and `options`. |
| Top-level execution request | `ExecutionRequestDTO` | Reuse. It includes `device_id`, `adhoc_endpoint`, optional `protocol`, `operations`, schema settings, `validate_only`, `timeout_seconds`, `times`, and `continue_on_error`. |
| Per-step result | `StepResultDTO` | Reuse. It carries `op_type`, `op_id`, `success`, `data`, `error`, and `error_code`. |
| Full execution result | `ExecutionResultDTO` | Reuse. It carries `target`, `protocol`, `success`, `results`, `error_summary`, `error_code`, and `execution_time_ms`. |
| Device inventory | `DeviceRegistryService` | Reuse for `register_device()`, `unregister_device()`, `get_device()`, `has_device()`, `list_devices()`, and `resolve_endpoint()`. It is not a transport-session pool. |
| Unary execution | `DeviceOperationService` | Reuse `execute_unary(request: ExecutionRequestDTO)` and `execute_batch(...)`. The current service does not expose the guide's `execute()` method. |
| Telemetry management | `SubscriptionService` | Reuse `start_subscription()`, `get_events()`, `consume_events_generator()`, `send_command()`, `stop_subscription()`, `list_active_subscriptions()`, `is_poll_subscription()`, `get_subscription_info()`, `has_active_threads()`, and `stop_all()`. |
| YANG schema facade | `SchemaCatalogService` | Reuse `validate_request_schema()`, `list_cached_modules()`, `get_database_statistics()`, and `get_profile_modules()`. It is not named `SchemaApplicationService`. |
| Application errors | `application.exceptions` | Reuse `ApplicationError`, `UnsupportedProtocolError`, `DeviceNotFoundError`, `InvalidRequestError`, and `SubscriptionNotFoundError`. `ApplicationError` currently exposes `error_code`; it does not define a `details` attribute. |

**Credential-state note:** the current `DeviceRegistryService` stores `AdhocEndpointDTO` endpoint profiles, and those DTOs can contain usernames, passwords, and certificate/key paths. A credential-vault or credential-reference abstraction is not currently part of this registry contract. For a production API, add server-side secret resolution or a credential-reference layer rather than making arbitrary secret values and filesystem paths part of the normal public request contract.

### 3.2 Names and assumptions that must not be copied from the earlier draft

| Earlier draft | Current repository contract / correction |
|---|---|
| `DeviceConnectionDTO` | `AdhocEndpointDTO` |
| `OperationRequestDTO` | `ExecutionRequestDTO` |
| `OperationResultDTO` | `ExecutionResultDTO` |
| `OperationResultDTO.data` | `ExecutionResultDTO.results`, a list of `StepResultDTO` |
| `SchemaApplicationService` | `SchemaCatalogService` |
| `operation` plus `pipeline` | `ExecutionRequestDTO.operations`, a list of `OperationStepDTO` |
| `service.execute()` | `service.execute_unary()` for one sequential request/pipeline |
| `ValidationError` | `InvalidRequestError` |
| `pop_event()` | `get_events()` or `consume_events_generator()` |
| `trigger_poll()` | `send_command(subscription_id, "poll")` |
| `stop()` | `stop_subscription()` or `send_command(subscription_id, "stop")` |
| `shutdown_all()` | `stop_all()` |
| `DeviceRegistryService.close_all()` | Not an existing device-registry responsibility or method |

Do not add these old names as compatibility wrappers solely to make the draft's examples work. If a genuinely useful new API is needed, add it deliberately and document why it belongs in the relevant layer.

### 3.3 Proposed new application concepts

The main genuinely new capability discussed for this design is managed logical-session lifecycle. It may justify adding:

- `DeviceSessionManager` in a new application module such as `application/sessions.py`.
- A small session-information DTO for safe metadata returned to callers.
- A protocol-resource abstraction or adapter that can retain and safely reuse an underlying NETCONF connection, and optionally reusable gNMI channel/context resources.

These are proposals, not existing interfaces. Define their exact method signatures only after inspecting how the current workers create, use, and close protocol clients.

## 4. Managed Logical Sessions: Design and Lifecycle

### 4.1 Why a logical session is useful

A request pipeline containing `lock → edit-config → validate → commit` can be submitted as one `ExecutionRequestDTO` with several `OperationStepDTO` entries. The current `DeviceOperationService.execute_unary()` is the existing entry point for a unary request containing a sequence of operations. Before relying on it for NETCONF transactions, add an integration test proving that the complete pipeline uses the same underlying NETCONF protocol session throughout.

A different use case occurs when the client sends separate requests over time:

```text
Request 1: lock(candidate)
Request 2: edit-config(candidate)
Request 3: validate(candidate)
Request 4: commit()
```

For this pattern, the application must preserve the same underlying NETCONF session between requests. Creating and closing a new NETCONF connection for each request would not preserve the session identity associated with the datastore lock.

A logical session gives API and future gRPC callers an opaque handle to that managed interaction. The caller does not need to know the raw transport object or the NETCONF server's session identifier.

### 4.2 Logical session versus protocol session

| Concept | Meaning |
|---|---|
| Logical/managed session | Application-owned record and opaque ID identifying a caller's managed interaction with a device |
| NETCONF protocol session | Actual NETCONF session over its transport; the server assigns its protocol session ID |
| gNMI connection context | Potentially reusable gRPC channel, credentials, and target context; ordinary gNMI unary RPCs do not inherently require a NETCONF-like transaction session |
| Telemetry subscription | A separate long-running stream managed by `SubscriptionService`; its subscription ID must not be confused with a logical device-session ID |

The logical session must not be described as a fake NETCONF session. It is a real application resource that owns or references real protocol resources. It should never expose the underlying client/transport object in an API response.

### 4.3 Proposed `DeviceSessionManager` responsibilities

`DeviceSessionManager` should be separate from `DeviceRegistryService` and should own:

- Creating and closing logical sessions.
- Binding a logical session to a registered device or an approved ad-hoc endpoint.
- Recording the authenticated owner/tenant and authorization scope.
- Retaining or resolving the underlying protocol resource needed by the session.
- Acquiring and releasing an operation lease so an idle sweeper cannot close a resource while it is in use.
- Serializing operations against the same NETCONF session.
- Tracking lifecycle state, last activity, expiration, and transaction/lock metadata where available.
- Performing controlled shutdown of managed resources.

Illustrative responsibilities (not a committed public API):

```python
class DeviceSessionManager:
    def create_session(self, *, device_id, owner_id, protocol=None): ...
    def get_session_info(self, session_id, *, owner_id): ...
    def acquire_session(self, session_id, *, owner_id): ...
    def release_session(self, lease): ...
    def close_session(self, session_id, *, owner_id): ...
    def close_all(self): ...
```

The implementation should prefer a context-managed lease so release occurs reliably even after exceptions. The manager must not return a raw protocol client to API route code.

### 4.4 Proposed session lifecycle

```text
             create
   [Creating] ---------> [Active / Idle]
                            |       ^
                   acquire |       | release / activity
                            v       |
                       [In Use] ---+
                            |
              explicit close / idle expiry
                            v
                        [Closing]
                            |
                     resource closed
                            v
                         [Closed]
```

An expired session must not be closed while a lease is active. A session record should at minimum track its opaque ID, owner, device/target reference, protocol, lifecycle state, created/last-used timestamps, and resource/transaction metadata needed for safe cleanup.

### 4.5 NETCONF transaction and cleanup rules

NETCONF requires special care because datastore locks are associated with the actual NETCONF protocol session.

- A managed NETCONF session must retain the same underlying NETCONF client/transport across separate operations when the client expects a lock or other session-scoped state to persist.
- Operations on one session should be serialized unless the underlying client and transaction model explicitly support concurrent use.
- Record which logical session and underlying NETCONF session acquired a datastore lock.
- Do not automatically run `<discard-changes>` merely because a logical session expires. The candidate datastore may contain edits from other workflows, and discarding it can revert changes beyond the expiring caller's work.
- Run `<unlock>` only when the manager can establish that the expiring protocol session owns the lock and the operation is safe. Closing the transport also terminates the protocol session; the server's session-termination behavior should be considered as part of cleanup.
- Make rollback an explicit transaction policy, not a generic idle-timeout side effect. Prefer an explicit transaction state machine and audit record for lock/edit/validate/commit/cancel/discard actions.

Transport cleanup and transaction cleanup are separate operations:

```text
Transport cleanup: release local resources and close the protocol connection.
Transaction cleanup: unlock or roll back only when ownership and safety are established.
```

### 4.6 gNMI behavior

Do not force NETCONF transaction semantics onto gNMI. A logical session may optionally retain a reusable gRPC channel or client context, but ordinary gNMI unary operations are not dependent on a persistent transaction session in the same way as NETCONF `<lock>` and `<commit>` workflows.

Long-running gNMI subscriptions should continue to use `SubscriptionService`. A logical device session ID and a subscription ID are separate resources with separate lifecycle policies.

### 4.7 API and gRPC consumers

The session manager belongs in the shared application layer, not in FastAPI routes. Both FastAPI and a future gRPC presentation adapter should refer to the same logical session abstraction.

A possible API surface is:

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/devices/{device_id}/sessions` | Create a managed session |
| `GET` | `/api/v1/sessions/{session_id}` | Inspect the caller-owned session |
| `DELETE` | `/api/v1/sessions/{session_id}` | Close the managed session |
| `POST` | `/api/v1/sessions/{session_id}/operations` | Execute operations using the session's retained protocol resource |

For the shared application contract, add a session-aware execution entry point only after the protocol worker lifecycle has been adapted to accept/reuse a managed protocol resource. For example, a future `DeviceOperationService.execute_in_session(...)` could acquire a lease, execute the request using that lease, and release it. This method does not exist today.

### 4.8 Process model

The initial implementation may keep logical-session state in memory, but it must run with a single API worker process. Multiple Uvicorn workers have separate memory, so a session created on worker A will not automatically exist on worker B. Before enabling multiple workers or horizontal replicas, use a shared session/resource owner or enforce routing affinity and define recovery semantics. A shared Redis record alone is not enough for a live SSH socket: the process owning the socket must also own and execute its operations.

## 5. Target Directory Layout (`api/`)

Create the `api/` package at the repository root alongside `application/` and `cli/`. The session route is optional until managed-session support is implemented.

```text
netmgmt-client-py/
├── api/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory and lifespan
│   ├── deps.py                 # Service dependencies and worker limiter
│   ├── exceptions.py           # Safe ASL/API error mapping
│   ├── adapters.py             # Pydantic <-> ASL DTO conversion
│   ├── schemas/
│   │   ├── common.py
│   │   ├── devices.py
│   │   ├── operations.py
│   │   ├── sessions.py         # Add with managed-session milestone
│   │   ├── subscriptions.py
│   │   └── schemas.py
│   └── routes/
│       ├── devices.py
│       ├── operations.py
│       ├── sessions.py         # Add with managed-session milestone
│       ├── subscriptions.py
│       └── schemas.py
├── application/
│   └── sessions.py             # Proposed DeviceSessionManager; not present today
└── tests/
    └── unit/
        └── test_api_routes.py
```

## 6. Step-by-Step Implementation Instructions

### Step 1: Add Dependencies

Add FastAPI and Uvicorn to the runtime environment, and add HTTPX for FastAPI/Starlette test-client support. Check the existing dependency set before adding duplicate or conflicting pins. Keep Pydantic on the repository's selected v2-compatible version range.

Example requirements (review against the project's existing pins before applying):

```text
fastapi>=0.115.0,<1.0.0
uvicorn[standard]>=0.30.0,<1.0.0
httpx>=0.27.0,<1.0.0
```

### Step 2: Implement Dependency Providers and Lifespan (`api/deps.py`, `api/main.py`)

Initialize the existing application services in the FastAPI lifespan. The following constructor wiring reflects the current service relationships:

```python
from application.devices import DeviceRegistryService
from application.operations import DeviceOperationService
from application.subscriptions import SubscriptionService
from application.schemas import SchemaCatalogService


def build_services():
    device_registry = DeviceRegistryService()
    schema_service = SchemaCatalogService()
    operation_service = DeviceOperationService(
        device_registry=device_registry,
        schema_service=schema_service,
    )
    subscription_service = SubscriptionService(
        device_registry=device_registry,
        operation_service=operation_service,
    )
    return {
        "device_registry": device_registry,
        "schema_service": schema_service,
        "operation_service": operation_service,
        "subscription_service": subscription_service,
    }
```

Do not add a `close_all()` call to `DeviceRegistryService`; it is an in-memory endpoint registry, not a transport-session pool. During shutdown, call `SubscriptionService.stop_all()`. When `DeviceSessionManager` is implemented, give it its own explicit `close_all()` lifecycle and call that method from lifespan.

For blocking calls, choose one strategy. The recommended option for `async def` routes is an application-scoped AnyIO `CapacityLimiter`:

```python
# Illustrative lifespan setup
import anyio

app.state.rpc_limiter = anyio.CapacityLimiter(32)
```

A blocking service call can then be run as follows:

```python
result = await anyio.to_thread.run_sync(
    service.execute_unary,
    request_dto,
    limiter=request.app.state.rpc_limiter,
)
```

Alternatively, use ordinary synchronous `def` route functions and let FastAPI/Starlette run them in its worker pool. Do not mix multiple unbounded thread-pool strategies. If an endpoint calls `execute_batch()`, account for its internal `ThreadPoolExecutor` so nested concurrency does not exceed the intended resource budget.

Use protocol-level connection/RPC timeouts. A timeout around `await` is not a reliable way to stop a worker thread blocked inside `ncclient` or synchronous gRPC.

Dependency providers should retrieve already-created services from `request.app.state`:

```python
from fastapi import Request


def get_operation_service(request: Request) -> DeviceOperationService:
    return request.app.state.operation_service
```

Repeat the pattern for device registry, schema catalog, subscription, and (when implemented) session manager dependencies.

### Step 3: Define Error Mapping (`api/exceptions.py`)

Use the exception hierarchy that exists today. Do not import `ValidationError`, `UnsupportedOperationError`, `SessionLockConflictError`, `ProtocolExecutionError`, or `DeviceTimeoutError` unless those classes are deliberately introduced with defined semantics.

Current application exceptions include:

- `ApplicationError`
- `UnsupportedProtocolError`
- `DeviceNotFoundError`
- `InvalidRequestError`
- `SubscriptionNotFoundError`

`ApplicationError` currently has an `error_code` attribute but no standard `details` attribute. Therefore, do not assume `exc.details` exists.

Also account for the behavior of `DeviceOperationService.execute_unary()`: it catches application and unexpected execution exceptions and normally represents them in an `ExecutionResultDTO` with `success=False`, `error_summary`, `error_code`, and failed `StepResultDTO` entries. Those failures may not reach a FastAPI exception handler.

Define a consistent API policy for:

- HTTP request/schema validation failures (normally 422 from FastAPI/Pydantic).
- Authentication and authorization failures (401/403).
- Missing device/session/subscription resources (404).
- Conflicting session or transaction state (409, once such errors are represented explicitly).
- Protocol execution failures and timeouts.
- Valid pipelines that complete with one or more failed steps.

Do not map every failed `ExecutionResultDTO` to the same HTTP status without considering the per-step results and error codes. For pipeline requests, preserve all step outcomes in the response so clients can see which operation failed and which operations were subsequently executed. Avoid returning stack traces, passwords, private-key paths, or raw transport diagnostics.

### Step 4: Define Pydantic v2 HTTP Schemas (`api/schemas/`)

Use strict request models and keep their names/fields close enough to the ASL contracts that the adapter remains explicit and understandable. The HTTP API may choose friendlier names, but every mapping must be documented.

Recommended constraints:

- Set `model_config = ConfigDict(extra="forbid")` on request models.
- Bound string lengths, list lengths, payload sizes, and timeout values.
- Validate target exclusivity: exactly one of `device_id` or `adhoc_endpoint` for an ordinary ad-hoc/device execution request.
- Use `op_type` in the request model, or explicitly map any public `operation` alias to `OperationStepDTO.op_type`.
- Validate operations against the protocol-operation matrix in `application/dto.py` rather than allowing every operation for every protocol.
- Align datastore names with the DTO's supported set: `running`, `candidate`, `startup`, `all`, `operational`, and `intended`, subject to protocol support.
- Represent mutations as typed change items and XML configuration as `xml_payload`; do not silently reinterpret arbitrary JSON as XML.
- Keep protocol-specific options in a validated `options` structure rather than inventing generic fields that are not supported by the DTO.

Illustrative request shape:

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdhocEndpointSchema(StrictRequestModel):
    target: str = Field(min_length=1, max_length=255, examples=["10.0.0.1:830"])
    protocol: Literal["gnmi", "netconf"]
    username: str | None = None
    password: str | None = None
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    skip_verify: bool = False
    insecure: bool = False
    tls_server_name: str | None = None
    tls_version: str | None = None
    ssh_key: str | None = None
    gnmi_use_tunnel: bool = False
    gnmi_target_name: str | None = None


class MutationChangeSchema(StrictRequestModel):
    path: str = Field(min_length=1, max_length=2048)
    value: Any | None = None
    operation: Literal["merge", "replace", "delete", "remove", "create"] = "merge"
    value_type: str | None = None


class TelemetryPolicySchema(StrictRequestModel):
    mode: Literal[
        "periodic", "event_driven", "snapshot", "on_demand", "server_determined"
    ] = "periodic"
    sample_interval_sec: float = Field(default=10.0, ge=0)
    heartbeat_interval_sec: float | None = Field(default=None, ge=0)
    suppress_redundant: bool = False


class OperationStepSchema(StrictRequestModel):
    op_type: str
    op_id: str | None = None
    paths: list[str] = Field(default_factory=list, max_length=256)
    filter_expression: str | None = None
    filter_type: Literal["xpath", "subtree"] | None = None
    prefix: str | None = None
    datastore: Literal[
        "running", "candidate", "startup", "all", "operational", "intended"
    ] = "all"
    changes: list[MutationChangeSchema] = Field(default_factory=list, max_length=256)
    xml_payload: str | None = None
    telemetry: TelemetryPolicySchema | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ExecutionRequestSchema(StrictRequestModel):
    device_id: str | None = None
    adhoc_endpoint: AdhocEndpointSchema | None = None
    protocol: Literal["gnmi", "netconf"] | None = None
    operations: list[OperationStepSchema] = Field(min_length=1, max_length=100)
    schema_profile_id: str | None = None
    schema_dirs: list[str] = Field(default_factory=list)
    schema_cache_dir: str | None = None
    schema_validate: bool = False
    schema_auto_discovery: bool = True
    schema_auto_fetch: bool = True
    validate_only: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=3600)
    times: int = Field(default=1, ge=1, le=1000)
    continue_on_error: bool = False

    @model_validator(mode="after")
    def validate_target(self):
        if bool(self.device_id) == bool(self.adhoc_endpoint):
            raise ValueError("Provide exactly one of device_id or adhoc_endpoint")
        return self


class StepResultSchema(StrictRequestModel):
    op_type: str
    op_id: str | None = None
    success: bool
    data: Any | None = None
    error: str | None = None
    error_code: str | None = None


class ExecutionResultSchema(StrictRequestModel):
    target: str
    protocol: str
    success: bool
    results: list[StepResultSchema] = Field(default_factory=list)
    error_summary: str | None = None
    error_code: str | None = None
    execution_time_ms: float = 0.0
```

This defines typed request and response shapes. The API should still validate each `op_type` against the protocol-operation matrix from `application/dto.py`, including registered-device protocol resolution. Also, do not expose arbitrary client-supplied filesystem paths for certificates, SSH keys, or YANG directories to untrusted callers. Prefer server-side credential/profile references and an allow-listed schema directory policy.

### Step 5: Build Explicit Schema-to-DTO Adapters (`api/adapters.py`)

The adapter should instantiate actual ASL dataclasses. It must not pass `model_dump()` dictionaries as substitutes for `OperationStepDTO` or `MutationChangeDTO` instances.

Illustrative conversion:

```python
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    OperationStepDTO,
    MutationChangeDTO,
)


def to_execution_request_dto(req: ExecutionRequestSchema) -> ExecutionRequestDTO:
    endpoint = None
    if req.adhoc_endpoint is not None:
        ep = req.adhoc_endpoint
        endpoint = AdhocEndpointDTO(
            target=ep.target,
            protocol=ep.protocol,
            username=ep.username,
            password=ep.password,
            tls_ca=ep.tls_ca,
            tls_cert=ep.tls_cert,
            tls_key=ep.tls_key,
            skip_verify=ep.skip_verify,
            insecure=ep.insecure,
            tls_server_name=ep.tls_server_name,
            tls_version=ep.tls_version,
            ssh_key=ep.ssh_key,
            gnmi_use_tunnel=ep.gnmi_use_tunnel,
            gnmi_target_name=ep.gnmi_target_name,
        )

    steps = []
    for step in req.operations:
        changes = [
            MutationChangeDTO(
                path=change.path,
                value=change.value,
                operation=change.operation,
                value_type=change.value_type,
            )
            for change in step.changes
        ]
        steps.append(
            OperationStepDTO(
                op_type=step.op_type,
                op_id=step.op_id,
                paths=step.paths,
                filter_expression=step.filter_expression,
                filter_type=step.filter_type,
                prefix=step.prefix,
                datastore=step.datastore,
                changes=changes,
                xml_payload=step.xml_payload,
                telemetry=to_telemetry_policy_dto(step.telemetry),
                options=step.options,
            )
        )

    return ExecutionRequestDTO(
        device_id=req.device_id,
        adhoc_endpoint=endpoint,
        protocol=req.protocol,
        operations=steps,
        schema_profile_id=req.schema_profile_id,
        schema_dirs=req.schema_dirs,
        schema_cache_dir=req.schema_cache_dir,
        schema_validate=req.schema_validate,
        schema_auto_discovery=req.schema_auto_discovery,
        schema_auto_fetch=req.schema_auto_fetch,
        validate_only=req.validate_only,
        timeout_seconds=req.timeout_seconds,
        times=req.times,
        continue_on_error=req.continue_on_error,
    )
```

The telemetry conversion can be implemented explicitly with `TelemetryPolicyDTO` and `DeliveryModeDTO.from_string(step.telemetry.mode)`, copying the timing and suppression fields. Keep the conversion in one place so the CLI and API continue to share semantic ASL behavior without sharing presentation models.

The response adapter should map `ExecutionResultDTO` and every `StepResultDTO` to `ExecutionResultSchema`/`StepResultSchema`. Preserve `op_id`, individual success/failure, data, error, and error code. Do not flatten a pipeline into one generic `data` field or discard partial results.

### Step 6: Implement Unary Operation Routes (`api/routes/operations.py`)

Use the existing `DeviceOperationService.execute_unary()` method. The following example assumes the dependency returns the application-scoped limiter initialized during lifespan.

```python
import anyio
from fastapi import APIRouter, Depends, Request
from application.operations import DeviceOperationService

router = APIRouter(prefix="/api/v1/operations", tags=["Operations"])


@router.post("/execute", response_model=ExecutionResultSchema)
async def execute_operation(
    payload: ExecutionRequestSchema,
    request: Request,
    service: DeviceOperationService = Depends(get_operation_service),
):
    request_dto = to_execution_request_dto(payload)
    result_dto = await anyio.to_thread.run_sync(
        service.execute_unary,
        request_dto,
        limiter=request.app.state.rpc_limiter,
    )
    return to_execution_result_schema(result_dto)
```

A request's `operations` list is the sequential pipeline. Use `continue_on_error` to control whether subsequent steps continue after a failed step, according to the ASL implementation. The API must preserve the resulting per-step outcomes.

Do not add a `session_id` field to `ExecutionRequestDTO` as a cosmetic API feature. Add managed-session execution only when `DeviceSessionManager` and the protocol-resource reuse path have been implemented. At that point, expose a session-aware application method or explicit execution context shared by all presentation adapters.

### Step 7: Implement Device and Managed-Session Routes

The device routes should use `DeviceRegistryService` for inventory/profile operations. Do not make the registry responsible for creating or pooling network connections.

The session routes are a separate milestone and should use the proposed `DeviceSessionManager`. Before writing them, verify that the underlying protocol clients can be retained and reused across calls. A session manager that only stores a UUID while every worker creates a fresh NETCONF connection does not solve the NETCONF lock-lifecycle problem.

Required tests include:

- A session ID is bound to its authenticated owner and cannot be used by another principal.
- Two operations using one NETCONF logical session use the same underlying NETCONF protocol session.
- Operations against one managed NETCONF session are serialized.
- An idle sweeper does not close an in-use lease.
- Closing a session releases its transport resources.
- A failed or disconnected transaction does not trigger an unsafe, unconditional `<discard-changes>`.
- A gNMI logical session does not incorrectly inherit NETCONF transaction semantics.

### Step 8: Implement Telemetry Subscription Routes

Use the current `SubscriptionService` interface:

- `start_subscription(request, ...)`
- `get_events(subscription_id, timeout=..., max_events=...)`
- `send_command(subscription_id, "poll")`
- `send_command(subscription_id, "stop")`
- `stop_subscription(subscription_id)`
- `get_subscription_info(subscription_id)`
- `list_active_subscriptions()`
- `stop_all()`

A subscription-create route must build an `ExecutionRequestDTO` containing a subscribe `OperationStepDTO` and call `start_subscription()`. The subscription lifecycle must exist independently of the WebSocket connection.

Suggested endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/subscriptions` | Create/start a subscription |
| `GET` | `/api/v1/subscriptions/{subscription_id}` | Inspect subscription status |
| `POST` | `/api/v1/subscriptions/{subscription_id}/poll` | Trigger an on-demand update |
| `DELETE` | `/api/v1/subscriptions/{subscription_id}` | Stop the subscription |
| `WS` | `/api/v1/subscriptions/{subscription_id}/ws` | Attach a WebSocket consumer to telemetry events |

The WebSocket handler can call `get_events()` through the worker-thread boundary and send each returned `StreamEventDTO` as JSON. On `WebSocketDisconnect`, detach the consumer only. Do **not** call `stop_subscription()` in the WebSocket `finally` block. The subscription should stop only through an explicit stop request, an application lifecycle policy, or a terminal worker condition.

The current `SubscriptionService` does not by itself provide a complete HTTP user-ownership/authorization model. Add ownership checks at the application/API boundary or extend the service with an explicit owner context before exposing subscriptions to multiple users. Protect against slow WebSocket consumers and enforce queue limits/overflow policy.

### Step 9: Implement Schema Routes

Use `SchemaCatalogService`, not the draft's `SchemaApplicationService` name. Existing methods include:

- `validate_request_schema(request: ExecutionRequestDTO)`
- `list_cached_modules()`
- `get_database_statistics()`
- `get_profile_modules(profile_id)`

Possible routes include:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/schemas/modules` | List cached YANG modules |
| `GET` | `/api/v1/schemas/statistics` | Read schema-cache statistics |
| `GET` | `/api/v1/schemas/profiles/{profile_id}/modules` | List modules for a profile |
| `POST` | `/api/v1/schemas/validate` | Run pre-flight validation |

Do not assume a thread-safe `RLock` already exists around the schema manager. Verify the concurrency guarantees of the shared `libyang` context and schema database first; add synchronization at the correct ownership boundary if required. Avoid adding a lock around the entire schema service without understanding whether the underlying operations already synchronize or whether separate contexts are safe.

### Step 10: Wire Routers and Lifespan (`api/main.py`)

The application factory should:

- Construct existing application services once per FastAPI application instance.
- Store them on `app.state` and expose typed dependencies.
- Construct a bounded worker limiter during lifespan.
- Register API exception handlers and middleware.
- Include device, operation, subscription, and schema routers as they are implemented. Include session routes only after managed-session support exists.
- On shutdown, stop subscriptions with `stop_all()` and close managed sessions using the new session manager's own shutdown method if it has been introduced.

CORS origins, methods, and headers must be configuration-driven. The example development origins may be allowed locally, but production must use an explicit origin allow-list. CORS is not an authentication mechanism.

## 7. Verification & Unit Testing Strategy

Use `fastapi.testclient.TestClient` and `app.dependency_overrides` to test route behavior without opening real SSH or gRPC sockets. Use fakes for session and subscription managers where lifecycle behavior is under test.

### 7.1 API and DTO contract tests

- Exactly one target is supplied for ordinary execution requests.
- Pydantic rejects unknown fields, invalid operation names, invalid datastores, excessive list sizes, and invalid timeouts.
- Every API operation step maps to an `OperationStepDTO`; mutations map to `MutationChangeDTO`; telemetry maps to `TelemetryPolicyDTO`.
- Registered device and ad-hoc endpoint requests map to the correct DTO fields.
- `ExecutionResultDTO` response mapping preserves every `StepResultDTO`, including `op_id` and individual errors.
- Existing CLI and ASL tests remain unaffected.

### 7.2 Blocking I/O and concurrency tests

- Concurrent API requests do not block the event loop.
- The configured limiter bounds simultaneous blocking execution calls.
- Protocol connection and RPC timeouts are enforced by the protocol implementation.
- Client cancellation/disconnect does not falsely imply that a blocking worker thread has stopped.
- Nested `execute_batch()` concurrency is accounted for.

### 7.3 NETCONF managed-session tests

- A single-request transaction pipeline uses one NETCONF protocol session from lock through commit/unlock.
- Separate API/gRPC calls referencing one logical session reuse the same underlying NETCONF session.
- Two independent logical sessions cannot incorrectly share one session-scoped lock owner.
- Concurrent operations on one managed session are serialized.
- A session lease prevents the idle sweeper from closing an in-use connection.
- Expiration and shutdown close resources deterministically.
- Candidate rollback is never triggered solely by generic idle expiration.
- Explicit `discard-changes` behavior is tested separately from transport cleanup.

### 7.4 gNMI and subscription tests

- gNMI unary operations work without requiring NETCONF-style transaction state.
- Subscription creation returns a subscription ID and status can be inspected.
- Poll mode uses `send_command(subscription_id, "poll")`.
- Stop uses `stop_subscription()` or the documented stop command.
- A WebSocket disconnect followed by reconnect does not terminate the underlying subscription.
- Unauthorized callers cannot inspect, poll, attach to, or stop another caller's subscription.
- Slow consumers, queue overflow, worker failure, and application shutdown are covered.

### 7.5 Error and security tests

- Current ASL exceptions map to the documented API error behavior.
- Failed `ExecutionResultDTO` responses preserve per-step diagnostics.
- Unexpected exceptions do not expose stack traces or secrets to clients.
- Passwords, private-key paths, certificate contents, and sensitive configuration values are redacted from logs and responses.
- Ad-hoc targets and credential references obey configured allow-lists and authorization policy.

## 8. Deployment and Acceptance Checklist

### 8.1 Deployment assumptions

- Device profiles, subscriptions, and any in-memory managed sessions are process-local unless explicitly externalized.
- Start with one Uvicorn worker while the session and subscription registries are in memory.
- Do not enable multiple workers or replicas until session ownership, live protocol-resource ownership, subscription routing, and event delivery are designed across processes.
- Use `--reload` only for local development.
- Add separate liveness and readiness endpoints. Readiness should reflect whether essential dependencies and application services are initialized, not just whether the process responds.
- Bind to `127.0.0.1` for local development unless external access is intended and protected by network controls, authentication, and TLS termination.

### 8.2 Layer-boundary check

```bash
grep -rnE "from (managers|specs|core|config|cli) import|import (grpc|ncclient)" api/
```

The API package should not directly depend on these lower-level modules or transport libraries. Imports from `application/` are expected.

### 8.3 Test commands

Run the existing suites and the new API tests. Adjust paths to match the actual test tree on the implementation branch:

```bash
pytest tests/unit/
pytest tests/composite/test_application_integration.py tests/composite/test_cli_schema.py
pytest tests/unit/test_api_routes.py
```

### 8.4 Local development server

```bash
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

## 9. Recommended Implementation Roadmap

1. **Step 0: Contract reconciliation.** Confirm the current DTO fields, service methods, exception behavior, worker lifecycle, and existing CLI adapter mapping on the implementation branch.
2. **Step 1: FastAPI foundation.** Add app factory, lifespan, dependency injection, exception handling, health/readiness routes, and a bounded worker-thread boundary.
3. **Step 2: Read-only unary operations.** Implement capabilities and get operations using `ExecutionRequestDTO`, `OperationStepDTO`, and `DeviceOperationService.execute_unary()`.
4. **Step 3: Sequential configuration pipelines.** Add set/edit-config and NETCONF transaction operations. Verify same-session behavior within a single request and preserve partial step results.
5. **Step 4: Device management and security.** Expose device profiles with authentication, authorization, credential references, and target restrictions.
6. **Step 5: Managed logical sessions.** Introduce `DeviceSessionManager` only after the protocol drivers can retain/reuse a real NETCONF session across separate calls. Add ownership, leases, per-session serialization, expiration, and safe cleanup. Keep gNMI semantics protocol-appropriate.
7. **Step 6: Telemetry subscriptions.** Implement create/status/poll/stop endpoints and WebSocket attachment. Stabilize REST lifecycle and reconnect behavior before relying on WebSockets.
8. **Step 7: YANG schema API.** Expose `SchemaCatalogService` functionality and verify shared libyang/database concurrency behavior.
9. **Step 8: Integration and deployment.** Test CLI compatibility, API/gRPC shared application services, lifecycle, concurrency, security, and the documented single-worker process model.

## 10. Final Design Decisions

- **Do not duplicate existing ASL DTOs or services to match the old draft's names.** Reuse existing classes and add only genuinely new responsibilities.
- **Do not convert existing protocol handlers to async for FastAPI.** Keep the current synchronous protocol implementations and isolate them behind a bounded worker-thread boundary.
- **Do not put transport-session pooling in `DeviceRegistryService`.** Keep device inventory and managed session lifecycle separate.
- **Introduce a real managed logical-session abstraction for cross-request NETCONF transactions.** It must own/reuse an actual protocol session; a UUID-only placeholder is insufficient.
- **Do not equate a logical session ID with the NETCONF server's session ID or a telemetry subscription ID.** These are different resources.
- **Do not discard candidate configuration automatically on generic session expiration.** Transaction rollback must be ownership-aware and explicit.
- **Do not stop a telemetry subscription when a WebSocket consumer disconnects.** Detach the consumer and let the subscription lifecycle be managed independently.
- **Keep the API and future gRPC interface as adapters over the same application services.**
