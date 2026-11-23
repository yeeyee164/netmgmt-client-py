# Test Architecture & Taxonomy Guide (netmgnt-client-py)

## 1. Overview

The test suite of **`netmgnt-client-py`** is bifurcated into two distinct categories: **Unit Tests** and **Composite Tests**, supported by centralized fixtures and test utilities.

```text
tests/
├── conftest.py               # Root test configuration (sys.path injection, path helpers)
├── testing_env.json          # Lab device endpoints, credentials, and vendor YANG paths
├── yang_fetcher.py           # Test utility for dynamic RFC 6022 model gathering
├── fixtures/                 # Centralized test payloads and configurations (.yaml, .xml)
├── unit/                     # Isolated, fast-executing unit test suite (22 files)
│   ├── test_api_concurrency.py
│   ├── test_api_routes.py
│   ├── test_application_devices.py
│   ├── test_application_dto.py
│   ├── test_application_operations.py
│   ├── test_application_sessions.py
│   ├── test_application_subscriptions.py
│   ├── test_cli_builder.py
│   ├── test_config_models.py
│   ├── test_delivery_mode.py
│   ├── test_path.py
│   ├── test_schema_cache.py
│   ├── test_schema_catalog.py
│   ├── test_schema_context.py
│   ├── test_schema_db_setup.py
│   ├── test_schema_feedback_fixes.py
│   ├── test_schema_profile.py
│   ├── test_schema_resolver.py
│   ├── test_schema_serializer.py
│   ├── test_schema_validator.py
│   ├── test_stream_types.py
│   └── test_validators.py
└── composite/                # Multi-component integration, pipeline, and live test suite (14 files)
    ├── test_api_testbed.py
    ├── test_application_integration.py
    ├── test_cli_schema.py
    ├── test_core_yang_ocnos.py
    ├── test_core_yang_srlinux.py
    ├── test_gnmi_schema_integration.py
    ├── test_gnmi_set.py
    ├── test_gnmi_subscribe.py
    ├── test_managers.py
    ├── test_netconf_edit_config.py
    ├── test_netconf_schema_integration.py
    ├── test_netconf_subscribe.py
    ├── test_netconf_transactions.py
    └── test_schema_manager.py
```

---

## 2. Test Taxonomy

### 2.1 Unit Tests (`tests/unit/`)
* **Characteristics**: Fast (<4 seconds total execution), deterministic, completely isolated, with mocked external I/O.
* **Scope**: Tests individual classes, functions, dataclass validation, string parsing, enum normalization, and single-component logic.
* **When to Run**: Continuously during development and refactoring loops.

| Test File | Target Subsystem | Scope & Verification |
| :--- | :--- | :--- |
| [`test_api_concurrency.py`](unit/test_api_concurrency.py) | `api/` | CapacityLimiter enforcement, WebSocket streaming concurrency, and API isolation. |
| [`test_api_routes.py`](unit/test_api_routes.py) | `api/` | Health probes, auth/policy checks, device, operation, session, subscription, and schema routes. |
| [`test_application_devices.py`](unit/test_application_devices.py) | `application/devices.py` | `DeviceRegistryService` CRUD, `testing_env.json` seeding, and endpoint resolution. |
| [`test_application_dto.py`](unit/test_application_dto.py) | `application/dto.py` | DTO validation, protocol-operation support matrix, and request repetition expansion. |
| [`test_application_operations.py`](unit/test_application_operations.py) | `application/operations.py` | `DeviceOperationService` unary, batch, validate-only, and session-bound execution. |
| [`test_application_sessions.py`](unit/test_application_sessions.py) | `application/sessions.py` | `DeviceSessionManager` leasing, lock/commit state tracking, and TTL expiration. |
| [`test_application_subscriptions.py`](unit/test_application_subscriptions.py) | `application/subscriptions.py` | `SubscriptionService` lifecycle, bounded `FanoutQueue` behavior, and consumer cursors. |
| [`test_cli_builder.py`](unit/test_cli_builder.py) | `cli/cli_builder.py` | CLI output flag mapping (`--output-type`, `--output-file`, `--output-format`). |
| [`test_path.py`](unit/test_path.py) | `modules/path.py` | XPath string parsing, keys extraction, gNMI `Path` and `PathElem` generation. |
| [`test_config_models.py`](unit/test_config_models.py) | `config/model.py`, `config/operations.py` | Immutability, validation logic, and property accessors for `ConnectionConfig`, `SessionConfig`, and `OperationConfig`. |
| [`test_delivery_mode.py`](unit/test_delivery_mode.py) | `config/delivery.py` | RFC 8641 protocol-neutral delivery mode resolution, case-insensitivity, and validation. |
| [`test_validators.py`](unit/test_validators.py) | `specs/*_validator.py` | Offline XPath and filter syntax validation for gNMI and NETCONF. |
| [`test_stream_types.py`](unit/test_stream_types.py) | `specs/stream_types.py` | Stream event lifecycle, sync markers, and stream command queues. |
| [`test_schema_profile.py`](unit/test_schema_profile.py) | `config/schema.py` | Canonical profile sorting, JSON serialization, and deterministic SHA-256 `profile_id` hashing. |
| [`test_schema_resolver.py`](unit/test_schema_resolver.py) | `core/schema_resolver.py` | Topological dependency resolution and cyclic import handling. |
| [`test_schema_validator.py`](unit/test_schema_validator.py) | `core/schema_validator.py` | Path existence validation and `config false` write protection. |
| [`test_schema_serializer.py`](unit/test_schema_serializer.py) | `core/schema_serializer.py` | `libyang` `DataNode` serialization to RFC 7951 JSON and XML. |
| [`test_schema_cache.py`](unit/test_schema_cache.py) | `core/schema_db.py` | Atomic model file caching, SHA-256 integrity verification, and directory traversal protection. |
| [`test_schema_context.py`](unit/test_schema_context.py) | `core/schema_context.py` | `ContextRegistry` thread safety and isolated `libyang.Context` management per `profile_id`. |
| [`test_schema_db_setup.py`](unit/test_schema_db_setup.py) | `core/schema_db.py` | SQLite schema creation, foreign key constraints, and indexing. |
| [`test_schema_catalog.py`](unit/test_schema_catalog.py) | `core/schema_catalog.py` | Mapping protocol capabilities (`ModelData`) to canonical `ModuleRef` tuples. |
| [`test_schema_feedback_fixes.py`](unit/test_schema_feedback_fixes.py) | `core/` regressions | Regression fixes and edge case validations for schema manager. |

---

### 2.2 Composite Tests (`tests/composite/`)
* **Characteristics**: Verifies interaction between multiple subsystems, end-to-end execution pipelines, worker threadpools, transaction lifecycles, and large-scale YANG schema compilations.
* **Scope**: Multiple components working in concert (CLI -> Config -> Manager -> Worker -> Client -> Schema -> Formatter).
* **When to Run**: Prior to submitting pull requests or committing substantial feature additions.

| Test File | Subsystems Integrated | Scope & Verification |
| :--- | :--- | :--- |
| [`test_api_testbed.py`](composite/test_api_testbed.py) | `api/`, `application/`, live testbed | Live FastAPI integration against SRLinux, cEOS, and Netopeer2 (via `testing_env.json`). |
| [`test_application_integration.py`](composite/test_application_integration.py) | `cli/`, `application/`, `managers/` | End-to-end YAML/CLI adapter, `CLIApplicationRunner`, `DeviceOperationService`, and legacy manager compatibility. |
| [`test_managers.py`](composite/test_managers.py) | `application/operations.py`, `managers/unary_worker.py`, `managers/subscribe_session.py` | Worker resolution (`DeviceOperationService`), `SequentialWorker`, `SubscribeSession`, and `OutputHandler` formatting. |
| [`test_schema_manager.py`](composite/test_schema_manager.py) | `core/schema_manager.py`, `core/schema_db.py`, `core/schema_context.py` | Singleton `SchemaManager` lifecycle coordinating DB metadata, dependency resolution, and context compilation. |
| [`test_cli_schema.py`](composite/test_cli_schema.py) | `cli/cmd.py`, `config/schema.py` | CLI argument parsing into `SchemaConfig`, YAML schema mappings, and multi-target schema overrides. |
| [`test_gnmi_set.py`](composite/test_gnmi_set.py) | `managers/unary_worker.py`, `specs/gnmi_validator.py`, `config/operations.py` | `SetWorker` validation and execution with update/replace/delete change models. |
| [`test_gnmi_subscribe.py`](composite/test_gnmi_subscribe.py) | `application/subscriptions.py`, `specs/gnmi_client.py` | Mocked and live gNMI subscription concurrency, state transitions, and multi-target streaming. |
| [`test_gnmi_schema_integration.py`](composite/test_gnmi_schema_integration.py) | `specs/gnmi_client.py`, `core/schema_manager.py` | End-to-end gNMI payload validation and RFC 7951 JSON serialization. |
| [`test_netconf_edit_config.py`](composite/test_netconf_edit_config.py) | `specs/netconf_client.py`, `managers/unary_worker.py` | NETCONF candidate `<edit-config>` payload generation, default-operation handling, and RPC error parsing. |
| [`test_netconf_transactions.py`](composite/test_netconf_transactions.py) | `specs/netconf_client.py`, `managers/unary_worker.py` | Datastore locking, candidate validation, confirmed commit, persist-id tokens, and automatic `<discard-changes>` rollback. |
| [`test_netconf_schema_integration.py`](composite/test_netconf_schema_integration.py) | `specs/netconf_client.py`, `core/schema_manager.py` | End-to-end NETCONF schema-aware validation and namespaced XML serialization. |
| [`test_netconf_subscribe.py`](composite/test_netconf_subscribe.py) | `managers/subscribe_session.py`, `specs/netconf_client.py` | RFC 5277 streaming event notification subscription lifecycle, time bounds, and XML event parsing. |
| [`test_core_yang_srlinux.py`](composite/test_core_yang_srlinux.py) | `core/`, `testing_env.json` | Multi-model Nokia SR Linux YANG tree compilation and dependency resolution. |
| [`test_core_yang_ocnos.py`](composite/test_core_yang_ocnos.py) | `core/`, `testing_env.json` | Multi-model IP Infusion OcNOS YANG tree compilation and dependency resolution. |

---

## 3. Execution Commands

`pytest.ini` defines markers `unit`, `composite`, and `live`. Use `python -m pytest` (or activate the virtual environment).

### Run Offline Test Suite (Recommended)
```bash
pytest -m "not live" tests/
```

### Run Unit Tests Only (Fast Feedback ~5s)
```bash
pytest tests/unit/
```

### Run Offline Composite Tests Only
```bash
pytest -m "not live" tests/composite/
```

### Run a Single Test File
```bash
pytest tests/unit/test_config_models.py
pytest tests/composite/test_netconf_transactions.py
```

### Filter Tests by Pattern or Keyword
```bash
pytest -k "delivery_mode"
pytest -k "transactions"
```

---

## 4. Test Fixtures & Models

* **Testbed Configuration (`tests/testing_env.json`):**
  Defines lab target endpoints (`srlinux`, `ceos`, `ocnos`, `netopeer2`), credentials, and vendor YANG directory paths consumed by `tests/yang_fetcher.py` and testbed integration tests.
* **Configuration & Payload Fixtures (`tests/fixtures/`):**
  Contains sample YAML configurations and XML payloads used across composite and unit tests (e.g. `tests/fixtures/netconf_transaction_pipeline_test.yaml`, `tests/fixtures/gnmi_multi_operation_pipeline_test.yaml`).

