# NETCONF User Guide (netmgnt-client-py)

## 1. Overview

**`netmgnt-client-py`** provides an enterprise-grade **NETCONF (RFC 6241)** client implementation built on top of `ncclient` over SSH/TLS. It supports multi-vendor network operating systems (Nokia SR Linux/SROS, Cisco IOS-XE/XR, Juniper Junos, Arista EOS) with full candidate datastore transactions, atomic commits, confirmed rollback, dynamic schema discovery, and RFC 5277 event subscriptions.

### Supported RPC Operations
* **`capability`**: Negotiate `<hello>` and list client/server capabilities.
* **`get`**: Retrieve running configuration and device operational state.
* **`get-config`**: Query specific configuration datastores (`running`, `candidate`, `startup`).
* **`get-schema`**: Download YANG data models on-the-fly via RFC 6022.
* **`edit-config`**: Mutate configuration with fine-grained transaction controls:
  * Default operations: `merge`, `replace`, `none`.
  * Datastore locking: `--lock` (acquires lock, edits, and guarantees unlock in `finally:`).
  * Candidate verification: `--validate` (`<validate>` RPC).
  * Atomic commits: `--commit`, `--confirmed`, `--confirm-timeout`.
  * Safe error handling: automatic `<discard-changes>` on `RPCError`.
* **`commit` / `discard-changes` / `cancel-commit`**: Standalone transaction lifecycle operations.
* **`subscribe`**: Stream asynchronous event notifications in real time via RFC 5277.

---

## 2. CLI Usage & Recipes

### 2.1 Basic CLI Syntax

```bash
python client_main.py [connection & output options] netconf <operation> [operation options]
```

#### Common Connection & Output Flags
* `-t, --target <IP:PORT>`: Target NETCONF endpoint (e.g. `172.20.20.2:830`).
* `-u, --username <USER>` / `-p, --password <PASS>`: Authentication credentials.
* `--device <NAME>`: Vendor driver for `ncclient` (e.g., `default`, `sros`, `junos`, `iosxr`).
* `--ssh-key <PATH>`: Path to private SSH key for key-based authentication.
* `--output-format {json,xml,text}`: Output format (default: `json`).
* `--output-type {stdout,stderr,file,syslog}`: Destination sink (default: `stdout`).
* `--output-file <PATH>`: Destination file when `--output-type file` is used.

---

### 2.2 CLI Operation Recipes

#### Recipe 1: Discover Server Capabilities
Query device capabilities and supported NETCONF extensions:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf capability
```

#### Recipe 2: Read Datastore Configuration (`get-config`)
Query running configuration with an XPath expression:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf get-config --source running --filter '/interfaces'
```

Query using a Subtree XML filter:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf get-config --source running \
  --filter '<interfaces xmlns="http://openconfig.net/yang/interfaces"><interface><name>mgmt0</name></interface></interfaces>' \
  --filter-type subtree
```

#### Recipe 3: Download YANG Models via RFC 6022 (`get-schema`)
Retrieve a specific YANG module directly from the router:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf get-schema --identifier openconfig-system
```

#### Recipe 4: Edit Configuration with Candidate Transactions
Apply an XML configuration snippet from a file, acquire datastore locks, validate the candidate datastore, and commit atomically:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf edit-config \
    --target candidate \
    --config @payload.xml \
    --default-operation merge \
    --lock \
    --validate
```

*Note: By default, candidate commits are automatically executed on successful edits unless `--no-commit` is specified.*

#### Recipe 5: Confirmed Commit with Rollback Protection
Commit changes with a rollback timer (e.g., automatically reverts after 60 seconds unless confirmed):
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf edit-config \
    --target candidate \
    --config @payload.xml \
    --confirmed \
    --confirm-timeout 60
```

Confirm the commit to make it permanent:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf commit
```

Or abort and revert immediately:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf discard-changes
```

#### Recipe 6: Streaming Event Notifications (RFC 5277)
Subscribe to real-time asynchronous NETCONF notifications (e.g., link flaps, syslog streams, threshold breaches):
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf subscribe --stream-name NETCONF
```

Filter notifications starting from an RFC 3339 timestamp:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf subscribe --stream-name NETCONF --start-time "2026-10-16T00:00:00Z"
```

#### Recipe 7: Offline Schema Validation with libyang
Compile local vendor YANG models and validate your XML payloads before sending them over the wire:
```bash
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  --yang-dir ./yang/srlinux \
  --validate-schema \
  netconf edit-config --target candidate --config @payload.xml
```

---

## 3. YAML Configuration Pipelines

Launch multi-step NETCONF pipelines using:
```bash
python client_main.py netconf -c pipeline.yaml
```

### 3.1 Transactional Multi-Step Pipeline

Execute a transactional workflow (Query Capabilities -> Edit -> Validate -> Commit -> Verify):

```yaml
protocol: "netconf"

targets:
  172.20.20.2:830:
    username: "admin"
    password: "NokiaSrl1!"
    operations:
      # Step 1: Discover capabilities
      - capability: {}

      # Step 2: Download YANG model
      - get-schema:
          identifier: "openconfig-interfaces"

      # Step 3: Edit candidate datastore without immediate commit
      - edit-config:
          target: "candidate"
          config: "payloads/update_description.xml"
          commit: false

      # Step 4: Validate candidate syntax and semantics
      - validate:
          source: "candidate"

      # Step 5: Commit changes to running datastore
      - commit:
          confirmed: false

      # Step 6: Verify configuration in running
      - get-config:
          source: "running"
          filter: '<interfaces xmlns="http://openconfig.net/yang/interfaces"><interface><name>mgmt0</name></interface></interfaces>'
          filter_type: "subtree"

outputs:
  default_output:
    output-type: stdout
    format: json
```

### 3.2 RFC 5277 Event Notification Stream

Subscribe to telemetry notifications and route events to a Syslog server:

```yaml
protocol: "netconf"

targets:
  172.20.20.2:830:
    username: "admin"
    password: "NokiaSrl1!"
    operations:
      - subscribe:
          stream_name: "NETCONF"
          poll_timeout: 1.0

outputs:
  syslog_sink:
    output-type: syslog
    syslog_host: "10.10.10.50"
    syslog_port: 514
    format: json
```

