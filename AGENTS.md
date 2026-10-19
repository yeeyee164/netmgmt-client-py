# Context & Architecture Guide: netmgnt-client-py

## 1. Project Background & Mission Statement

**`netmgnt-client-py`** is an enterprise-grade, protocol-agnostic Northbound (NB) network automation client engine written in Python 3. It interfaces with multi-vendor network operating systems (Cisco IOS-XE/XR, Juniper Junos, Arista EOS, Nokia SR Linux) using standardized YANG-based management protocols:

1. **gNMI** (gRPC Network Management Interface via `grpcio` and Protobuf stubs in `specs/gnmi/`).
2. **NETCONF** (RFC 6241, RFC 5277, RFC 6022 via `ncclient` over SSH/TLS).
3. *(Planned)* **RESTCONF** (RFC 8040 over HTTPS).

The engine supports dual execution paradigms:
* **Nested CLI Subparsers:** `python client_main.py <protocol> <operation> [options]`
* **Hierarchical YAML Pipelines:** `python client_main.py [gnmi|netconf] -c config.yaml` (supporting multi-target, multi-subscription, and multi-operation orchestration).

---

## 2. Module Directory & Navigation Guide

This section provides a structured inventory of the codebase to help contributors and agents quickly locate and navigate modules without needing to read entire subtrees:

| Path | Primary Exports / Components | Primary Role & Description |
| :--- | :--- | :--- |
| [`client_main.py`](client_main.py) | Entrypoint script | Universal CLI launcher; parses arguments, sets up logging, builds `ParsedConfig`, and dispatches to `ManagerFactory`. |
| [`config/`](config/) | `SessionConfig`, `OperationConfig`, `DeliveryPolicy`, `SchemaConfig` | Domain configuration layer. Decouples session parameters into `ConnectionConfig`, `ExecutionConfig`, `OperationConfig`, and `SchemaConfig`. Defines protocol-neutral `DeliveryMode` (RFC 8641). |
| [`config/protocol_options/`](config/protocol_options/) | `GnmiOptions`, `NetconfOptions` | Protocol-specific configuration structures (gNMI encodings/sample intervals; NETCONF datastores, locks, confirmed commits, RFC 5277 parameters). |
| [`core/`](core/) | `SchemaManager`, `SchemaContext`, `SchemaDB`, `SchemaValidator`, `SchemaSerializer` | Protocol-agnostic YANG schema compilation, SQLite metadata caching (`schema_cache.db`), topological dependency resolution, path validation, and `libyang` XML/JSON serialization. Detailed guide in [`core/README.md`](core/README.md). |
| [`managers/`](managers/) | `ManagerFactory`, `UnaryManager`, `SubscriptionManager`, Worker classes | Concurrency and orchestration layer. Dispatches unary tasks via `ThreadPoolExecutor` and streaming telemetry via worker threads and a centralized `queue.Queue`. |
| [`specs/`](specs/) | `BaseClient`, `GNMIClient`, `NetconfClient`, `BaseValidator`, Stream types | Concrete protocol implementations. Implements gRPC calls for gNMI and `ncclient` SSH/TLS sessions for NETCONF. Implements semantic operations (`execute_capabilities`, `execute_get`, `execute_set`, `execute_subscribe`, `execute_transaction`, `execute_schema`). |
| [`specs/gnmi/`](specs/gnmi/) | `gnmi_pb2.py`, `gnmi_pb2_grpc.py`, `gnmi_ext_pb2.py` | Compiled Google gNMI Protocol Buffer stubs. |
| [`modules/`](modules/) | `OutputHandler`, `ProtocolFormatter`, `SecurityModule`, `parse_path` | Output routing (STDOUT, STDERR, file, RFC 5424 syslog), formatters (JSON, XML, text), TLS/SSH security profiles, and XPath string parsing. |
| [`ui/`](ui/) | `build_args`, `config_builder`, `ParsedConfig` | Interface ingestion layer. Defines `argparse` nested subparsers and hierarchical YAML parser that normalizes inputs into `ParsedConfig`. |
| [`util/`](util/) | `utils.py` | Common helper functions (file reading, byte conversions, string formatting). |
| [`protos/`](protos/) | `gnmi.proto`, `gnmi_ext.proto`, `tunnel.proto` | Source Protocol Buffer definitions for gNMI and gRPC tunneling. |
| [`tests/`](tests/) | Unit & Composite test suites, YAML/XML fixtures | Test suite covering models, validators, managers, schema compilation, transaction lifecycles, and streaming subscriptions. |
| [`docs/`](docs/) | Architecture and user guides | Detailed specifications, milestone records, and protocol user guides ([`docs/gnmi_usage.md`](docs/gnmi_usage.md), [`docs/netconf_usage.md`](docs/netconf_usage.md)). |

---

## 3. Core Architecture & Data Flow

```text
[ CLI Input / YAML Config ]
            │
            ▼
       [ ui/cmd.py ]  ──────────> Creates [ ParsedConfig ] -> [ SessionConfig ]
            │
            ▼
   [ ManagerFactory ]
        ├── UnaryManager            ───> Spawns Worker Threads [ GetWorker / SetWorker / CapabilityWorker / TransactionWorker / SequentialWorker ]
        └── SubscriptionManager     ───> Spawns Worker Threads [ SubscribeSession ]
                                                │
                                                ▼
                                        [ ClientFactory ]
                                        ├── GNMIClient (BaseClient)    ──> HTTP/2 (gRPC via grpcio)
                                        └── NetconfClient (BaseClient) ──> SSH / TLS (ncclient)
                                                │
                                                ▼
                                        [ OutputHandler ]
                                        └── Formatter (GNMIFormatter / NETCONFFormatter)
                                                │
                                                ▼
                                     [ stdout / stderr / file / syslog ]
```

### Key Reference Documents
- **Core Architecture & Data Flow:** [`docs/core_architecture_and_data_flow.md`](docs/core_architecture_and_data_flow.md)
- **YANG Schema Engine Architecture:** [`core/README.md`](core/README.md) and [`docs/yang_schema_architecture.md`](docs/schema/yang_schema_architecture.md)
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
* System C-libraries for `libyang`(>= v5.0.0) (`libpcre2`(>= 10.21))

### Virtual Environment & Dependencies
```bash
# Activate virtual environment
source $HOME/pyvenv3.10/bin/activate

# Install / update core dependencies
pip install grpcio grpcio-tools ncclient xmltodict pyyaml lxml pytest libyang

# Run the test suite
pytest tests/
```

### Testing Guidelines
* All tests in the test suite(`./tests`) must pass without regressions before committing code changes.
* When adding new RPC capabilities, create corresponding unit tests and composite workflow YAML fixtures in `tests/`.