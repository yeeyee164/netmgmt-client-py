import time
import hashlib
import logging
import queue
from typing import Optional, Any

from managers.factory import ClientFactory
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder, safe_record
from util.utils import str_to_bytes, format_error_summary
from config.model import SessionConfig
from config.operations import SubscribeOperation
from config.delivery import DeliveryMode
from specs.stream_types import StreamContext, StreamEvent

logger = logging.getLogger(__name__)


class SubscribeSession:
    """
    Handles a single Subscribe service to a single target.
    Delegates streaming execution and transport control protocol-agnostically.
    """

    def __init__(self, target_ip=None, target_port=None, data_queue=None,
                 protocol="gnmi", username="", password="",
                 security=None, subscription_name="default_sub",
                 config=None, stats_recorder: Optional[StatsRecorder] = None,
                 subscription_id: Optional[str] = None, **kwargs):
        self.data_queue = data_queue
        self.stats_recorder: StatsRecorder = stats_recorder or NOOP_STATS_RECORDER
        self.config = config
        self.session = config
        self.selectors = None
        self.protocol_options = None
        self.kwargs = dict(kwargs)
        self.target_ip = target_ip
        self.target_port = target_port
        self.username = username
        self.password = password
        self.protocol = protocol
        self.security = security
        self.insecure = kwargs.get('insecure', False)
        self.operation = None
        self.subscription_name = subscription_name

        if config is not None:
            if isinstance(config, SessionConfig) or hasattr(config, 'connection'):
                self.target_ip = config.target_ip
                self.target_port = config.target_port
                self.username = config.username
                self.password = config.password
                proto_val = config.protocol
                self.protocol = proto_val.value if hasattr(proto_val, 'value') else str(proto_val).lower()
                self.security = config.security
                self.insecure = config.insecure
                self.operation = config.operation
                self.subscription_name = getattr(config.operation, 'subscription_name', 'default_sub')
                self.selectors = getattr(config.operation, 'selector', None)
                self.protocol_options = getattr(config.operation, 'protocol_options', None)

        self.debug = self.kwargs.get('debug', False)
        self.target = f'{self.target_ip}:{self.target_port}'

        raw_id_str = f"{self.target_ip}:{self.target_port}:{self.subscription_name}:{time.time()}"
        self.session_id = hashlib.md5(str_to_bytes(raw_id_str)).hexdigest()[:10]
        self.subscription_id = subscription_id or self.session_id

        self.is_running = False
        self.stream_context = StreamContext()
        self._thread = None
        self.timeout = self.kwargs.get('timeout', 10)
        self.client = None
        self._start_mono: float = time.perf_counter()
        self._sync_recorded: bool = False
        self._disconnect_reason: Optional[str] = None

    def is_poll_mode(self) -> bool:
        """Determines if the session uses on-demand delivery (gNMI POLL subscription)."""
        if isinstance(self.operation, SubscribeOperation):
            return getattr(self.operation.delivery, 'mode', None) is DeliveryMode.ON_DEMAND
        return str(self.kwargs.get('mode', '')).lower() == 'poll'

    def _enqueue_item(self, item: dict) -> None:
        if self.data_queue is None:
            return
        handles_drops = bool(getattr(self.data_queue, "handles_drop_stats", False))
        try:
            self.data_queue.put_nowait(item)
            if not handles_drops:
                safe_record(
                    self.stats_recorder.record_subscription_queue_depth,
                    self.subscription_id,
                    self.data_queue.qsize(),
                )
        except queue.Full:
            # DROP_OLDEST policy to prevent worker thread stall and unbounded memory growth
            try:
                _ = self.data_queue.get_nowait()
                self.data_queue.task_done()
                if not handles_drops:
                    safe_record(
                        self.stats_recorder.record_subscription_drop,
                        self.subscription_id,
                        "queue_full",
                    )
            except (queue.Empty, ValueError):
                pass
            try:
                self.data_queue.put_nowait(item)
                if not handles_drops:
                    safe_record(
                        self.stats_recorder.record_subscription_queue_depth,
                        self.subscription_id,
                        self.data_queue.qsize(),
                    )
            except queue.Full:
                pass
            logger.warning(
                f"[Worker {self.session_id} | {self.target}] Telemetry queue full; dropped oldest event."
            )

    @staticmethod
    def _estimate_payload_bytes(payload: Any) -> int:
        if payload is None:
            return 0
        byte_size_fn = getattr(payload, "ByteSize", None)
        if callable(byte_size_fn):
            try:
                return int(byte_size_fn())
            except Exception:
                return 0
        if isinstance(payload, bytes):
            return len(payload)
        if isinstance(payload, str):
            return len(payload.encode("utf-8"))
        return 0

    def _handle_stream_event(self, event: StreamEvent) -> None:
        """Processes incoming StreamEvent and forwards structured message to data_queue."""
        proto = event.protocol or self.protocol
        if event.error is not None:
            self.last_error = str(event.error)
            self._disconnect_reason = "timeout" if isinstance(event.error, TimeoutError) else "transport_error"
            safe_record(
                self.stats_recorder.record_subscription_event,
                self.subscription_id,
                proto,
                self.target,
                "error",
                0,
            )
            summary = format_error_summary(event.error)
            logger.error(f"[Worker {self.session_id} | {self.target_ip}] Stream event error: {summary}")
            self._enqueue_item({
                'session_id': self.session_id,
                'target': self.target,
                'subscription_name': self.subscription_name,
                'rpc': 'subscribe' if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('operation', 'unknown'),
                'data': event.error,
                'protocol': proto,
                'error': event.error,
            })
            return

        payload = event.raw_payload if event.raw_payload is not None else event.data
        ev_kind = getattr(event, "event_kind", None) or ("sync_response" if event.is_sync_marker else "update")
        safe_record(
            self.stats_recorder.record_subscription_event,
            self.subscription_id,
            proto,
            self.target,
            ev_kind,
            self._estimate_payload_bytes(payload),
        )
        if event.is_sync_marker and not self._sync_recorded:
            self._sync_recorded = True
            elapsed_ms = (time.perf_counter() - self._start_mono) * 1000.0
            safe_record(self.stats_recorder.record_subscription_sync, self.subscription_id, elapsed_ms)

        self._enqueue_item({
            'session_id': self.session_id,
            'target': self.target,
            'subscription_name': self.subscription_name,
            'rpc': 'subscribe' if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('operation', 'unknown'),
            'data': payload,
            'protocol': proto,
            'is_sync_marker': event.is_sync_marker,
        })

    def start(self) -> None:
        """Starts streaming execution loop delegating to client.execute_subscribe."""
        self.is_running = True
        self.last_error = None
        self._start_mono = time.perf_counter()
        self._sync_recorded = False
        self._disconnect_reason = None
        mode_str = self.operation.delivery.mode.value if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('mode', '').upper()
        logger.debug(f"[Worker {self.session_id} | {self.target_ip}]"
                     f" Starting '{self.subscription_name}' ({mode_str}) session...")

        try:
            client_kwargs = dict(self.kwargs) if hasattr(self, 'kwargs') and self.kwargs else {}
            for k in ['target', 'security', 'username', 'password', 'protocol']:
                client_kwargs.pop(k, None)
            if hasattr(self, 'insecure'):
                client_kwargs.setdefault('insecure', self.insecure)
            client_kwargs.setdefault('stats_recorder', self.stats_recorder)

            with ClientFactory.get_client(
                protocol=self.protocol, target=self.target,
                username=self.username, password=self.password,
                security=self.security, **client_kwargs) as client:
                self.client = client

                if isinstance(self.operation, SubscribeOperation):
                    stream = client.execute_subscribe(
                        operation=self.operation,
                        context=self.stream_context,
                    )
                else:
                    stream = client.execute_subscribe(
                        selectors=self.selectors,
                        options=self.protocol_options,
                        context=self.stream_context,
                        **self.kwargs,
                    )

                for event in stream:
                    if not self.is_running or self.stream_context.is_cancelled:
                        break

                    if isinstance(event, StreamEvent):
                        self._handle_stream_event(event)
                    else:
                        self._handle_stream_event(StreamEvent(
                            protocol=self.protocol,
                            timestamp=time.time(),
                            raw_payload=event,
                        ))

        except Exception as e:
            self.last_error = str(e)
            self._disconnect_reason = "timeout" if isinstance(e, TimeoutError) else "transport_error"
            if not self.stream_context.is_cancelled:
                summary = format_error_summary(e)
                logger.error(f"[Worker {self.session_id} | {self.target_ip}] Error in '{self.subscription_name}': {summary}")
        finally:
            self.is_running = False
            reason = self._disconnect_reason
            if reason is None:
                reason = "client_stopped" if self.stream_context.is_cancelled else "server_eof"
            safe_record(
                self.stats_recorder.record_subscription_ended,
                self.subscription_id,
                self.protocol,
                self.target,
                reason,
            )
            logger.info(f"[Worker {self.session_id} | {self.target_ip}] Disconnected from '{self.subscription_name}'.")

    def stop(self) -> None:
        """Stops the streaming session and cancels underlying stream context."""
        self.is_running = False
        if self.stream_context:
            self.stream_context.cancel()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.timeout)

    def trigger_poll(self) -> None:
        """Triggers a poll cycle on the active subscription stream via StreamContext."""
        if self.is_poll_mode():
            if self.stream_context:
                self.stream_context.request_poll()
                safe_record(self.stats_recorder.record_subscription_poll, self.subscription_id)
        else:
            mode_desc = self.operation.delivery.mode.value if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('mode', 'no')
            logger.warning(f"[Worker {self.session_id} | {self.target_ip}] Ignored polling trigger. Session is in '{mode_desc}' mode.")
