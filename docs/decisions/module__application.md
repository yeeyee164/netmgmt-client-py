# Architecture Decision Records (ADRs) for application module

## ADR-005: Decoupled Application Service Layer with Pure DTO Boundaries

- **Status:** Proposed
- **Date:** 2026-10-21
- **Context:**
  `netmgmt-client-py` was originally constructed around a CLI execution lifecycle where `ui/cmd.py` directly compiles terminal arguments or YAML files into `ParsedConfig` and `SessionConfig`. These objects tightly bind operational intent (paths, mutations) with transport secrets (credentials, certificates), local CLI output routing (`OutputHandler`), and filesystem schema paths.
  
  Supporting modern Web APIs (FastAPI / WebSocket), automated workflows, and multi-tenant telemetry streaming requires isolating caller intent from CLI sinks and local runtime configurations.

- **Decision:**
  1. Introduce an `application/` service package containing pure Data Transfer Objects (`application/dto.py`) that never import CLI (`argparse`), transport drivers (`grpc`, `ncclient`), or presentation sinks (`OutputHandler`).
  2. Enforce **explicit protocol declaration** (`protocol: "gnmi" | "netconf"`) across all DTOs and device registry profiles, strictly rejecting protocol auto-detection heuristics.
  3. Keep the underlying domain models (`config/`) and worker implementations (`managers/unary_worker.py`) unchanged; have `DeviceOperationService` assemble `SessionConfig` internally and invoke `worker.start()` directly to return structured Python dictionaries.
  4. Encapsulate continuous streaming sessions in `SubscriptionService`, using thread-safe bounded queues and `StreamContext` lifecycle controls.

- **Alternatives Considered:**
  - *Direct FastAPI routing to `ui/cmd.py` (`ParsedConfig`)*: Rejected because API clients would have to simulate fake CLI options, mock `OutputHandler` file descriptors, and expose internal file paths.
  - *Automatic protocol negotiation via port heuristics*: Rejected because switches often expose both gNMI and NETCONF on non-standard ports or behind common TLS proxies.
  - *Heavyweight ORM / Active-Record Domain Models*: Rejected because the existing dataclass architecture in `config/` is already immutable, thread-safe, and highly optimized.

- **Consequences:**
  - `ui/cmd.py` becomes a thin presentation adapter translating CLI arguments into `ExecutionRequestDTO` and passing an optional `OutputHandler` sink.
  - Unary operations return structured results directly to callers (`ExecutionResultDTO`), enabling direct JSON serialization in REST endpoints.
  - Telemetry streaming outlives single HTTP cycles and is addressable by unique `subscription_id` tokens.
  - Dual backwards compatibility is preserved: all existing YAML pipelines and CLI commands remain 100% operational.