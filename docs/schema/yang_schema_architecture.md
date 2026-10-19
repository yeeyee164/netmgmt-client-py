# Phase 4: YANG Schema Management & Dynamic Discovery Architecture

## 1. Overview & Objectives

Step 4 transitions `gnmi_client_py` from string-based and syntactic XML/XPath handling to **schema-aware validation, namespace resolution, and data serialization** using YANG models.

This document consists of two parts:
1. Initial concept of regarding YANG schema support are described in Section 1 through 6.
2. Extended structure/call flow/management are described in Section 7 to end.

This document details:
1. **Required Python package and usage patterns** (`libyang-python`).
2. **Singleton `SchemaManager` architecture and interaction with `SessionConfig` dataclasses**.
3. **Model lifecycle management** (bundled models, local repository loading, and filesystem caching).
4. **Protocol differences and schema caching database** (RFC 6022 `<get-schema>` vs gNMI `CapabilityResponse`).
5. **Concrete prompt-ready instructions for coding agents**.

---

## 2. Python Package & Runtime Requirements

### 2.1 Selected Package: `libyang` (Python CFFI Bindings for `libyang2`)

`libyang` is the industry-standard YANG data modeling library in C, with official high-performance Python bindings (`libyang-python`).

* **PyPI Package**: `libyang`
* **System Prerequisite**: `libyang` version 2.x shared library installed on the host system (or binary wheels if pre-compiled for the platform).
* **Installation**:
  ```bash
  pip install libyang
  ```

### 2.2 Core Usage Patterns

#### A. Initializing Context & Loading Search Directories
```python
import libyang

# Context handles parser settings, search paths, and active modules
ctx = libyang.Context(
    search_dir="/path/to/yang/modules",
    options=libyang.ContextOptions.DISABLE_SEARCHDIR_CWD,
)
# Add additional search directories dynamically
ctx.set_search_dir("/additional/path/to/yang")
```

#### B. Loading and Parsing Modules
```python
# Load module from file
module = ctx.parse_module_file("/path/to/ietf-interfaces@2014-05-08.yang")

# Or parse in-memory string (retrieved via NETCONF <get-schema>)
module = ctx.parse_module_str(yang_content_string)

# Or load by name and revision from registered search paths
module = ctx.load_module("ietf-interfaces", "2014-05-08")
```

#### C. Validating & Navigating Schemas (XPaths)
```python
# Validate whether an XPath target exists in the compiled schema context
schema_node = ctx.find_path("/ietf-interfaces:interfaces/interface")
if schema_node:
    print(f"Node Type: {schema_node.nodetype()}, Config: {schema_node.config_false()}")
```

#### D. Validating & Serializing Data (Dict -> XML / JSON)
```python
# Parse and validate incoming XML/JSON data
dnode = ctx.parse_data_mem(
    xml_string,
    fmt="xml",
    parse_only=False,
    validate=True
)

# Convert libyang DataNode to formatted XML or JSON
xml_output = dnode.print_mem("xml")
json_output = dnode.print_mem("json")
```

---

## 3. Singleton `SchemaManager` & Interaction with `SessionConfig`

### 3.1 Why a Single Global `Context` Is Insufficient

YANG modules from different vendors (e.g., Cisco IOS-XR vs. Arista EOS vs. Juniper Junos) or different OS versions contain **conflicting revisions, deviations, and vendor augmentations**. Loading conflicting revisions of the same module into a single `libyang.Context` will fail or produce invalid validation results.

Therefore, the **`SchemaManager` is a thread-safe Singleton**, but it manages **isolated `libyang.Context` instances keyed by context/session profile**.

### 3.2 Thread-Safe `SchemaManager` Design

```text
+---------------------------------------------------------------------------------+
|                       SchemaManager (Thread-Safe Singleton)                     |
|                                                                                 |
|  - _instance: SchemaManager                                                     |
|  - _lock: threading.RLock()                                                     |
|  - _contexts: Dict[str, libyang.Context] (Key: context_id / device_fingerprint)  |
|  - _schema_db: SchemaMetadataDB (SQLite / Index)                                |
+---------------------------------------------------------------------------------+
           |                                                      |
           v                                                      v
+-----------------------+                              +-----------------------+
|  SessionConfig (A)    |                              |  SessionConfig (B)    |
|  - target: router1    |                              |  target: switch2      |
|  - yang_dirs: [...]   |                              |  yang_dirs: [...]     |
|  - profile: "iosxr7"  |                              |  profile: "eos4"      |
+-----------------------+                              +-----------------------+
```

### 3.3 Core Methods on `SchemaManager`

```python
class SchemaManager:
    _instance = None
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def initialize(self, global_cache_dir: Optional[str] = None):
        """Initializes global cache directory and schema database."""
        ...

    def get_or_create_context(self, context_id: str, search_dirs: List[str] = None) -> libyang.Context:
        """Retrieves or instantiates an isolated libyang.Context for a device profile/session."""
        ...

    def register_module_str(self, context_id: str, module_str: str) -> None:
        """Parses and loads a dynamic YANG schema string into the target context."""
        ...

    def validate_xpath(self, xpath: str, context_id: str) -> bool:
        """Validates if an XPath expression conforms to the compiled schema context."""
        ...

    def validate_and_serialize_payload(
        self,
        data: Union[dict, str],
        data_format: str,
        context_id: str,
        target_format: str = "xml"
    ) -> str:
        """Validates payload against schema and returns namespaced XML/JSON string."""
        ...
```

### 3.4 Integration with `SessionConfig` and Validation Flow

1. **Session Configuration Setup**:
   `SessionConfig` (and its protocol options in `config/protocol_options/`) gains schema attributes:
   * `yang_dirs: List[str]`: Local directories containing offline YANG files.
   * `yang_cache_dir: str`: Directory to store downloaded or precompiled schemas (default: `~/.cache/gnmi_client_py/yang/`).
   * `device_profile: Optional[str]`: User-defined or auto-discovered fingerprint grouping targets with identical schemas.
   * `enable_schema_validation: bool`: Toggle for strict schema validation.

2. **Validation Hook in `modules/validate.py` & `specs/base_validator.py`**:
   Before executing an RPC (`SetWorker`, `UnaryWorker`, or client `set()` / `get()`):
   * `NetconfValidator` / `GNMIValidator` asks `SchemaManager` to:
     * Check path syntax and valid schema node existence.
     * Validate payload values against YANG typedefs, ranges, and patterns.
     * Inject appropriate XML namespaces without requiring the user to handcraft `xmlns:...` attributes.

---

## 4. YANG Model Lifecycle (Providing, Setting Up, Maintaining)

### 4.1 Providing: What Models We Support

* **Standard Base Models**: Shipped with the repository under `schemas/base/` or installed system paths:
  * RFC Core: `ietf-yang-types`, `ietf-inet-types`, `ietf-netconf`, `ietf-netconf-monitoring`.
  * OpenConfig Core: `openconfig-interfaces`, `openconfig-platform`, etc.
* **User-Provided Models**: Pointed to via `--yang-dir` on CLI or `yang_dirs` in YAML configuration.
* **On-Demand Device Models**: Retrievable dynamically at runtime via protocol capabilities.

### 4.2 Setting Up: Initialization Lifecycle

1. **Application Bootstrap**:
   * `SchemaManager` initializes SQLite schema database at `~/.cache/gnmi_client_py/schema_cache.db`.
   * Indexes known local/bundled `.yang` files by parsing module name and revision from their headers.
2. **Session / Client Connection**:
   * Client establishes transport (SSH for NETCONF, gRPC for gNMI).
   * Generates a `context_id` based on `device_profile` or host hash: e.g., `sha256(host + port + capabilities)`.
   * Populates `libyang.Context` with paths specified in `SessionConfig.yang_dirs`.
3. **Dynamic Loading**:
   * If a required module or XPath is missing during session execution, triggers dynamic resolution.

### 4.3 Maintaining: Filesystem Organization and Metadata DB

* **File Storage Layout**:
  Following RFC 6020/7950 naming convention:
  ```text
  ~/.cache/gnmi_client_py/
  ├── schema_cache.db           # SQLite metadata catalog
  └── models/
      ├── ietf-interfaces@2014-05-08.yang
      ├── ietf-interfaces@2018-02-20.yang
      ├── ietf-ip@2014-06-16.yang
      └── openconfig-interfaces@2021-04-06.yang
  ```

* **SQLite Schema Index (`schema_cache.db`)**:
  ```sql
  CREATE TABLE IF NOT EXISTS modules (
      module_name TEXT NOT NULL,
      revision TEXT,
      file_path TEXT NOT NULL,
      sha256 TEXT NOT NULL,
      source TEXT NOT NULL, -- 'bundled', 'local_dir', 'netconf_download'
      PRIMARY KEY (module_name, revision)
  );

  CREATE TABLE IF NOT EXISTS device_profiles (
      device_fingerprint TEXT PRIMARY KEY,
      protocol TEXT NOT NULL,
      capabilities_json TEXT,
      last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );

  CREATE TABLE IF NOT EXISTS device_modules (
      device_fingerprint TEXT,
      module_name TEXT,
      revision TEXT,
      FOREIGN KEY (device_fingerprint) REFERENCES device_profiles(device_fingerprint)
  );
  ```

---

## 5. Protocol Differences: NETCONF vs. gNMI

| Feature | NETCONF (RFC 6241, RFC 6022, RFC 8525) | gNMI (gRPC Network Management Interface) |
| :--- | :--- | :--- |
| **Initial Capability Exchange** | `<hello>` contains capability URIs including `module` and `revision` parameters. RFC 8525 exposes `ietf-yang-library`. | `CapabilityResponse` contains `supported_models`: list of `ModelData` (`name`, `organization`, `version`). |
| **Dynamic Schema Fetching** | **Native**: RFC 6022 `<get-schema identifier="..." version="..." format="yang"/>` returns the raw YANG file content. | **Not natively defined**: gNMI specification does not define an RPC to download `.yang` files directly from the device. |
| **Missing Schema Fallback** | When a module is missing, call `NetconfClient.get_schema(...)` -> write to cache -> load into `libyang.Context`. | Cross-reference local SQLite database and shared cache. If missing, log actionable error or attempt fallback to a common vendor cache. |
| **Payload Representation** | Native XML elements with XML namespaces (RFC 7950) or JSON (RFC 7951). | JSON / JSON_IETF encoded in protobuf `TypedValue`, or ASCII/Proto bytes. |

---

## 6. Implementation Agent Instructions

Below is the execution plan to be provided to coding agents to implement Phase 4 step-by-step.

### Task 4.1: Core `SchemaManager` & Cache Database
* Create `core/schema_manager.py` implementing the Singleton pattern with `threading.RLock`.
* Create `core/schema_db.py` to wrap SQLite operations for indexing modules by `(name, revision)` and storing device fingerprints.
* Provide methods to create and access isolated `libyang.Context` objects keyed by `context_id`.

### Task 4.2: Update Configuration Dataclasses
* Update `config/model.py` and `config/protocol_options/` to accept:
  * `yang_dirs: List[Path] = field(default_factory=list)`
  * `yang_cache_dir: Path = Path("~/.cache/gnmi_client_py/yang")`
  * `enable_schema_validation: bool = False`
* Ensure CLI parser in `ui/cmd.py` exposes `--yang-dir`, `--yang-cache-dir`, and `--validate-schema`.

### Task 4.3: Dynamic Discovery for NETCONF (`<get-schema>`)
* In `specs/netconf_client.py`:
  * Implement `get_schema(identifier: str, version: Optional[str] = None) -> str`.
  * Wire capability discovery from `<hello>`: extract all `(module, revision)` pairs.
  * Connect `get_schema` to `SchemaManager`: when a model is referenced but absent from `libyang.Context`, invoke `NetconfClient.get_schema()`, write to cache, index in `schema_cache.db`, and load via `ctx.parse_module_str()`.

### Task 4.4: Implement `NetconfValidator` & `GNMIValidator` Integration
* In `specs/netconf_validator.py` and `specs/gnmi_validator.py`:
  * Inherit from `specs/base_validator.py`.
  * Validate target XPaths using `SchemaManager.validate_xpath()`.
  * Verify payload against YANG data tree before sending RPC.
* In `modules/validate.py`:
  * Register the new validators in `ValidatorFactory`.

### Task 4.5: Payload Serialization (Dict -> Namespaced `<edit-config>` XML)
* Add serialization helper in `core/schema_manager.py`:
  * Take a Python dictionary representing configuration hierarchy.
  * Construct a `libyang.DataNode`.
  * Call `dnode.print_mem("xml")` to generate perfectly namespaced XML subtrees for `<edit-config>` operations.
* Test against standard models (`ietf-interfaces` and `openconfig-interfaces`).

---

## 7. Phase 4 Continuation: Remaining Architecture

> **Implementation baseline:** Sections 1 through 5 and the existing Tasks 4.1 through 4.5 are treated as completed. Do not reopen or refactor those areas unless a minimal integration change is required by the tasks below.

The remaining work should make the YANG layer a reusable platform rather than a protocol-specific validation helper.

### 7.1 Target Architecture

```text
                         +----------------------+
                         |    SessionConfig     |
                         |    SchemaConfig      |
                         +----------+-----------+
                                    |
                                    v
                         +----------------------+
                         |    SchemaManager     |
                         |   facade / service   |
                         +----------+-----------+
                                    |
          +-------------------------+-------------------------+
          |                         |                         |
          v                         v                         v
+-------------------+     +--------------------+     +--------------------+
| ContextRegistry   |     | ModelCatalog       |     | SchemaStore        |
| libyang Contexts  |     | schema identities  |     | files + SQLite     |
+-------------------+     +--------------------+     +--------------------+
          |                         |                         |
          +-------------------------+-------------------------+
                                    |
                                    v
                         +----------------------+
                         | SchemaResolver       |
                         | dependency loading   |
                         +----------+-----------+
                                    |
                                    v
                         +----------------------+
                         | SchemaValidator      |
                         | paths + data trees   |
                         +----------+-----------+
                                    |
                                    v
                         +----------------------+
                         | SchemaSerializer     |
                         | XML / JSON / RFC7951 |
                         +----------+-----------+
                                    |
                    +---------------+---------------+
                    |                               |
                    v                               v
             +--------------+                +--------------+
             |  GNMIClient  |                | NetconfClient|
             +--------------+                +--------------+
```

`SchemaManager` should remain the public entry point, but the implementation should not become a single class containing cache management, protocol discovery, validation, and serialization logic.

### 7.2 `SchemaConfig` Should Be a First-Class Configuration Object

Schema settings are cross-protocol concerns, so they should not be buried inside `GNMIOptions` or `NetconfOptions`.

```python
@dataclass(frozen=True)
class SchemaConfig:
    yang_dirs: tuple[Path, ...] = ()
    cache_dir: Path | None = None
    profile: str | None = None
    validate: bool = False
    auto_discovery: bool = True
    auto_fetch: bool = True
```

Recommended ownership:

```text
SessionConfig
├── connection
├── protocol
├── operation
├── execution
└── schema
    ├── yang_dirs
    ├── cache_dir
    ├── profile
    ├── validate
    ├── auto_discovery
    └── auto_fetch
```

Protocol-specific options should contain only protocol-specific behavior.

---

## 8. Schema Identity and `SchemaProfile`

### 8.1 Do Not Use Host/Port as the Schema Identity

A device address identifies a connection target, not a schema.

The same NOS release can expose the same schema on multiple devices, while one device can change its schema because of software upgrade, feature enablement, deviation, or datastore-specific behavior.

Do not make the final schema identity:

```text
sha256(host + port + capabilities)
```

Instead, introduce a canonical schema profile.

### 8.2 Recommended Data Model

```python
@dataclass(frozen=True)
class ModuleRef:
    name: str
    revision: str | None
    namespace: str | None = None


@dataclass(frozen=True)
class DeviationRef:
    name: str
    revision: str | None


@dataclass(frozen=True)
class FeatureRef:
    module: str
    name: str


@dataclass(frozen=True)
class SchemaProfile:
    modules: tuple[ModuleRef, ...]
    submodules: tuple[ModuleRef, ...]
    features: tuple[FeatureRef, ...]
    deviations: tuple[DeviationRef, ...]
    datastore_schemas: tuple[str, ...] = ()
    content_id: str | None = None
```

Before hashing the profile:

1. Sort every collection deterministically.
2. Normalize missing values consistently.
3. Serialize the profile as canonical JSON.
4. Hash the canonical representation.

```python
profile_id = sha256(canonical_json(profile).encode()).hexdigest()
```

### 8.3 `content-id` Semantics

When YANG Library is available, its `content-id` should be used as a **freshness/version signal** for the server's current YANG Library contents.

It should not be treated as a globally meaningful schema identifier across unrelated devices. The cache key should still include the actual module/profile information.

---

## 9. Model Catalog Layer

The client needs one common representation for all sources of schema information.

### 9.1 `ModelCatalog`

```python
@dataclass(frozen=True)
class ModelCatalogEntry:
    name: str
    organization: str | None = None
    version: str | None = None

    # Resolved YANG information, when available.
    modules: tuple[ModuleRef, ...] = ()
    source_uri: str | None = None
    source_type: str | None = None
```

The catalog should answer questions such as:

```python
catalog.resolve_model(name, organization, version)
catalog.modules_for_profile(profile_id)
catalog.find_module(name, revision)
```

### 9.2 gNMI `ModelData` Must Not Be Interpreted as a YANG Revision

gNMI `ModelData` contains `name`, `organization`, and `version`. Those values identify an entry in a model catalog. A model can represent a module, a bundle, an augmentation, or a deviation.

Therefore:

```text
gNMI ModelData
      |
      v
ModelCatalogEntry
      |
      v
module / bundle / augmentation / deviation
      |
      v
SchemaProfile
```

Do **not** implement:

```python
yang_revision = model_data.version
```

unless an external catalog explicitly defines that mapping.

### 9.3 NETCONF Sources

For NETCONF, schema information may come from:

1. `<hello>` capabilities.
2. RFC 8525 YANG Library data.
3. RFC 6022 schema information.
4. RFC 6022 `<get-schema>`.
5. Configured local YANG repositories.

These sources have different information density and should not be merged into one untyped dictionary.

---

## 10. YANG Library Discovery

### 10.1 Preferred NETCONF Discovery Order

```text
NETCONF session established
        |
        v
Read <hello>
        |
        +--> identify YANG Library support
        |
        v
Read YANG Library data
        |
        +--> module sets
        +--> modules
        +--> submodules
        +--> features
        +--> deviations
        +--> datastore schemas
        +--> content-id
        |
        v
Build SchemaProfile
        |
        v
Find or create libyang Context
        |
        v
Resolve missing module content
```

RFC 8525 describes the server's YANG library as information about modules, submodules, features, deviations, datastores, and datastore schemas. Use that richer structure when it is available.

### 10.2 Legacy Capability Parsing

The implementation may parse `module` and `revision` information from NETCONF capability URIs for compatibility, but this should be treated as a reduced schema description.

The complete profile should prefer YANG Library data when available.

### 10.3 Schema Content Retrieval

Schema metadata and schema content are separate concepts:

```text
Schema metadata
    -> "ietf-interfaces", revision "2018-02-20"

Schema content
    -> actual ietf-interfaces YANG source
```

The metadata is enough to describe a profile, but not enough to build a libyang context unless the corresponding YANG source is already available locally.

---

## 11. Recursive Schema Resolution

### 11.1 Missing Modules Must Be Resolved as a Dependency Graph

Loading one `.yang` module can require:

```text
ietf-interfaces
  ├── import ietf-yang-types
  ├── import ietf-inet-types
  └── include ietf-interfaces-sub
```

Therefore the resolver must operate on the complete dependency closure.

```text
required module
      |
      v
parse
      |
      +--> missing import ------> resolve import
      |
      +--> missing include -----> resolve submodule
      |
      +--> missing deviation ---> resolve deviation
      |
      v
all dependencies available
      |
      v
load into Context
```

### 11.2 Resolver Contract

```python
class SchemaResolver:
    def ensure_module(
        self,
        profile_id: str,
        module_name: str,
        revision: str | None = None,
    ) -> None:
        ...

    def ensure_profile(
        self,
        profile: SchemaProfile,
    ) -> None:
        ...
```

The resolver should try sources in deterministic order:

```text
1. active libyang Context
2. configured yang_dirs
3. local schema cache
4. protocol-specific discovery
5. protocol-specific download
6. fail with actionable error
```

Do not silently use a different revision merely because it exists locally.

---

## 12. Cache Database Redesign

The initial `(module_name, revision)` table is not enough to represent schema identity and provenance.

### 12.1 Recommended Logical Tables

```sql
CREATE TABLE IF NOT EXISTS schema_modules (
    module_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    revision TEXT,
    namespace TEXT,
    kind TEXT NOT NULL,              -- module | submodule
    sha256 TEXT NOT NULL,
    file_path TEXT NOT NULL,
    source TEXT NOT NULL,            -- bundled | local_dir | netconf_get_schema | other
    source_uri TEXT,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(name, revision, sha256)
);

CREATE TABLE IF NOT EXISTS schema_features (
    module_id INTEGER NOT NULL,
    feature_name TEXT NOT NULL,
    PRIMARY KEY(module_id, feature_name),
    FOREIGN KEY(module_id) REFERENCES schema_modules(module_id)
);

CREATE TABLE IF NOT EXISTS schema_deviations (
    profile_id TEXT NOT NULL,
    module_name TEXT NOT NULL,
    revision TEXT,
    PRIMARY KEY(profile_id, module_name, revision)
);

CREATE TABLE IF NOT EXISTS schema_profiles (
    profile_id TEXT PRIMARY KEY,
    content_id TEXT,
    canonical_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_modules (
    profile_id TEXT NOT NULL,
    module_id INTEGER NOT NULL,
    PRIMARY KEY(profile_id, module_id),
    FOREIGN KEY(profile_id) REFERENCES schema_profiles(profile_id),
    FOREIGN KEY(module_id) REFERENCES schema_modules(module_id)
);

CREATE TABLE IF NOT EXISTS profile_datastores (
    profile_id TEXT NOT NULL,
    datastore_name TEXT NOT NULL,
    schema_name TEXT,
    PRIMARY KEY(profile_id, datastore_name),
    FOREIGN KEY(profile_id) REFERENCES schema_profiles(profile_id)
);

CREATE TABLE IF NOT EXISTS device_bindings (
    device_key TEXT PRIMARY KEY,
    protocol TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    FOREIGN KEY(profile_id) REFERENCES schema_profiles(profile_id)
);
```

### 12.2 Cache Rules

A downloaded schema must be committed only after:

1. The content is written to a temporary file.
2. SHA-256 is calculated.
3. libyang successfully parses the module.
4. The final file is atomically renamed.
5. SQLite metadata is committed in one transaction.

This prevents a partially written YANG file from poisoning the cache.

### 12.3 Cache Lookup Priority

```text
exact name + exact revision + exact checksum
        >
exact name + exact revision
        >
name-only lookup
        >
anything else: reject
```

A name-only match should never automatically satisfy a request when the requested revision is known.

---

## 13. `ContextRegistry` and libyang Lifecycle

### 13.1 Context Lifetime

A `libyang.Context` should represent a stable schema profile.

```text
SchemaProfile
      |
      v
ContextRegistry
      |
      +--> profile A -> Context A
      |
      +--> profile B -> Context B
      |
      +--> profile C -> Context C
```

A context should not be rebuilt for every RPC.

### 13.2 Context Mutability

Treat a context as a managed resource.

Recommended rules:

* Build the context before normal RPC execution.
* Load all required schemas before validating payloads.
* Do not mutate a context during concurrent validation unless the implementation provides the necessary synchronization.
* When the schema profile changes materially, create a new context instead of attempting an in-place replacement of conflicting modules.

### 13.3 Suggested Wrapper

Avoid exposing raw `libyang.Context` objects to unrelated application code.

```python
@dataclass
class SchemaContext:
    profile_id: str
    yang: libyang.Context
```

Only the schema subsystem should need the raw libyang object.

---

## 14. Validation Boundary

### 14.1 Validation Is Operation-Aware

The validator should not only answer "is this YANG-valid?"

It should answer:

```text
Is this path valid for this operation?
Is this payload valid for this operation?
Is this node writable for this protocol operation?
Are required keys present?
Are configured features enabled?
Do deviations modify the expected node?
```

### 14.2 Validation Flow

```text
Semantic Operation
      |
      v
Resolve Schema Profile
      |
      v
Resolve PathSelector
      |
      +--> path syntax invalid ---------> error
      |
      +--> schema node not found -------> error
      |
      v
Operation-specific checks
      |
      +--> read-only node for Set ------> error
      +--> missing list key ------------> error
      +--> unsupported feature ---------> error
      |
      v
Build libyang Data Tree
      |
      v
Validate data tree
      |
      v
Serialize for protocol
```

### 14.3 Get vs. Set

Do not reuse identical validation rules for every operation.

For example:

```text
GetOperation
  -> may target config and operational/state data

SetOperation
  -> must target data that is writable according to the protocol/schema
```

The semantic operation layer should express intent, while the protocol validator applies protocol-specific constraints.

---

## 15. Validation API

Instead of exposing many low-level calls to clients, define an operation-oriented interface.

```python
class SchemaValidator:
    def validate_path(
        self,
        path: PathSelector,
        *,
        profile_id: str,
        operation: OperationConfig,
    ) -> None:
        ...

    def validate_payload(
        self,
        payload: object,
        *,
        profile_id: str,
        operation: OperationConfig,
    ) -> None:
        ...
```

Then protocol adapters can use a common implementation:

```text
SetOperation
    |
    v
BaseClient.execute_set()
    |
    +--> SchemaValidator
    |
    +--> Protocol Serializer
    |
    v
GNMIClient / NetconfClient
```

The concrete validators may still exist:

```text
BaseValidator
├── GNMIValidator
└── NetconfValidator
```

but their primary difference should be protocol behavior, not duplicated YANG parsing logic.

---

## 16. Data Tree Construction and Serialization

### 16.1 Separate Construction from Serialization

Do not put dict-to-XML logic directly in `SchemaManager`.

Use:

```text
Change
  |
  v
DataBuilder
  |
  v
libyang DataNode
  |
  +--> validate
  |
  +--> XML serializer
  |
  +--> JSON serializer
```

### 16.2 NETCONF Serialization

For NETCONF:

```text
Change
  -> libyang DataNode
  -> XML
  -> attach NETCONF operation metadata
  -> place under <config>
  -> edit-config RPC
```

Important distinction:

```text
YANG data:
    <interface>...</interface>

NETCONF protocol wrapper:
    <config>...</config>

NETCONF operation metadata:
    nc:operation="merge"
```

`nc:operation` is NETCONF protocol metadata and should not be modeled as a normal YANG data node.

### 16.3 gNMI Serialization

For gNMI:

```text
Change
  -> libyang DataNode
  -> RFC 7951 JSON
  -> TypedValue.json_ietf_val
  -> SetRequest.Update / Replace
```

The serializer should own JSON encoding details. The gNMI client should own protobuf message construction.

### 16.4 Serializer Contract

```python
class SchemaSerializer:
    def to_xml(self, data_node: object) -> str:
        ...

    def to_json_ietf(self, data_node: object) -> str:
        ...
```

Protocol-specific wrappers should be outside this serializer.

---

## 17. Dynamic Discovery Runtime Flow

### 17.1 NETCONF

```text
connect
  |
  v
<hello>
  |
  v
YANG Library / capability discovery
  |
  v
SchemaProfile
  |
  v
ContextRegistry
  |
  +--> profile found --------> reuse Context
  |
  +--> profile missing ------> resolve dependencies
                                  |
                                  +--> local repository
                                  +--> cache
                                  +--> <get-schema>
                                  |
                                  v
                               build Context
  |
  v
validate operation
  |
  v
serialize XML
  |
  v
send RPC
```

RFC 6022 defines `<get-schema>` specifically for retrieving schema content from a NETCONF server. RFC 8525 provides the richer library metadata needed to describe the server's module and datastore schema state.

### 17.2 gNMI

```text
connect
  |
  v
Capabilities
  |
  v
ModelCatalog
  |
  +--> local catalog resolves ModelData
  |
  +--> shared cache resolves ModelData
  |
  +--> unresolved model --------> actionable error
  |
  v
SchemaProfile
  |
  v
ContextRegistry
  |
  v
validate operation
  |
  v
serialize JSON_IETF
  |
  v
send gNMI RPC
```

Do not invent a schema-download RPC for gNMI. Model discovery and schema-content retrieval remain separate concerns.

---

## 18. Error Model and Failure Semantics

The schema platform should expose typed errors so callers can decide whether to retry, reconfigure, or abort.

```python
class SchemaError(Exception):
    pass


class SchemaNotFoundError(SchemaError):
    pass


class SchemaDependencyError(SchemaError):
    pass


class SchemaProfileConflictError(SchemaError):
    pass


class SchemaValidationError(SchemaError):
    pass


class InvalidSchemaPathError(SchemaValidationError):
    pass


class SchemaSerializationError(SchemaError):
    pass


class SchemaCacheError(SchemaError):
    pass


class SchemaDiscoveryError(SchemaError):
    pass
```

Recommended behavior:

```text
Missing schema + auto_fetch=True
    -> try discovery

Missing schema + auto_fetch=False
    -> fail immediately

Validation disabled
    -> skip YANG validation, preserve existing syntactic validation

Validation enabled + validation failure
    -> do not send the RPC

Cache corruption
    -> discard invalid cache entry
    -> attempt re-fetch when permitted
```

There should be no implicit "retry without validation" fallback after a schema-validation failure.

---

## 19. Logging and Observability

Every schema decision should be diagnosable.

Recommended structured fields:

```text
device
protocol
profile_id
content_id
module
revision
source
cache_hit
validation_enabled
operation
```

Example:

```text
schema_resolve
  device=router1
  profile_id=8a5e...
  module=ietf-interfaces
  revision=2018-02-20
  source=netconf_get_schema
  cache_hit=false
```

For validation failures, include:

```text
operation
path
schema_node
error_type
yang_error_message
```

Do not log complete credentials, secrets, or full sensitive payloads merely for schema debugging.

---

## 20. Test Plan

### 20.1 Schema Profile Tests

Test:

* deterministic profile hashing
* order-independent module lists
* feature changes produce different profiles
* deviation changes produce different profiles
* datastore schema changes produce different profiles
* identical profiles produce identical IDs

### 20.2 Cache Tests

Test:

* insert and lookup by exact module/revision
* submodule storage
* duplicate content handling
* cache corruption recovery
* atomic writes
* SQLite transaction rollback
* profile-to-module relationships
* multiple revisions of the same module

### 20.3 libyang Context Tests

Test:

```text
valid module set -> context creation succeeds
missing import -> SchemaDependencyError
conflicting revision -> ProfileConflict / context build failure
feature disabled -> expected schema behavior
deviation applied -> expected effective schema
```

### 20.4 Path Validation Tests

Positive cases:

```text
/ietf-interfaces:interfaces
/ietf-interfaces:interfaces/interface
```

Negative cases:

```text
unknown module
unknown node
invalid list predicate
missing list key
invalid path syntax
```

### 20.5 Payload Validation Tests

Include:

```text
wrong leaf type
invalid enum
out-of-range integer
invalid pattern
invalid identityref
invalid leafref
missing mandatory node
missing list key
read-only node in Set
```

### 20.6 Protocol Integration Tests

NETCONF:

```text
hello -> YANG Library -> profile -> get-schema -> cache -> context
```

gNMI:

```text
Capabilities -> ModelCatalog -> profile -> context
```

For gNMI, explicitly test the unresolved-model case and verify that the client returns an actionable error instead of pretending that `ModelData.version` is a YANG revision.

### 20.7 Golden Serialization Tests

Use fixed expected outputs for:

* NETCONF namespaced XML
* NETCONF edit operation metadata
* gNMI RFC 7951 JSON
* nested containers
* keyed lists
* augmentations
* identityrefs and leafrefs
* multiple top-level modules

---

## 21. Agent Execution Order After Task 4.5

### Task 4.6: Introduce `SchemaConfig` and `SchemaProfile`

* Add `config/schema.py` containing schema-specific immutable dataclasses.
* Add `SchemaProfile`, `ModuleRef`, `FeatureRef`, and `DeviationRef`.
* Add deterministic canonicalization and profile hashing.
* Keep completed configuration and operation models intact except for the minimum `SessionConfig.schema` integration.

**Acceptance criteria**

```text
same logical profile -> same profile_id
different revision/features/deviations -> different profile_id
host/port change alone -> profile_id unchanged
```

### Task 4.7: Add `ModelCatalog`

* Create a protocol-neutral model catalog.
* Map NETCONF-discovered modules and gNMI `ModelData` into catalog entries.
* Do not equate gNMI model version with YANG revision.
* Add catalog resolution APIs and unit tests.

### Task 4.8: Refactor Cache Metadata

* Replace the module-only cache index with normalized profile/module metadata.
* Store module, submodule, feature, deviation, datastore schema, and source metadata.
* Add checksum verification.
* Implement transactional metadata updates.

### Task 4.9: Implement Recursive `SchemaResolver`

* Resolve imports and includes recursively.
* Search local directories and cache before network retrieval.
* Retrieve NETCONF schema content through the existing client when permitted.
* Never silently substitute a different requested revision.

### Task 4.10: Introduce `ContextRegistry`

* Manage one libyang context per effective schema profile.
* Reuse contexts for identical profiles.
* Prevent unsafe concurrent mutation of the same data/context object.
* Expose a small wrapper instead of leaking raw libyang objects into the rest of the application.

### Task 4.11: Integrate Operation-Aware Validation

* Add common schema validation methods.
* Connect path and payload validation to `BaseClient.execute_*()` flow.
* Keep protocol-specific policy in `GNMIValidator` and `NetconfValidator`.
* Ensure validation occurs before wire serialization and before RPC transmission.

### Task 4.12: Split Data Construction and Serialization

Create:

```text
core/schema_data.py
core/schema_serializer.py
```

Responsibilities:

```text
schema_data.py
    Change -> libyang DataNode

schema_serializer.py
    DataNode -> XML / JSON_IETF
```

Do not place NETCONF `<config>` or `nc:operation` behavior inside the generic YANG serializer.

### Task 4.13: NETCONF YANG Library + `<get-schema>`

* Parse YANG Library metadata.
* Construct the effective `SchemaProfile`.
* Use `content-id` as a freshness signal.
* Resolve missing schema source through `<get-schema>` when supported.
* Persist successful retrievals into the cache.

### Task 4.14: gNMI Capability / Model Catalog Integration

* Parse `CapabilityResponse.supported_models`.
* Resolve each model through `ModelCatalog`.
* Build the effective schema profile when the required model source is known.
* Return `SchemaDiscoveryError` when model metadata cannot be mapped to actual YANG content.

### Task 4.15: End-to-End Protocol Integration

Implement:

```text
GNMIClient.execute_get()
GNMIClient.execute_set()
NetconfClient.execute_get()
NetconfClient.execute_set()
```

with the following invariant:

```text
semantic operation
      |
      v
schema resolution
      |
      v
schema validation
      |
      v
protocol serialization
      |
      v
wire RPC
```

No worker should need to know how YANG modules are loaded.

### Task 4.16: Test and Documentation Completion

* Add unit, integration, negative, cache, discovery, and golden serialization tests.
* Document cache locations and lifecycle.
* Document offline mode.
* Document schema discovery requirements per protocol.
* Add examples for local YANG directories and automatic NETCONF discovery.
* Document known limitations for gNMI model-catalog resolution.

---

## 22. Recommended Package Layout

```text
gnmi_client_py/
├── config/
│   ├── model.py
│   ├── operations.py
│   ├── selectors.py
│   ├── delivery.py
│   ├── schema.py
│   └── protocol_options/
│       ├── gnmi.py
│       └── netconf.py
│
├── core/
│   ├── schema_manager.py
│   ├── schema_context.py
│   ├── schema_db.py
│   ├── schema_catalog.py
│   ├── schema_resolver.py
│   ├── schema_validator.py
│   ├── schema_data.py
│   └── schema_serializer.py
│
├── specs/
│   ├── base_client.py
│   ├── gnmi_client.py
│   ├── netconf_client.py
│   ├── base_validator.py
│   ├── gnmi_validator.py
│   └── netconf_validator.py
│
├── modules/
│   └── validate.py
│
└── tests/
    ├── test_schema_profile.py
    ├── test_schema_cache.py
    ├── test_schema_catalog.py
    ├── test_schema_resolver.py
    ├── test_schema_validator.py
    ├── test_schema_serializer.py
    ├── test_gnmi_schema_discovery.py
    └── test_netconf_schema_discovery.py
```

The exact filenames may be adapted to the repository's existing conventions. The architectural boundary is more important than the literal names.

---

## 23. Definition of Done

Phase 4 should be considered complete when all of the following are true:

```text
[ ] A semantic operation can request schema validation without knowing libyang APIs.
[ ] The same effective schema profile reuses an existing libyang Context.
[ ] Different schema profiles do not share conflicting module revisions.
[ ] NETCONF can discover schema metadata and retrieve missing YANG source when supported.
[ ] gNMI ModelData can be resolved through a model catalog without assuming version == YANG revision.
[ ] Imports/includes are resolved recursively.
[ ] Cache entries are checksum-verified and atomically stored.
[ ] Paths are validated against the effective schema.
[ ] Payloads are validated against the effective schema before transmission.
[ ] NETCONF XML is namespaced automatically.
[ ] NETCONF protocol metadata remains outside the generic YANG data tree.
[ ] gNMI JSON_IETF output is generated from the validated data tree.
[ ] Schema failures prevent an unsafe RPC from being transmitted when validation is enabled.
[ ] Tests cover positive, negative, cache, discovery, and serialization cases.
```

---

## 24. Normative References

The implementation should use these standards as the primary protocol and schema references:

* **RFC 7950**: The YANG 1.1 Data Modeling Language.
* **RFC 7951**: JSON Encoding of Data Modeled with YANG.
* **RFC 6241**: NETCONF Protocol.
* **RFC 6022**: YANG Module for NETCONF Monitoring, including `<get-schema>`.
* **RFC 8525**: YANG Library.
* **OpenConfig gNMI Specification**: `Capabilities`, `ModelData`, `use_models`, and `TypedValue` behavior.
* **libyang 2.x documentation**: schema parsing, context management, data-tree validation, and XML/JSON printing.

These references should be treated as normative for protocol behavior. Repository-specific abstractions such as `SchemaManager`, `SchemaProfile`, and `ModelCatalog` are implementation constructs layered above those standards.
