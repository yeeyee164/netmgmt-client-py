# netmgnt-client-py

A high-performance, protocol-agnostic Northbound (NB) network management and automation engine written in Python 3. 

**`netmgnt-client-py`** provides a unified interface to interact with multi-vendor Network Operating Systems (Cisco IOS-XE/XR, Juniper Junos, Arista EOS, Nokia SR Linux/SROS) using standardized, YANG-driven management protocols:
* **gNMI** (gRPC Network Management Interface over HTTP/2)
* **NETCONF** (RFC 6241, RFC 5277, RFC 6022 over SSH/TLS)
* *(Planned)* **RESTCONF** (RFC 8040 over HTTPS)

---

## Key Features

* **Dual Invocation Paradigms:**
  * **Nested CLI Subparsers:** Intuitive command-line interface for ad-hoc inspection, troubleshooting, and one-liner automation (`client_main.py <protocol> <operation> [options]`).
  * **Hierarchical YAML Pipelines:** Version-controlled multi-target orchestration, sequential multi-step transactional workflows, and streaming subscriptions (`client_main.py [protocol] -c config.yaml`).
* **Multi-Protocol Operational Coverage:**
  * **gNMI:** `capability`, `get`, `set` (atomic mutations), and `subscribe` (`STREAM` with `sample` or `on_change`, `ONCE` snapshots, and `POLL` interactive polling).
  * **NETCONF:** `capability` (`<hello>`), `get`, `get-config`, RFC 6022 dynamic `get-schema`, `<edit-config>` with candidate datastore locking, candidate validation, confirmed commit with rollback timer, and RFC 5277 streaming event notifications.
* **Integrated YANG Schema Engine:**
  * Embedded `libyang` compilation and SQLite metadata caching (`core/schema_cache.db`).
  * Offline path and leaf type validation enforcing `config false` write protection.
  * Automatic translation between semantic models, RFC 7951 JSON, and namespaced XML subtrees.
* **Pluggable Telemetry Sinks:**
  * Route responses and real-time streaming telemetry to `stdout`, `stderr`, rotated local files, or RFC 5424 Syslog servers.

---

## Installation & Setup

### Prerequisites
* Python 3.10+ (Python 3.10.12 recommended)
* System C-libraries for SSH and XML manipulation (`libxml2-dev`, `libxslt1-dev`, `libffi-dev`)
* System C-libraries for `libyang`(>= v5.0.0) (`libpcre2`(>= 10.21))

### Quick Setup

```bash
# 1. Activate your virtual environment. For example:
source $HOME/pyvenv3.10/bin/activate

# 2. Install required Python packages
pip install grpcio grpcio-tools ncclient xmltodict pyyaml lxml pytest libyang

# 3. Verify CLI entrypoint
python client_main.py --help
python client_main.py gnmi --help
python client_main.py netconf --help
```

---

## Quick Usage

The engine is launched via `client_main.py` using either command-line flags or YAML configuration files.

Below cases were tested with this environment:
```text

  +--------------------+                                +--------------------+
  |                    |                                |                    |
  |   client_main.py   |   172.20.20.2:(830, 57401)     |   Nokia SR Linux   |
  |               eth0 o<------------------------------>o mgmt-ipv4          |
  +--------------------+                                +--------------------+

```

### 1. CLI Quickstart

#### gNMI Quick Commands
```bash
# Query gNMI server capabilities
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi capability

# Query operational state
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi get --path '/interfaces/interface[name=mgmt0]/state'

# Mutate configuration
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi set --update-path '/interfaces/interface[name=mgmt0]/config/description' \
           --update-value 'Configured-via-CLI'

# Stream telemetry with a 10s sample interval
python client_main.py -t 172.20.20.2:57401 -u admin -p NokiaSrl1! -i \
  gnmi subscribe --mode stream --sub-mode sample --sample-interval 10 \
                 --path '/interfaces/interface[name=mgmt0]/state/counters'
```

#### NETCONF Quick Commands
```bash
# Read running configuration
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf get-config --source running --filter '/interfaces'

# Download YANG model directly from device (RFC 6022)
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf get-schema --identifier openconfig-system

# Edit candidate datastore with exclusive lock, validation, and commit
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf edit-config --target candidate --config @payload.xml --lock --validate

# Stream real-time NETCONF event notifications (RFC 5277)
python client_main.py -t 172.20.20.2:830 -u admin -p NokiaSrl1! \
  netconf subscribe --stream-name NETCONF
```

---

### 2. YAML Pipeline Quickstart

Execute repeatable multi-operation workflows or multi-target subscriptions via YAML:

```bash
python client_main.py gnmi -c pipeline_gnmi.yaml
python client_main.py netconf -c pipeline_netconf.yaml
```

**Example NETCONF Transaction Pipeline (`pipeline_netconf.yaml`):**
```yaml
protocol: "netconf"

targets:
  172.20.20.2:830:
    username: "admin"
    password: "NokiaSrl1!"
    operations:
      - capability: {}
      - edit-config:
          target: "candidate"
          config: "payloads/interface_config.xml"
          commit: false
      - validate:
          source: "candidate"
      - commit:
          confirmed: false
      - get-config:
          source: "running"
          filter: '<interfaces xmlns="http://openconfig.net/yang/interfaces"/>'

outputs:
  default_output:
    output-type: stdout
    format: json
```

---

## Protocol User Guides

For detailed, in-depth walkthroughs of all CLI flags, options, schema validation, and YAML orchestration patterns, consult the dedicated protocol user guides:

| Protocol | Documentation Guide | Key Topics Covered |
| :--- | :--- | :--- |
| **gNMI** | [**gNMI User Guide**](docs/gnmi_usage.md) | `capability`, `get`, `set` mutations, `subscribe` (`STREAM`, `ONCE`, `POLL`), encodings, offline YANG validation, multi-target subscription pipelines. |
| **NETCONF** | [**NETCONF User Guide**](docs/netconf_usage.md) | `capability`, `get`, `get-config`, RFC 6022 `get-schema`, `edit-config`, candidate locks, validation, confirmed commit with rollback, RFC 5277 event subscriptions. |

---

## Contributor & Architecture Navigation

If you are developing or contributing to **`netmgnt-client-py`**:
* **Architecture, Module Inventory & Guidelines:** Consult [`AGENTS.md`](AGENTS.md) for a concise component breakdown of each directory (`config/`, `core/`, `managers/`, `specs/`, `modules/`, `ui/`).
* **YANG Schema & Caching Engine:** Consult [`core/README.md`](core/README.md) for data models, SQLite cache schemas, and `libyang` integration details.
* **Running Tests:** Run the full test suite via `pytest tests/`.