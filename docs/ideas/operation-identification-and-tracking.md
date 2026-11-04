# Operation Identification and Tracking (`op_id`)

## Problem Statement
How might we unambiguously correlate each step in a multi-operation execution pipeline with its corresponding log entries and structured output results, while keeping YAML authoring ergonomic, natural, and zero-overhead when explicit names are omitted?

## Recommended Direction
Implement a dual explicit and implicit Operation ID system across the presentation, application, and worker layers:

1. **Explicit Identification**:
   Users can assign meaningful names to steps using either top-level sibling syntax (`- id: update_valid`) or nested mapping syntax (`- edit-config: { id: update_valid }`).
2. **Implicit Fallback**:
   When `id` is omitted, the engine automatically assigns a deterministic, human-readable ordinal tag matching the file sequence:
   `<proto>-<op>-<index:02d>` (e.g., `netconf-capability-01`, `netconf-get-config-02`, `netconf-edit-config-03`, `gnmi-set-01`).
3. **Multi-Sink Traceability**:
   The generated or assigned `op_id` flows end-to-end through `OperationStepDTO` -> `OperationConfig` -> `SequentialWorker` -> `StepResultDTO`, surfacing consistently in console/file logs and structured JSON/Text output dictionaries.

## Key Assumptions to Validate
- [ ] **Assumption 1 (YAML Parser Flexibility)**: PyYAML parses both single-key dictionaries (`- edit-config: {...}`) and multi-key sibling dictionaries (`- id: ...`, `edit-config: ...`) without order ambiguity or parsing collisions.
- [ ] **Assumption 2 (Sequential Worker Scoping)**: Index counting resets per target in multi-target YAML pipelines, ensuring Target A and Target B both have predictable `01..N` step sequences without counter bleed.
- [ ] **Assumption 3 (Downstream Consumer Compatibility)**: Adding an optional `"op_id"` key to the JSON output dictionaries in `CLIApplicationRunner` does not break any existing test assertions or downstream output parsers.

## MVP Scope
### In Scope
- **Base Domain Config (`config/operations.py`)**: Add `op_id: Optional[str] = None` to `OperationConfig`.
- **Application DTOs (`application/dto.py`)**: Add `op_id: Optional[str] = None` to `OperationStepDTO` and `StepResultDTO`.
- **Application Service (`application/operations.py`)**: Map `op_id` across `OperationStepDTO`, `OperationConfig`, and `StepResultDTO`.
- **YAML Ingestion (`ui/cmd.py`)**:
  - Parse explicit `id`, `name`, or `op_id` from either top-level step or nested operation dictionary.
  - Generate implicit ordinal IDs (`f"{protocol}-{op_type}-{index:02d}"`) for sequential operations when explicit ID is absent.
- **Worker Logs (`managers/unary_worker.py`)**: Prepend `[<op_id>]` to execution and error log messages in `SequentialWorker`.
- **Presentation Routing (`ui/runner.py`)**: Include `"op_id"` in structured result items written by `CLIApplicationRunner.write_results()`.

### Out of Scope (Not Doing & Why)
- **NETCONF Wire `message-id` Override**: `ncclient` manages its own internal integer message IDs for SSH RPC framing and reply demuxing; tampering with wire IDs risks desyncing `ncclient` channel listeners without providing tangible user benefit.
- **Distributed Tracing / OpenTelemetry Spans**: Unnecessary complexity for local network automation runs. The simple ordinal tag solves 100% of the correlation pain without requiring external tracing collectors.
- **Random Hex Hashes (UUID/MD5)**: Random hashes like `netconf-edit-a8f3b2` make it harder to match step 7 in a YAML file than a simple zero-padded index `netconf-edit-07`.

## Open Questions
- None. Requirements validated and confirmed via user interview.

