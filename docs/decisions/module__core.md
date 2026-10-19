
## Architecture Decision Records (ADRs) for `core` module

### ADR-001: Profile-Scoped `libyang.Context` Isolation vs. Single Global Context

- **Status:** Accepted
- **Context:** Multi-vendor network automation sessions simultaneously target heterogeneous NOS implementations (e.g., Nokia SRLinux `openconfig-interfaces@2024-04-04`, OcNOS `ipi-interface@2025-02-12`, Cisco IOS-XR, Juniper Junos). Different devices load incompatible revisions, vendor-specific deviations, and conflicting augmentations of the same base YANG modules.
- **Decision:** Keep [`SchemaManager`](../../core/schema_manager.py) as a process-wide Singleton, but isolate compiled schemas inside separate [`SchemaContext`](../../core/schema_context.py) wrappers managed by [`ContextRegistry`](../../core/schema_context.py) and keyed by `profile_id`.
- **Alternatives Considered:**
  - *Single global `libyang.Context`*: Rejected because `libyang` forbids conflicting module revisions or target-specific deviation trees within a single C context.
  - *Per-connection `libyang.Context`*: Rejected because compiling 80+ OpenConfig YANG modules per connection takes several seconds and duplicates memory across targets sharing the same NOS version.
- **Consequences:**
  - [`SchemaProfile.profile_id`](../../config/schema.py) computes a deterministic SHA-256 hash over sorted modules, submodules, features, deviations, and datastores—excluding `host:port`.
  - Multiple devices running the same NOS image share one compiled `SchemaContext` in memory while remaining isolated from other vendor profiles.

---

### ADR-002: Hybrid SQLite Metadata Index + Atomic Filesystem Cache (`SchemaDB`)

- **Status:** Accepted
- **Context:** Dynamically fetched YANG schemas (e.g., via NETCONF `<get-schema>`) must persist across CLI runs, prevent corrupt partial writes during concurrent worker execution, and remain directly loadable by the `libyang` C library.
- **Decision:** Store raw `.yang` files on disk under `~/.cache/gnmi_client_py/models/<name>@<revision>.yang` using temporary file staging + `os.replace()`, paired with a relational SQLite index (`~/.cache/gnmi_client_py/schema_cache.db`) in [`SchemaDB`](../../core/schema_db.py).
- **Alternatives Considered:**
  - *SQLite BLOB-only storage*: Rejected because `libyang`'s internal C `import`/`include` resolver searches filesystem directories (`ly_ctx_set_searchdir`) when compiling transitive dependencies.
  - *Flat directory without database*: Rejected because tracking SHA-256 file integrity, module features, profile-to-module associations, and `device_key -> profile_id` bindings requires structured queries.
- **Consequences:**
  - Every `find_module()` call verifies the on-disk file's SHA-256 checksum against the SQLite record. If a file is tampered with or truncated on disk, `_verify_and_return_path()` automatically unlinks the corrupt file and purges its SQLite row, allowing transparent re-fetching.

---

### ADR-003: Decoupling gNMI `ModelData.version` from YANG Revision (`ModelCatalog`)

- **Status:** Accepted
- **Context:** In gNMI `CapabilityResponse`, `ModelData.version` often carries a semantic version (e.g., `"0.2.2"` or `"2.4.1"` via `openconfig-extensions:openconfig-version`) rather than a standard YANG `revision` date (`"YYYY-MM-DD"`). Passing a semantic version string as a revision to `libyang` or NETCONF `<get-schema>` causes lookup failures.
- **Decision:** Introduce [`ModelCatalog`](../../core/schema_catalog.py) to map `(name, organization, version)` triples to explicit `Tuple[ModuleRef, ...]` definitions with valid YANG revision dates.
- **Consequences:**
  - `build_profile_from_gnmi_models()` never equates `ModelData.version` with `ModuleRef.revision`. In `strict=True` mode, unmapped models raise `SchemaDiscoveryError`; in `strict=False` mode, they fall back to `ModuleRef(name=name, revision=None)`.

---

### ADR-004: Separating `DataBuilder` from `SchemaSerializer`

- **Status:** Accepted
- **Context:** Both gNMI `Set` and NETCONF `<edit-config>` originate from protocol-neutral [`Change`](../../config/operations.py) objects (`path`, `value`, `operation`), yet require different wire encodings (RFC 7951 JSON vs. namespaced XML with `xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0"` attributes).
- **Decision:** Split in-memory `libyang.DataNode` construction into [`DataBuilder`](../../core/schema_data.py) and wire encoding into [`SchemaSerializer`](../../core/schema_serializer.py).
- **Consequences:**
  - `DataBuilder` validates and merges multi-path changes inside `libyang`'s data tree without protocol pollution.
  - `SchemaSerializer` renders the validated tree to either RFC 7951 JSON (`to_json_ietf`) or NETCONF XML (`serialize_netconf_edit_config`).

---