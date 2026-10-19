# NETCONF Event Notifications Specification

**Target Feature**: RFC 5277 (`<create-subscription>` & `<notification>`) Streaming Integration  
**Related Milestones & Specs**:
* [`docs/milestone/milestone_netconf.md`](./milestone/milestone_netconf.md) (Phase 4: Telemetry Streaming)
* [`docs/RPC_structure/fully_support_subscribe_services.md`](./RPC_structure/fully_support_subscribe_services.md)
* [`docs/RPC_structure/protocol_neutral_delivery_mode_spec.md`](./RPC_structure/protocol_neutral_delivery_mode_spec.md)
* [`docs/core_architecture_and_data_flow.md`](./core_architecture_and_data_flow.md)

**Core Modules Affected**:
* `config/protocol_options/netconf.py` (Extended options & RFC 5277 validation)
* `specs/netconf_client.py` (Implements `execute_subscribe` adhering to `BaseClient`)
* `specs/netconf_validator.py` (Validation for filters and timestamp ranges)
* `modules/formatter.py` (Formatting NETCONF `StreamEvent` instances)
* `ui/cmd.py` (CLI & YAML subscription configuration mapping)

---

## 1. Executive Summary & Architectural Alignment

To achieve streaming parity between gNMI `Subscribe` and NETCONF Event Notifications (RFC 5277), `NetconfClient` integrates directly into the existing protocol-neutral streaming engine managed by `managers/subscribe_session.py`.

### 1.1 Strict Architectural Guardrails
1. **Zero Modifications to `SubscribeSession`**:
   `managers/subscribe_session.py` is already decoupled from transport specifics. It delegates streaming execution via:
   ```python
   stream = client.execute_subscribe(
       operation=self.operation,
       context=self.stream_context,
   )
   ```
   `NetconfClient` implements this exact interface and yields standard `specs/stream_types.StreamEvent` instances.
2. **Unified Event Abstraction (`StreamEvent`)**:
   Do **not** introduce redundant data classes (such as `NormalizedNotification`). All telemetry notifications are wrapped into `StreamEvent(protocol="netconf", timestamp=..., raw_payload=..., data=...)`.
3. **Polymorphic Protocol Options (`NetconfOptions`)**:
   RFC 5277 parameters (`stream_name`, `start_time`, `stop_time`, `poll_timeout`) are incorporated directly into `config/protocol_options/netconf.NetconfOptions`, inheriting from `BaseProtocolOptions`.
4. **Dedicated Transport Lifecycle**:
   RFC 5277 specifies that once `<create-subscription>` succeeds, the session enters notification-only mode and cannot service standard RPCs unless `:interleave:1.0` is supported. `SubscriptionManager` automatically allocates a dedicated `SubscribeSession` and client connection per stream, avoiding cross-worker session pollution.

---

## 2. Protocol & RFC 5277 Mechanics

```
    Client (NetconfClient)                             Server (NETCONF Device)
              │                                                   │
              │  1. Check capability :notification:1.0            │
              │ ────────────────────────────────────────────────► │
              │                                                   │
              │  2. <create-subscription>                         │
              │     stream, filter, startTime, stopTime           │
              │ ────────────────────────────────────────────────► │
              │                                                   │
              │  3. <rpc-reply><ok/></rpc-reply>                  │
              │ ◄──────────────────────────────────────────────── │
              │                                                   │
              │  4. <notification> (<eventTime>, body)            │
              │ ◄──────────────────────────────────────────────── │
              │     [StreamEvent yielded to SubscribeSession]     │
              │                                                   │
              │  5. (Bounded Replay) <replayComplete/>            │
              │ ◄──────────────────────────────────────────────── │
              │     [StreamEvent(is_sync_marker=True)]            │
              │                                                   │
              │  6. (Bounded Replay) <notificationComplete/>      │
              │ ◄──────────────────────────────────────────────── │
              │     [Stream terminated cleanly]                   │
              │                                                   │
              │  7. Cancellation (context.is_cancelled)           │
              │     client.close() / session.close_session()      │
              │ ────────────────────────────────────────────────► │
```

1. **Capability Exchange**: Verify the server advertises `urn:ietf:params:netconf:capability:notification:1.0`.
2. **Subscription Initiation**: Dispatch `<create-subscription>` specifying:
   * `stream` (default: `"NETCONF"`).
   * `filter` (Subtree XML or XPath expression).
   * `startTime` (ISO 8601 / RFC 3339 timestamp for replay).
   * `stopTime` (ISO 8601 / RFC 3339 timestamp for bounded replay).
3. **Notification Consumer Loop**: Wrap `ncclient`'s `take_notification(block=True, timeout=poll_timeout)` in a generator loop that monitors `context.is_cancelled`.
4. **Hybrid Payload Packaging**:
   * `StreamEvent.raw_payload`: Preserves the complete raw `<notification>` XML string.
   * `StreamEvent.data`: Parsed dictionary via `xmltodict` for rapid JSON serialization and output formatting.
   * `StreamEvent.timestamp`: Extracted `<eventTime>` parsed into a POSIX float timestamp.
5. **Replay Markers**:
   * `<replayComplete/>` sets `StreamEvent.is_sync_marker = True` (analogous to gNMI `sync_response`).
   * `<notificationComplete/>` signals end of bounded replay, cleanly breaking the generator loop.

---

## 3. Component Specification & Code Changes

> NOTE: These are example code segments, not full implementation specs.

### 3.1 Protocol Options (`config/protocol_options/netconf.py`)

Extend `NetconfOptions` with RFC 5277 fields and validation logic:

```python
@dataclass(frozen=True, kw_only=True)
class NetconfOptions(BaseProtocolOptions):
    """NETCONF-specific protocol options."""
    # Existing fields...
    target_datastore: str = "candidate"
    source: str = "running"
    source_datastore: Optional[str] = None
    config: Optional[str] = None
    default_operation: str = "merge"
    test_option: Optional[str] = None
    error_option: str = "stop-on-error"
    commit: bool = True
    device: str = "default"
    lock_target: bool = False
    validate_candidate: bool = False
    confirmed: bool = False
    confirm_timeout: Optional[int] = None
    persist: str = ""
    persist_id: str = ""

    # RFC 5277 Event Notification Fields
    stream_name: str = "NETCONF"
    start_time: Optional[str] = None      # ISO 8601 / RFC 3339 (e.g. '2026-10-14T00:00:00Z')
    stop_time: Optional[str] = None       # ISO 8601 / RFC 3339 (must be > start_time)
    poll_timeout: float = 1.0             # Non-blocking poll interval for take_notification()

    def validate(self) -> None:
        """Validate NETCONF options consistency and RFC 5277 constraints."""
        super().validate()
        
        # Datastore and operation validations...
        # (Existing validations remain intact)

        # RFC 5277 Constraints
        if self.stop_time and not self.start_time:
            raise ValueError(
                "RFC 5277 constraint violation: 'stop_time' cannot be specified without 'start_time'."
            )

        if self.start_time and self.stop_time:
            if self.stop_time <= self.start_time:
                raise ValueError(
                    f"Invalid time bounds: 'stop_time' ({self.stop_time}) must be strictly later "
                    f"than 'start_time' ({self.start_time})."
                )

        if self.poll_timeout <= 0:
            raise ValueError(
                f"Invalid poll_timeout '{self.poll_timeout}'. Must be a positive float."
            )
```

---

### 3.2 Client Implementation (`specs/netconf_client.py`)

Implement `execute_subscribe` and supporting helpers adhering to `BaseClient`:

```python
import time
import xmltodict
from datetime import datetime, timezone
from specs.base_client import BaseClient, UnsupportedOperationError
from specs.stream_types import StreamContext, StreamEvent
from config.selectors import PathSelector, FilterSelector
from config.operations import SubscribeOperation

class NetconfClient(BaseClient):
    # Existing methods...

    def _parse_event_time_to_posix(self, event_time_str: Optional[str]) -> float:
        """Converts an RFC 3339 eventTime string into a POSIX timestamp float."""
        if not event_time_str:
            return time.time()
        try:
            # Handle standard ISO formats, including Z suffix
            dt = datetime.fromisoformat(event_time_str.replace('Z', '+00:00'))
            return dt.timestamp()
        except Exception:
            return time.time()

    def _build_subscription_filter(self, selector: Any) -> Optional[tuple[str, str] | str]:
        """Constructs filter argument suitable for ncclient.create_subscription."""
        if selector is None:
            return None

        if isinstance(selector, FilterSelector):
            # If explicit filter selector provided
            content = selector.content.strip()
            if selector.filter_type == "xpath":
                return ("xpath", content)
            return content  # Subtree XML string

        if isinstance(selector, PathSelector):
            paths = list(selector.paths)
            if not paths:
                return None
            if len(paths) == 1 and not paths[0].startswith("<"):
                # Single XPath expression
                return ("xpath", paths[0])
            elif any(p.startswith("<") for p in paths):
                # Raw XML subtree filter
                return paths[0]
            else:
                # Disjunction or primary path
                return ("xpath", paths[0])

        if isinstance(selector, str):
            s = selector.strip()
            if s.startswith("<"):
                return s
            return ("xpath", s)

        return None

    def execute_subscribe(
        self,
        selectors: Any = None,
        options: Optional[BaseProtocolOptions] = None,
        context: Optional[StreamContext] = None,
        operation: Optional[SubscribeOperation] = None,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        """
        Executes an RFC 5277 NETCONF event notification subscription.
        Yields normalized StreamEvent objects until cancelled or replay finishes.
        """
        if context is None:
            context = StreamContext()

        # 1. Capability Verification
        server_caps = list(getattr(self.session, 'server_capabilities', []))
        if not any("urn:ietf:params:netconf:capability:notification:1.0" in c for c in server_caps):
            raise UnsupportedOperationError(
                "Target NETCONF server does not support RFC 5277 (:notification:1.0)."
            )

        # 2. Resolve parameters from operation or direct options
        opts = options or (operation.protocol_options if operation else None)
        stream_name = getattr(opts, 'stream_name', 'NETCONF') if opts else 'NETCONF'
        start_time = getattr(opts, 'start_time', None) if opts else None
        stop_time = getattr(opts, 'stop_time', None) if opts else None
        poll_timeout = float(getattr(opts, 'poll_timeout', 1.0) if opts else 1.0)

        # Extra kwargs overrides
        if 'stream_name' in kwargs:
            stream_name = kwargs['stream_name']
        if 'start_time' in kwargs:
            start_time = kwargs['start_time']
        if 'stop_time' in kwargs:
            stop_time = kwargs['stop_time']
        if 'poll_timeout' in kwargs:
            poll_timeout = float(kwargs['poll_timeout'])

        sel = operation.selector if operation else selectors
        nc_filter = self._build_subscription_filter(sel)

        logger.info(
            f"[NetconfClient {self.target}] Creating RFC 5277 subscription on stream '{stream_name}'..."
        )

        # 3. Dispatch <create-subscription> RPC
        try:
            reply = self.session.create_subscription(
                stream_name=stream_name,
                filter=nc_filter,
                start_time=start_time,
                stop_time=stop_time,
            )
            if not getattr(reply, 'ok', True):
                err = getattr(reply, 'error', 'Unknown error')
                raise RPCError(f"create-subscription failed: {err}")
        except Exception as e:
            logger.error(f"[NetconfClient {self.target}] create-subscription error: {e}")
            yield StreamEvent(protocol="netconf", timestamp=time.time(), raw_payload=None, error=e)
            return

        # 4. Continuous Notification Consumer Loop
        while not context.is_cancelled:
            try:
                notif = self.session.take_notification(block=True, timeout=poll_timeout)
                if notif is None:
                    continue

                raw_xml = getattr(notif, 'notification_xml', '')
                event_time_str = getattr(notif, 'event_time', None)
                ts = self._parse_event_time_to_posix(event_time_str)

                # Parse XML to dictionary for structured handlers
                event_dict = None
                try:
                    event_dict = xmltodict.parse(raw_xml)
                except Exception as parse_err:
                    logger.debug(f"[NetconfClient {self.target}] xmltodict parse warning: {parse_err}")

                is_replay_complete = "<replayComplete" in raw_xml
                is_notif_complete = "<notificationComplete" in raw_xml

                yield StreamEvent(
                    protocol="netconf",
                    timestamp=ts,
                    raw_payload=raw_xml,
                    data=event_dict,
                    is_sync_marker=is_replay_complete,
                )

                # Termination on bounded replay complete
                if is_notif_complete:
                    logger.info(
                        f"[NetconfClient {self.target}] Reached <notificationComplete/>. Stream finished."
                    )
                    break

            except Exception as e:
                if context.is_cancelled:
                    break
                logger.error(f"[NetconfClient {self.target}] Error reading notification: {e}")
                yield StreamEvent(protocol="netconf", timestamp=time.time(), raw_payload=None, error=e)
                break
```

---

### 3.3 Output Formatting Integration (`modules/formatter.py`)

Ensure `NETCONFFormatter.format_event()` formats incoming `StreamEvent` dictionaries and raw XML envelopes cleanly:

```python
class NETCONFFormatter(ProtocolFormatter):
    # Existing methods...

    def format_event(self, event: Any) -> str:
        """Formats a streaming notification StreamEvent."""
        from specs.stream_types import StreamEvent
        if isinstance(event, StreamEvent):
            if event.error is not None:
                return f"[NETCONF Notification Error] {event.error}"

            # If JSON output requested, serialize event.data or wrap envelope
            if self.output_format == OutputFormat.JSON:
                envelope = {
                    "protocol": "netconf",
                    "timestamp": event.timestamp,
                    "is_sync_marker": event.is_sync_marker,
                    "notification": event.data if event.data else event.raw_payload,
                }
                return json.dumps(envelope, indent=2)

            # Default / TEXT / XML output
            if event.raw_payload:
                return str(event.raw_payload).strip()
            if event.data:
                return json.dumps(event.data, indent=2)

        return super().format_event(event)
```

---

### 3.4 Configuration Normalization (`ui/cmd.py`)

Ensure `ui/cmd.py` maps NETCONF subscription blocks from YAML configs or CLI into `SubscribeOperation`:

```yaml
# Supported YAML Format
subscriptions:
  netconf_alarms:
    stream_name: "NETCONF"
    start_time: "2026-10-14T00:00:00Z"
    stop_time: "2026-10-14T12:00:00Z"
    filter:
      type: "xpath"
      select: "/ietf-interfaces:interfaces"

targets:
  172.20.20.2:830:
    protocol: "netconf"
    username: "admin"
    password: "password"
    subscriptions:
      - netconf_alarms
```

`ui/cmd.py` maps `protocol: netconf` with subscriptions into:
* `operation = SubscribeOperation(...)`
* `delivery = DeliveryPolicy(mode=DeliveryMode.EVENT_DRIVEN)`
* `protocol_options = NetconfOptions(stream_name=..., start_time=..., stop_time=...)`

---

## 4. Verification & Quality Gates

### 4.1 Unit Tests (`tests/test_netconf_subscribe.py`)
Implement comprehensive mocks for `ncclient.manager`:

1. **Capability Check Failure**:
   Verify `execute_subscribe()` raises `UnsupportedOperationError` if `:notification:1.0` is omitted from `server_capabilities`.
2. **Standard Continuous Stream**:
   Mock `take_notification()` returning simulated `<notification>` messages containing `<eventTime>` and link up/down payloads. Verify `StreamEvent` attributes (`timestamp`, `raw_payload`, `data`).
3. **Bounded Replay Termination**:
   Emit simulated `<notification>` followed by `<replayComplete/>` (`is_sync_marker=True`) and `<notificationComplete/>`. Confirm generator exits cleanly.
4. **Context Cancellation**:
   Trigger `context.cancel()` in a separate thread. Verify the generator breaks out of the loop immediately.
5. **Validation of `stop_time` without `start_time`**:
   Verify `NetconfOptions.validate()` raises `ValueError`.

### 4.2 Live Interoperability Test
Verify against a containerized router (e.g., Cisco XR or Netopeer2 NETCONF server simulator):
```bash
python client_main.py --target 172.20.20.4:830 netconf subscribe --stream NETCONF
```
Verify notifications are logged to stdout and that pressing `Ctrl+C` terminates the session cleanly without orphan SSH processes.