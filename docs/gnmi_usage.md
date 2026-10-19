# gNMI User Guide (netmgnt-client-py)

## 1. Overview

**`netmgnt-client-py`** provides a full-featured, high-performance client for the **gRPC Network Management Interface (gNMI)**. Built on top of `grpcio` and Protobuf stubs, it connects to any gNMI-compliant network operating system (Nokia SR Linux, Arista EOS, Cisco IOS-XE/XR, Juniper Junos) over HTTP/2.

### Supported RPC Operations
* **`capability`**: Query gNMI server version, supported YANG models, and supported encodings.
* **`get`**: Retrieve operational state and configuration datastores across single or multiple paths.
* **`set`**: Atomic configuration mutations (`update`, `replace`, `delete`) with optional offline YANG schema validation.
* **`subscribe`**: Long-lived streaming telemetry supporting:
  * **`STREAM`**: Real-time telemetry (`sample` interval, `on_change` event-driven, or `target_defined`).
  * **`ONCE`**: Single snapshot retrieval across multiple targets.
  * **`POLL`**: Interactive on-demand polling controller.

---

## 2. CLI Usage & Recipes

### 2.1 Basic CLI Syntax

```bash
python client_main.py [connection & output options] gnmi <operation> [operation options]
```

#### Common Connection & Output Flags
* `-t, --target <IP:PORT>`: Target router endpoint (e.g. `172.20.20.2:57401`).
* `-u, --username <USER>` / `-p, --password <PASS>`: Authentication credentials.
* `-i, --insecure`: Use plaintext gRPC (disable TLS).
* `--tls-ca <FILE>`, `--tls-cert <FILE>`, `--tls-key <FILE>`: TLS mutual authentication certificates.
* `--output-format {json,text,xml}`: Output format (default: `json`).
* `--output-type {stdout,stderr,file,syslog}`: Destination sink (default: `stdout`).
* `--output-file <PATH>`: Destination file when `--output-type file` is selected.
* `-e, --encoding {json_ietf,json,bytes,proto,ascii}`: gNMI wire encoding (default: `json_ietf`).

---

### 2.2 CLI Operation Recipes

#### Recipe 1: Query Server Capabilities
Discover the device's supported YANG models, gNMI version, and supported encodings:
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi capability
```

#### Recipe 2: Get Operational State & Configuration
Query operational state or configuration for a specific path:
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi get --path '/interfaces/interface[name=mgmt0]/state'
```

Query with a common prefix and multiple paths:
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi get --prefix '/interfaces/interface[name=mgmt0]' \
           --path 'state/oper-status' \
           --path 'state/counters'
```

#### Recipe 3: Set Configuration Mutations
Apply atomic configuration modifications using `update`, `replace`, or `delete`.

**Update a single leaf value inline:**
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi set --update-path '/interfaces/interface[name=mgmt0]/config/description' \
           --update-value 'Configured-via-gNMI'
```

**Alternative paired syntax (`PATH:::VALUE`):**
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi set --update '/interfaces/interface[name=mgmt0]/config/description:::Configured-via-gNMI'
```

**Replace from a JSON payload file (`PATH:::@file`):**
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi set --replace '/system/config:::@system_config.json'
```

**Delete a container or leaf:**
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi set --delete '/interfaces/interface[name=mgmt0]/config/description'
```

#### Recipe 4: Streaming Telemetry (STREAM Mode)

**Periodic Sampling (SAMPLE):**
Receive updates every 10 seconds:
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi subscribe --mode stream --sub-mode sample --sample-interval 10 \
                 --path '/interfaces/interface[name=mgmt0]/state/counters'
```

**Event-Driven Telemetry (ON_CHANGE):**
Receive immediate notifications only when values mutate:
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi subscribe --mode stream --sub-mode on_change \
                 --path '/interfaces/interface[name=mgmt0]/state/oper-status'
```

#### Recipe 5: Snapshot (ONCE) and Interactive Polling (POLL)

**One-Time Snapshot:**
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi subscribe --mode once --path '/interfaces/interface[name=mgmt0]'
```

**Interactive On-Demand Polling (POLL Mode):**
Starts an interactive terminal menu allowing you to trigger on-demand telemetry polls:
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi subscribe --mode poll --path '/interfaces/interface[name=mgmt0]/state'
```

#### Recipe 6: Strict Offline YANG Validation
Validate paths and payload values against local YANG definitions using the built-in `libyang` engine before transmitting to the device:
```bash
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  --yang-dir ./yang/srlinux \
  --validate-schema \
  gnmi set --update-path '/interfaces/interface[name=mgmt0]/config/description' \
           --update-value 'Valid-Description'
```

---

## 3. YAML Configuration Pipelines

Using YAML allows you to define complex multi-target operations or sequential operational pipelines in a repeatable, version-controlled file.

Launch YAML configs using:
```bash
python client_main.py gnmi -c pipeline.yaml
```

### 3.1 Sequential Operational Pipeline

Execute a sequence of dependent operations (e.g., Query -> Mutate -> Verify -> Delete) over a single persistent session:

```yaml
protocol: "gnmi"
insecure: true

targets:
  172.20.20.2:57401:
    username: "admin"
    password: "NokiaSrl1!"
    operations:
      # Step 1: Query capabilities
      - capability: {}

      # Step 2: Read current hostname
      - get:
          path:
            - "/system/state/hostname"

      # Step 3: Apply mutation
      - set:
          update:
            - path: "/interfaces/interface[name=mgmt0]/config/description:::Managed-via-YAML"

      # Step 4: Verify mutation
      - get:
          path:
            - "/interfaces/interface[name=mgmt0]/config/description"

outputs:
  default_output:
    output-type: stdout
    format: json
```

### 3.2 Multi-Target Streaming Subscriptions

Stream metrics from multiple endpoints concurrently across named subscription profiles:

```yaml
protocol: "gnmi"
insecure: true

subscriptions:
  interface_counters:
    mode: "stream"
    encoding: "json_ietf"
    subscription:
      path:
        - '/interfaces/interface[name="mgmt0"]/state/counters'
      mode: "sample"
      sample_interval: 15

  interface_status:
    mode: "stream"
    encoding: "json_ietf"
    subscription:
      path:
        - '/interfaces/interface[name="mgmt0"]/state/oper-status'
      mode: "on_change"

targets:
  172.20.20.2:57401:
    username: "admin"
    password: "NokiaSrl1!"
    subscriptions:
      - interface_counters
      - interface_status

  172.20.20.3:57401:
    username: "admin"
    password: "NokiaSrl1!"
    subscriptions:
      - interface_counters

outputs:
  file_sink:
    output-type: file
    path: "/tmp/gnmi_telemetry.json"
    format: json
```

