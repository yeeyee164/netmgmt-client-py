# Context & Architecture Guide: netmgmt-client-py

## 1. Project Background & Mission Statement

**`netmgmt-client-py`** is a protocol-agnostic Northbound (NB) network automation client engine written in Python 3. It interfaces with multi-vendor network operating systems (Cisco IOS-XE/XR, Juniper Junos, Arista EOS, Nokia SR Linux) using standardized YANG-based management protocols:

1. **gNMI** (gRPC Network Management Interface via `grpcio` and Protobuf stubs in `specs/gnmi/`).
2. **NETCONF** (RFC 6241, RFC 5277, RFC 6022 via `ncclient` over SSH/TLS).
3. *(Planned)* **RESTCONF** (RFC 8040 over HTTPS).

The engine supports three invocation surfaces:
* **Nested CLI Subparsers:** `python client_main.py <protocol> <operation> [options]`
* **Hierarchical YAML Pipelines:** `python client_main.py [gnmi|netconf] -c config.yaml` (supporting multi-target, multi-subscription, and multi-operation orchestration).
* **FastAPI Web API:** `api/main.py` (`create_app()`), exposing REST and WebSocket endpoints over the Application Service Layer.

---

## 2. Module Directory & Navigation Guide

This section provides a structured inventory of the codebase to help contributors and agents quickly locate and navigate modules without needing to read entire subtrees:

| Path | Primary Exports / Components | Primary Role & Description |
| :--- | :--- | :--- |
| [`client_main.py`](client_main.py) | Entrypoint script | Universal CLI launcher; parses arguments, sets up logging, builds `ParsedConfig`, and dispatches via `CLIApplicationRunner` to the Application Service Layer. |
| [`api/`](api/) | `create_app`, `APISettings`, `Principal`, routers (`routes/`), Pydantic schemas (`schemas/`), `adapters.py` | FastAPI presentation adapter. Exposes `/healthz`, `/readyz`, and `/api/v1/{devices,operations,sessions,subscriptions,schemas}` with API-key/Bearer auth, target/path security policy checks, and WebSocket subscription streaming. |
| [`application/`](application/) | `DeviceOperationService`, `OperationRequestMapper`, `ExecutionResultMapper`, `SubscriptionService`, `FanoutQueue`, `DeviceRegistryService`, `SchemaCatalogService`, `DeviceSessionManager`, DTOs | Application Service Layer. Protocol-agnostic, presentation-decoupled service layer with explicit protocol declaration (`SupportedProtocol`). Maps caller intent (`ExecutionRequestDTO`) to domain configs (`operation_mapper.py`), coordinates unary/batch/in-session operations, returns structured `ExecutionResultDTO`s, and manages telemetry lifecycles (`subscription_queue.py`). |
| [`cli/`](cli/) | `build_args`, `config_builder`, `CLIApplicationRunner`, `CLIStreamRunner`, `ParsedConfig`, `CLIConfigBuilder`, `FileConfigBuilder`, `parse_operation_item` (`operation_parser.py`) | CLI & YAML ingestion and presentation layer. Defines `argparse` nested subparsers, hierarchical YAML parser, `SessionConfig` ↔ `ExecutionRequestDTO` adapters, and CLI execution/streaming runners. |
| [`config/`](config/) | `SessionConfig`, `OperationConfig`, `DeliveryPolicy`, `SchemaConfig`, `OutputConfig` | Domain configuration layer. Decouples session parameters into `ConnectionConfig`, `ExecutionConfig`, `OperationConfig`, and `SchemaConfig`. Defines protocol-neutral `DeliveryMode` (RFC 8641). |
| [`config/protocol_options/`](config/protocol_options/) | `GnmiOptions`, `NetconfOptions` | Protocol-specific configuration structures (gNMI encodings/sample intervals; NETCONF datastores, locks, confirmed commits, RFC 5277 parameters). |
| [`core/`](core/) | `SchemaManager`, `SchemaContext`, `SchemaDB`, `SchemaValidator`, `SchemaSerializer` | Protocol-agnostic YANG schema compilation, SQLite metadata caching (`~/.cache/gnmi_client_py/schema_cache.db`), topological dependency resolution, path validation, and `libyang` XML/JSON serialization. Detailed guide in [`core/README.md`](core/README.md). |
| [`managers/`](managers/) | `ClientFactory`, `ValidatorFactory`, `SubscribeSession`, Unary workers (`GetWorker`, `SetWorker`, `CapabilityWorker`, `TransactionWorker`, `SequentialWorker`) | Worker execution and factory layer. Instantiates protocol clients/validators and runs unary workers and `SubscribeSession` threads orchestrated by `application/` services. |
| [`specs/`](specs/) | `BaseClient`, `GNMIClient`, `NetconfClient`, `BaseValidator`, `GNMIValidator`, `NetconfValidator`, `PathValidationError`, Stream types | Concrete protocol implementations and offline path/filter validators. Implements gRPC calls for gNMI, `ncclient` SSH/TLS sessions for NETCONF, semantic operations (`execute_*`), and protocol validators. |
| [`specs/gnmi/`](specs/gnmi/) | `gnmi_pb2.py`, `gnmi_pb2_grpc.py`, `gnmi_ext_pb2.py` | Compiled Google gNMI Protocol Buffer stubs. |
| [`modules/`](modules/) | `OutputHandler`, `ProtocolFormatter`, `normalize_protocol_payload`, `SecurityModule`, `setup_logger`, `parse_path` | Cross-cutting utilities: output routing (`stdout`, `stderr`, file append), formatters & payload normalization (JSON, XML, text), TLS/SSH credential builders, RFC 5424 syslog/file logging (`logger.py`), and XPath string parsing. |
| [`util/`](util/) | `utils.py` | Common helper functions (file reading, byte conversions, string formatting). |
| [`protos/`](protos/) | `gnmi.proto`, `gnmi_ext.proto`, `tunnel.proto` | Source Protocol Buffer definitions for gNMI and gRPC tunneling. |
| [`tests/`](tests/) | Unit & Composite test suites, YAML/XML fixtures, `testing_env.json` | Test suite covering DTOs, API routes/concurrency, models, validators, schema compilation, transaction lifecycles, and streaming subscriptions. |
| [`docs/`](docs/) | Architecture, ADRs, and user guides | Detailed specifications, architectural decisions ([`docs/decisions/architectural_decisions.md`](docs/decisions/architectural_decisions.md)), ASL concerns ([`docs/ASL/architectural_concerns.md`](docs/ASL/architectural_concerns.md)), and protocol user guides ([`docs/gnmi_usage.md`](docs/gnmi_usage.md), [`docs/netconf_usage.md`](docs/netconf_usage.md)). |

---

## 3. Core Architecture & Data Flow

```text
[ CLI Input / YAML Config ]                 [ FastAPI Web / WebSocket API ]
            │                                              │
            ▼                                              ▼
 [ cli/ (cmd.py, runner.py) ]                 [ api/ (routes/, adapters.py) ]
            └──────────────────┬───────────────────────────┘
                               │ builds ExecutionRequestDTO
                               ▼
[ Application Service Layer (application/) ]
     ├── DeviceRegistryService    ───────────────> Resolves device endpoints & credentials
     ├── SchemaCatalogService     ───────────────> Pre-flight schema validation & profile caching
     ├── DeviceSessionManager     ───────────────> Logical session leases & lock/commit tracking
     ├── DeviceOperationService   ───────────────> Orchestrates unary/batch requests; returns ExecutionResultDTO
     └── SubscriptionService      ───────────────> Coordinates streaming sessions & bounded FanoutQueues
                               │                                      │
                               │ (assembles domain SessionConfig)     │ (spawns SubscribeSession worker)
                               ▼                                      ▼
[ Worker Engines (managers/) ]
     ├── Unary Workers [ GetWorker / SetWorker / CapabilityWorker / TransactionWorker / SequentialWorker ]
     └── Stream Worker [ SubscribeSession ]
                               │
                               ▼
    [ ClientFactory (managers/factory.py -> specs/) ]
     ├── GNMIClient (BaseClient)    ─────────────> HTTP/2 (gRPC via grpcio)
     └── NetconfClient (BaseClient) ─────────────> SSH / TLS (ncclient)
                               │
                               ▼
 [ Result Delivery & Presentation Sinks ]
     ├── Direct ExecutionResultDTO ──────────────> Structured return objects to CLI/API adapters
     └── OutputHandler (cli/runner.py) ──────────> Formatter (GNMIFormatter / NETCONFFormatter)
                                                   └── [ stdout / stderr / file ]
```

### Layering & Refactoring Boundaries
* **Dependency direction:** Presentation adapters (`api/`, `cli/`) → `application/` → engine (`config/`, `core/`, `managers/`, `specs/`).
* **Presentation isolation:** `api/` must import only from `application/` (never `config/`, `core/`, `managers/`, or `specs/` directly). Core engine packages (`config/`, `core/`, `managers/`, `specs/`) must never import `fastapi`, `api/`, or `cli/`.
* **Structured results:** Application services return DTOs (`ExecutionResultDTO`, `StreamEventDTO`) and must not depend on CLI `OutputHandler` sinks.
* **Preservation rules:** Do not introduce new upward imports from `util/`/`modules/`/`managers/` into `specs/`/`cli/`/`application/`. Keep `tests/unit/` isolated and fast without live network calls.

### Key Reference Documents
- **Application Service Layer Spec:** [`docs/application_service_layer_specification.md`](docs/application_service_layer_specification.md)
- **Architectural Decisions (ADRs):** [`docs/decisions/architectural_decisions.md`](docs/decisions/architectural_decisions.md)
- **ASL Architectural Concerns:** [`docs/ASL/architectural_concerns.md`](docs/ASL/architectural_concerns.md)
- **Core Architecture & Data Flow:** [`docs/core_architecture_and_data_flow.md`](docs/core_architecture_and_data_flow.md)
- **YANG Schema Engine Architecture:** [`core/README.md`](core/README.md) and [`docs/schema/yang_schema_architecture.md`](docs/schema/yang_schema_architecture.md)
- **NETCONF Client Milestones:** [`docs/milestone/milestone_netconf.md`](docs/milestone/milestone_netconf.md)
- **Protocol Neutral Delivery Spec:** [`docs/RPC_structure/protocol_neutral_delivery_mode_spec.md`](docs/RPC_structure/protocol_neutral_delivery_mode_spec.md)
- **User Guides:**
  - gNMI User Guide: [`docs/gnmi_usage.md`](docs/gnmi_usage.md)
  - NETCONF User Guide: [`docs/netconf_usage.md`](docs/netconf_usage.md)

---

## 4. Environment & Verification

### Prerequisites
* Python 3.10+ (Python 3.10.12 tested)
* System C-libraries for SSH and XML manipulation (`libxml2-dev`, `libxslt1-dev`, `libffi-dev`)
* System C-libraries for `libyang` (`libyang==5.0.0` in `requirements.txt`, `libpcre2` >= 10.21)

### Virtual Environment & Dependencies
```bash
# Activate virtual environment (configured in local test env as /home/vbx/pyvenv3.10)
source /home/vbx/pyvenv3.10/bin/activate

# Install / update all dependencies (core + FastAPI backend)
pip install -r requirements.txt

# Run unit tests (fast offline feedback)
/home/vbx/pyvenv3.10/bin/python -m pytest tests/unit -q

# Run offline unit + composite tests (excluding live device tests)
/home/vbx/pyvenv3.10/bin/python -m pytest -m "not live" tests/
```

### Testing Guidelines
* Target connection details and vendor YANG paths for live/integration tests are configured in [`tests/testing_env.json`](tests/testing_env.json).
* Offline tests (`tests/unit` and `-m "not live"`) must pass without regressions before committing code changes.
* Mark live device tests with `@pytest.mark.live` so offline test runs stay deterministic.
* When adding new RPC capabilities, create corresponding unit tests and composite workflow YAML fixtures in `tests/`.