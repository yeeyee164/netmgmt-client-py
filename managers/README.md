# Worker & Factory Layer (`managers/`)

## 1. Overview

The [`managers/`](__init__.py) package provides the worker execution and client/validator factory layer for **`netmgmt-client-py`**. It bridges declarative [`SessionConfig`](../config/model.py) instances and protocol-specific clients/validators in [`specs/`](../specs/), orchestrated by [`application/`](../application/__init__.py) (`DeviceOperationService`, `SubscriptionService`) and [`cli/runner.py`](../cli/runner.py).

The layer separates execution into two worker models:
1. **Unary Workers ([`unary_worker.py`](unary_worker.py)):** Run bounded, one-shot or sequential RPCs (`capability`, `get`, `set`, `get-schema`, `transaction`).
2. **Streaming Telemetry ([`subscribe_session.py`](subscribe_session.py)):** Runs continuous, long-lived subscriptions (`STREAM`, `ONCE`, `POLL`, RFC 5277 notifications) and emits [`StreamEvent`](../specs/stream_types.py) items into a target queue.

---

## 2. Module Inventory

| File | Primary Exports | Responsibility |
| :--- | :--- | :--- |
| [`managers/factory.py`](factory.py) | `ClientFactory`, `ValidatorFactory` | Instantiates protocol client drivers (`GNMIClient`, `NetconfClient`) and offline validators (`GNMIValidator`, `NetconfValidator`). |
| [`managers/unary_worker.py`](unary_worker.py) | `BaseUnaryWorker`, `GetWorker`, `SetWorker`, `CapabilityWorker`, `TransactionWorker`, `SequentialWorker` | Protocol-agnostic worker implementations for single-request RPC execution and multi-step sequential pipelines. |
| [`managers/subscribe_session.py`](subscribe_session.py) | `SubscribeSession` | Encapsulates a streaming telemetry connection, event loop consumption, and queue delivery. |

---

## 3. Key Component Reference

### 3.1 Worker Classes ([`managers/unary_worker.py`](unary_worker.py))
Workers isolate operational execution from transport protocols:
* **`SequentialWorker`**: Executes an ordered pipeline of operations within a single client connection context manager (`with client:`), ensuring transaction continuity (e.g. `lock` -> `edit-config` -> `validate` -> `commit` -> `unlock`).
* **`SetWorker`**: Performs offline syntax path validation via [`ValidatorFactory.get_validator(self.protocol)`](factory.py) before dispatching mutations over the wire.
* **`GetWorker` / `CapabilityWorker` / `TransactionWorker`**: Formats standardized response dictionaries containing `session_id`, `target`, `rpc`, `data`, and `protocol`.

### 3.2 `ClientFactory` & `ValidatorFactory` ([`managers/factory.py`](factory.py))
Instantiates concrete protocol implementations:
* `ClientFactory.get_client(protocol, target, username, password, security, **kwargs)`: Returns either a [`GNMIClient`](../specs/gnmi_client.py) or [`NetconfClient`](../specs/netconf_client.py) configured with connection parameters and schema configurations.
* `ValidatorFactory.get_validator(protocol)`: Returns [`GNMIValidator`](../specs/gnmi_validator.py) or [`NetconfValidator`](../specs/netconf_validator.py).

---

## 4. Usage Examples

### 4.1 Executing Sessions via `CLIApplicationRunner`
```python
from cli.cmd import build_args, config_builder
from cli.runner import CLIApplicationRunner

# Parse CLI arguments into ParsedConfig
args = build_args(["-t", "172.20.20.2:57401", "-i", "gnmi", "capability"])
cfg = config_builder(args)

# Execute via CLIApplicationRunner -> DeviceOperationService
CLIApplicationRunner.execute(cfg)
```

### 4.2 Direct Unary Worker Invocation
```python
from config.model import SessionConfig, ConnectionConfig, Protocol
from config.operations import GetOperation
from config.selectors import PathSelector
from managers.unary_worker import GetWorker

# Define session
session = SessionConfig(
    connection=ConnectionConfig(
        target="172.20.20.2:57401",
        username="admin",
        password="NokiaSrl1!",
        insecure=True,
    ),
    protocol=Protocol.GNMI,
    operation=GetOperation(
        selector=PathSelector(paths=("/system/state/hostname",)),
    ),
)

# Run worker directly
worker = GetWorker(config=session)
result = worker.start()
print("Result:", result["data"])
```

