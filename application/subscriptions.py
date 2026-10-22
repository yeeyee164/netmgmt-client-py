# -*- coding: utf-8 -*-
"""
Subscription Service.
Coordinates long-lived streaming telemetry lifecycles, background worker threads,
and thread-safe bounded event queues.
"""

import queue
import threading
import time
from typing import Any, Dict, Iterator, List, Optional
import uuid

from application.dto import ExecutionRequestDTO, StreamEventDTO, SupportedProtocol
from application.devices import DeviceRegistryService
from application.operations import DeviceOperationService
from application.exceptions import (
    InvalidRequestError,
    SubscriptionNotFoundError,
    UnsupportedProtocolError,
)
from managers.subscribe_session import SubscribeSession


class ManagedSubscription:
    """Holds references to an active streaming session thread and queue."""

    def __init__(
        self,
        subscription_id: str,
        target: str,
        protocol: str,
        subscription_name: str,
        worker: SubscribeSession,
        thread: threading.Thread,
        data_queue: queue.Queue,
    ):
        self.subscription_id = subscription_id
        self.target = target
        self.protocol = protocol
        self.subscription_name = subscription_name
        self.worker = worker
        self.thread = thread
        self.data_queue = data_queue
        self.created_at = time.time()

    @property
    def is_alive(self) -> bool:
        return bool(self.worker and getattr(self.worker, "is_running", False))


class SubscriptionService:
    """
    Stateful application service managing streaming telemetry subscriptions.
    Decoupled from CLI loops; suitable for WebSockets and async event routers.
    """

    def __init__(
        self,
        device_registry: Optional[DeviceRegistryService] = None,
        operation_service: Optional[DeviceOperationService] = None,
    ):
        self._registry = device_registry or DeviceRegistryService()
        self._op_service = operation_service or DeviceOperationService(device_registry=self._registry)
        self._lock = threading.RLock()
        self._active_subscriptions: Dict[str, ManagedSubscription] = {}

    def start_subscription(
        self,
        request: ExecutionRequestDTO,
        queue_size: int = 1000,
        shared_queue: Optional[queue.Queue] = None,
    ) -> str:
        """
        Starts a streaming telemetry session in a managed background thread.
        Returns a unique subscription_id token.
        """
        # Validate explicit protocol
        endpoint = self._registry.resolve_endpoint(request)
        if endpoint.protocol not in (SupportedProtocol.GNMI.value, SupportedProtocol.NETCONF.value):
            raise UnsupportedProtocolError(
                f"Protocol '{endpoint.protocol}' is unsupported for streaming telemetry."
            )

        # Confirm there is at least one subscribe operation
        has_sub = any(step.op_type.lower() == "subscribe" for step in request.operations)
        if not has_sub:
            raise InvalidRequestError("ExecutionRequestDTO must contain an operation with op_type='subscribe'.")

        session_cfg = self._op_service.build_session_config(request)
        sub_name = getattr(session_cfg.operation, "subscription_name", "sub")

        subscription_id = str(uuid.uuid4())[:8]
        event_queue: queue.Queue = shared_queue if shared_queue is not None else queue.Queue(maxsize=queue_size)

        worker = SubscribeSession(
            config=session_cfg,
            data_queue=event_queue,
            subscription_name=sub_name,
        )

        worker_thread = threading.Thread(
            target=worker.start,
            name=f"SubSession-{subscription_id}-{endpoint.target}",
            daemon=True,
        )
        worker_thread.start()

        managed = ManagedSubscription(
            subscription_id=subscription_id,
            target=endpoint.target,
            protocol=endpoint.protocol,
            subscription_name=sub_name,
            worker=worker,
            thread=worker_thread,
            data_queue=event_queue,
        )

        with self._lock:
            self._active_subscriptions[subscription_id] = managed

        return subscription_id

    def get_events(
        self,
        subscription_id: str,
        timeout: float = 1.0,
        max_events: int = 10,
    ) -> List[StreamEventDTO]:
        """
        Retrieves up to max_events from the subscription event queue without blocking indefinitely.
        """
        managed = self._get_managed(subscription_id)
        events: List[StreamEventDTO] = []

        try:
            first_item = managed.data_queue.get(timeout=timeout)
            events.append(self._to_dto(first_item, managed))
            managed.data_queue.task_done()

            # Drain any additional pending items up to max_events
            while len(events) < max_events:
                try:
                    item = managed.data_queue.get_nowait()
                    events.append(self._to_dto(item, managed))
                    managed.data_queue.task_done()
                except queue.Empty:
                    break
        except queue.Empty:
            pass

        return events

    def consume_events_generator(
        self,
        subscription_id: str,
        timeout: float = 1.0,
    ) -> Iterator[StreamEventDTO]:
        """
        Generator yielding streaming telemetry events while the subscription remains active.
        """
        managed = self._get_managed(subscription_id)
        while managed.is_alive or not managed.data_queue.empty():
            try:
                item = managed.data_queue.get(timeout=timeout)
                yield self._to_dto(item, managed)
                managed.data_queue.task_done()
            except queue.Empty:
                continue

    def send_command(self, subscription_id: str, command: str) -> None:
        """
        Dispatches control commands ('poll', 'stop') to an active subscription.
        """
        managed = self._get_managed(subscription_id)
        cmd = command.strip().lower()
        if cmd == "poll":
            managed.worker.trigger_poll()
        elif cmd == "stop":
            self.stop_subscription(subscription_id)
        else:
            raise InvalidRequestError(f"Unsupported subscription command '{command}'.")

    def stop_subscription(self, subscription_id: str) -> bool:
        """
        Terminates a subscription, cancels underlying streams, and cleans up resources.
        """
        with self._lock:
            managed = self._active_subscriptions.pop(subscription_id, None)

        if managed is None:
            return False

        managed.worker.stop()
        if hasattr(managed.worker, "is_running"):
            try:
                managed.worker.is_running = False
            except Exception:
                pass
        if managed.thread.is_alive():
            managed.thread.join(timeout=2.0)
        return True

    def list_active_subscriptions(self) -> List[Dict[str, Any]]:
        """Returns metadata for all running subscriptions."""
        with self._lock:
            # Purge dead threads
            dead_ids = [sid for sid, m in self._active_subscriptions.items() if not m.is_alive and m.data_queue.empty()]
            for did in dead_ids:
                self._active_subscriptions.pop(did, None)

            return [
                {
                    "subscription_id": m.subscription_id,
                    "target": m.target,
                    "protocol": m.protocol,
                    "subscription_name": m.subscription_name,
                    "created_at": m.created_at,
                    "is_alive": m.is_alive,
                }
                for m in self._active_subscriptions.values()
            ]

    def is_poll_subscription(self, subscription_id: str) -> bool:
        """Returns True if the subscription operates in ON_DEMAND (poll) mode."""
        managed = self._get_managed(subscription_id)
        return bool(
            getattr(managed.worker, "is_poll_mode", lambda: False)()
            or getattr(managed.worker, "kwargs", {}).get("mode", "").lower() == "poll"
        )

    def get_subscription_info(self, subscription_id: str) -> Dict[str, Any]:
        """Returns detailed metadata for a specific active subscription."""
        managed = self._get_managed(subscription_id)
        return {
            "subscription_id": managed.subscription_id,
            "session_id": getattr(managed.worker, "session_id", managed.subscription_id),
            "target": managed.target,
            "protocol": managed.protocol,
            "subscription_name": managed.subscription_name,
            "created_at": managed.created_at,
            "is_alive": managed.is_alive,
            "is_poll_mode": self.is_poll_subscription(subscription_id),
        }

    def has_active_threads(self) -> bool:
        """Returns True if any managed subscription thread is currently alive."""
        with self._lock:
            return any(m.thread.is_alive() for m in self._active_subscriptions.values())

    def stop_all(self) -> None:
        """Terminates all active subscriptions."""
        with self._lock:
            sub_ids = list(self._active_subscriptions.keys())
        for sid in sub_ids:
            self.stop_subscription(sid)

    def _get_managed(self, subscription_id: str) -> ManagedSubscription:
        with self._lock:
            managed = self._active_subscriptions.get(subscription_id)
            if managed is None:
                raise SubscriptionNotFoundError(f"Active subscription '{subscription_id}' not found.")
            return managed

    def _to_dto(self, item: Dict[str, Any], managed: ManagedSubscription) -> StreamEventDTO:
        return StreamEventDTO(
            session_id=item.get("session_id", managed.subscription_id),
            target=item.get("target", managed.target),
            protocol=item.get("protocol", managed.protocol),
            subscription_name=item.get("subscription_name", managed.subscription_name),
            data=item.get("data"),
            timestamp=item.get("timestamp", time.time()),
            is_sync_marker=item.get("is_sync_marker", False),
            error=str(item.get("error")) if item.get("error") is not None else None,
        )
