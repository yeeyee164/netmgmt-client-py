import time
import hashlib
import logging
import queue
from typing import Optional, Any

from managers.factory import ClientFactory
from util.utils import str_to_bytes
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
                 config=None, **kwargs):
        self.data_queue = data_queue
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

        self.is_running = False
        self.stream_context = StreamContext()
        self._thread = None
        self.timeout = self.kwargs.get('timeout', 10)
        self.client = None

    def is_poll_mode(self) -> bool:
        """Determines if the session uses on-demand delivery (gNMI POLL subscription)."""
        if isinstance(self.operation, SubscribeOperation):
            return getattr(self.operation.delivery, 'mode', None) is DeliveryMode.ON_DEMAND
        return str(self.kwargs.get('mode', '')).lower() == 'poll'

    def _enqueue_item(self, item: dict) -> None:
        if self.data_queue is None:
            return
        try:
            self.data_queue.put_nowait(item)
        except queue.Full:
            # DROP_OLDEST policy to prevent worker thread stall and unbounded memory growth
            try:
                _ = self.data_queue.get_nowait()
                self.data_queue.task_done()
            except (queue.Empty, ValueError):
                pass
            try:
                self.data_queue.put_nowait(item)
            except queue.Full:
                pass
            logger.warning(
                f"[Worker {self.session_id} | {self.target}] Telemetry queue full; dropped oldest event."
            )

    def _handle_stream_event(self, event: StreamEvent) -> None:
        """Processes incoming StreamEvent and forwards structured message to data_queue."""
        if event.error is not None:
            logger.error(f"[Worker {self.session_id} | {self.target_ip}] Stream event error: {event.error}")
            self._enqueue_item({
                'session_id': self.session_id,
                'target': self.target,
                'subscription_name': self.subscription_name,
                'rpc': 'subscribe' if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('operation', 'unknown'),
                'data': event.error,
                'protocol': event.protocol or self.protocol,
                'error': event.error,
            })
            return

        payload = event.raw_payload if event.raw_payload is not None else event.data
        self._enqueue_item({
            'session_id': self.session_id,
            'target': self.target,
            'subscription_name': self.subscription_name,
            'rpc': 'subscribe' if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('operation', 'unknown'),
            'data': payload,
            'protocol': event.protocol or self.protocol,
            'is_sync_marker': event.is_sync_marker,
        })

    def start(self) -> None:
        """Starts streaming execution loop delegating to client.execute_subscribe."""
        self.is_running = True
        self.last_error = None
        mode_str = self.operation.delivery.mode.value if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('mode', '').upper()
        logger.debug(f"[Worker {self.session_id} | {self.target_ip}]"
                     f" Starting '{self.subscription_name}' ({mode_str}) session...")

        try:
            client_kwargs = dict(self.kwargs) if hasattr(self, 'kwargs') and self.kwargs else {}
            for k in ['target', 'security', 'username', 'password', 'protocol']:
                client_kwargs.pop(k, None)
            if hasattr(self, 'insecure'):
                client_kwargs.setdefault('insecure', self.insecure)

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
            if not self.stream_context.is_cancelled:
                logger.error(f"[Worker {self.session_id} | {self.target_ip}] Error in '{self.subscription_name}': {e}")
        finally:
            self.is_running = False
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
        else:
            mode_desc = self.operation.delivery.mode.value if isinstance(self.operation, SubscribeOperation) else self.kwargs.get('mode', 'no')
            logger.warning(f"[Worker {self.session_id} | {self.target_ip}] Ignored polling trigger. Session is in '{mode_desc}' mode.")
