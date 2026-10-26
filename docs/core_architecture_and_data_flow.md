Data and Data flow
====

Data
----

In `config` module, each protocl data defined.
* `config` defines two kinds of classes: Semantic operations and actual protocol operations.

### Semantic Operations

Semantic operations defines 'common' operations for each supported northbound protocols.

### Protocol Operations

Protocol operations defines 'specific' operations per RPCs.


### Relation table

Semantic operation | gNMI | NETCONF 
-------------------|------|--------
`CapabilitesOperation` | `Capabilities` | `<hello>`/capabilities
`GetOperation` | `Get` | `<get>`/`<get-config>`
`SetOperration` | `Set` | `<edit-config>` + optional commit
`GetSchemaOperration` | requires Extensions | `<get-schema>`
`SubscribeOperation` | `Subscribe` | NETCONF notification/subscription(RFC 5277)


Data flow
---------

```text
[ CLI Input / YAML Config ] (ParsedConfig)
            │
            ▼
            │
       [ ui/cmd.py ]  ──────────> Creates [ SessionConfig ] (SessionConfig in {ConnectionConfig, ExecutionConfig, OperationConfig})[for each operation and defined RPCs]
            │
            ▼
            │
       [ ui/cmd.py ]  ──────────> Construct `DeviceOperationService` and `SubscriptionService` (see `Application Services Layer`)
            │
            ▼
            │
     [ ui/runner.py ]  ──────────> Creates one of runners(`CLIApplicationRunner`, `CLIStreamRunner`) using `DTO`
        ├── CLIApplicationRunner  ───> Spawns Worker Threads [ GetWorker / SetWorker / CapabilitiesWorker / SequentialWorker ]
        └── CLIStreamRunner       ───> Spawns Worker Threads [ SubscribeSession ]
                                                │
                                                ▼
                                        [ ClientFactory ]
                                        ├── GNMIClient (BaseClient)    ──> HTTP/2 (gRPC)
                                        └── NetconfClient (BaseClient) ──> SSH / TLS (ncclient)
                                                │
                                                ▼
                                        [ OutputHandler ]
                                        └── Formatter (GNMIFormatter / NETCONFFormatter)
```

Architecture Decoupling
=======================

Application Services Layer
--------


### DTO(Data Transfer Object)s

This is a kind of presentation layer that gives means of forwarding information between upper and lower layer.
* By defining DTO(Data Transfer Object)s, this layer can express one or more northbound protocols.
* It has similar information which defined at `config`, but used between layers.

Upper layer containes defined interfaces — Command-line arguments, YAML, and so on.

Lower layer implements actual northbound-protocol operations.
* Defined `Workers` proceed desired operation based of given information from Application Services Layer.

#### Supported DTOs

Name | Represents 
-------------------|------
`DeliveryModeDTO` | Methods of streaming 
`TelemetryPolicyDTO` | Configuration of streaming 
`MutationChangeDTO` | How to set data 
`AdhocEndpointDTO` | `ParsedConfig` from `cmd.py`
`OperationStepDTO` | RPC Operations 
`ExecutionRequestDTO` | Forwarding rules 
`StepResultDTO` | Operation output  
`ExecutionResultDTO` | Result of an operation 
`StreamEventDTO` | Streaming(or event) output 

### Services

Application Services Layer has couple of services for operating protoocols. All services return `ExecutionResultDTO` as result of protocol operation.

#### Supported Services

Name | Description | Used for
-------------------|------|---------------
`DeviceOperationService` | Application service executing unary requests against network devices. | "Unary" Operations
`SubscriptionService` | Stateful application service managing streaming telemetry subscriptions.  | "Stream" Operations
`DeviceRegistryService` |Thread-safe registry for known network device profiles. | The runner of services described above
`SchemaCatalogService` | Application-level facade bridging to the core YANG schema engine and SQLite database. | Schema management



Workers
-------

Workers (BaseUnaryWorker, SubscribeSession) are protocol-agnostic. They use defined classes in `config` directory.

### SequentialWorker

A worker may hold one or more RPCs which represented by `SessionConfig`. To serve such operation, `SequentialWorker` handles operations via one-shot instruction(YAML).
* NOTE: It only cares Unary RPCs.

Modules
-------

Security credentials and options are encapsulated inside `SecurityProfile` / `SecurityModule` in `modules/security.py`.
* But it's not yet fully implemented - Need to test

Outputs
-------

`OutputHandler` will be created for each information decribed in `OutputType`, and `OutputFormat`.
* They are created ONLY ONCE and for all reply will send a message to handler.


`OutputHandler` creates `Formatter` classes for each given protocols.

```text

               ExecutionResultDTO / StreamEvent
                                │
                  message(Dict)={protocol:, ...}
                                │
                                ▼
                        [ OutputHandler ]
                                │
                     OutputFormat, OutputType
                                │
                                ▼
                 (GNMIFormatter, NETCONFFormatter)
                                │
        ┌─────────────────────────────────────────────────┐
        │                       │                         │
  OutputType(STD)         OutputType(File)         OutputType(SYSLOG)
        │                       │                         │
        ▼                       ▼                         ▼
   prints to terminal     prints to file           creates SYSLOG Client(IP, PORT)
                                                          │
                                                          ▼
                                                    push data into server

```

Clients
-------


#### gNMI Client

Fully functional across `capability`, `get`, `set`, and `subscribe` (stream, once, poll).
- Extensions are not yet supported.

**plans to be supported extensions**

1. CLI execution(SetRequest)
2. commit(SetRequest)


#### NETCONF Client

Phase 1:
* Implemented in `specs/client.py`.
* Supports `<capabilities>` (client.capabilities()).
* Supports `<get>`, `<get-config>`, and RFC 6022 `<get-schema>`.
* Added `RPCError` handling to capture router `<rpc-error>` replies and parse the resulting `lxml` nodes cleanly into JSON dictionaries using `NETCONFFormatter` in `modules/formatter.py`.
* `ManagerFactory` routes `get`, `get-config`, and `get-schema` to `GetWorker`.

Phase 2:
* Refactor protocol configuration.
* Supports NETCONF `<edit-config>` RPC.

Phase 3:
* Support transactional Operations defined at RFC 6022.
* Support sequential Operations(YAML)

Phase 4:
* Define managing/handling schema information at `./core` directory
* Support request validation

Phase 5:
* Support Netconf Event notification(RFC5277)