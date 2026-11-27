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
    PermissionDeniedError,
    SubscriptionNotFoundError,
    UnsupportedProtocolError,
)
from application.subscription_queue import (
    _STOP_SENTINEL,
    _ConsumerQueue,
    FanoutQueue,
    ManagedSubscription,
    SubscriptionState,
)
from managers.subscribe_session import SubscribeSession
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder, safe_record


class SubscriptionService:
    """
    Stateful application service managing streaming telemetry subscriptions.

    Provides a clean, presentation-decoupled interface suitable for WebSockets,
    SSE routers, event busses, and interactive CLI streaming runners. Manages background
    worker threads, thread-safe bounded event queues with overflow policies, interactive
    poll triggers, and deterministic resource cancellation.
    """

    def __init__(
        self,
        device_registry: Optional[DeviceRegistryService] = None,
        operation_service: Optional[DeviceOperationService] = None,
        stats_recorder: Optional[StatsRecorder] = None,
    ):
        """
        Initializes the subscription service.

        Args:
            device_registry: Optional `DeviceRegistryService` for endpoint resolution.
            operation_service: Optional `DeviceOperationService` for building session configs.
            stats_recorder: Optional `StatsRecorder` for telemetry and queue metrics.
        """
        self._registry = device_registry or DeviceRegistryService()
        self._stats_recorder: StatsRecorder = stats_recorder or NOOP_STATS_RECORDER
        self._op_service = operation_service or DeviceOperationService(
            device_registry=self._registry,
            stats_recorder=self._stats_recorder,
        )
        self._lock = threading.RLock()
        self._active_subscriptions: Dict[str, ManagedSubscription] = {}
        self._consumer_records: Dict[str, tuple[ManagedSubscription, queue.Queue]] = {}
        if hasattr(self._stats_recorder, "register_gauge_provider"):
            self._stats_recorder.register_gauge_provider(
                "subscriptions_active",
                self._live_subscription_count,
            )

    def _live_subscription_count(self) -> int:
        with self._lock:
            return sum(1 for m in self._active_subscriptions.values() if m.is_alive)

    def start_subscription(
        self,
        request: ExecutionRequestDTO,
        queue_size: int = 1000,
        shared_queue: Optional[queue.Queue] = None,
        *,
        owner_id: Optional[str] = None,
    ) -> str:
        """
        Starts a streaming telemetry session in a managed background daemon thread.

        Translates the request into a `SubscribeSession`, wires a thread-safe event queue,
        spawns a dedicated worker thread, and registers the subscription under a unique UUID4.

        Args:
            request: `ExecutionRequestDTO` containing a subscribe `OperationStepDTO`.
            queue_size: Maximum event buffer capacity if creating a private queue (default: 1000).
            shared_queue: Optional shared queue.Queue instance for multi-target aggregation.

        Returns:
            Opaque 36-character UUID string identifying the started subscription.

        Raises:
            UnsupportedProtocolError: If protocol is not 'gnmi' or 'netconf'.
            InvalidRequestError: If request does not contain a subscribe operation step.
            DeviceNotFoundError: If request specifies an unregistered device_id.
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
        delivery_obj = getattr(getattr(session_cfg.operation, "delivery", None), "mode", None)
        delivery_mode = delivery_obj.value if hasattr(delivery_obj, "value") else "periodic"

        subscription_id = str(uuid.uuid4())
        event_queue: queue.Queue = (
            shared_queue
            if shared_queue is not None
            else FanoutQueue(
                maxsize=queue_size,
                subscription_id=subscription_id,
                stats_recorder=self._stats_recorder,
            )
        )

        safe_record(
            self._stats_recorder.record_subscription_started,
            subscription_id,
            endpoint.protocol,
            delivery_mode,
            endpoint.target,
            owner_id=owner_id,
        )

        worker = SubscribeSession(
            config=session_cfg,
            data_queue=event_queue,
            subscription_name=sub_name,
        )
        if "Mock" not in type(worker).__name__ and hasattr(worker, "stats_recorder"):
            worker.stats_recorder = self._stats_recorder
            worker.subscription_id = subscription_id

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
            owner_id=owner_id,
        )

        with self._lock:
            self._active_subscriptions[subscription_id] = managed

        return subscription_id

    def get_events(
        self,
        subscription_id: str,
        timeout: float = 1.0,
        max_events: int = 10,
        *,
        owner_id: Optional[str] = None,
    ) -> List[StreamEventDTO]:
        """
        Retrieves up to max_events from the subscription event queue without blocking indefinitely.

        Args:
            subscription_id: Unique subscription ID token.
            timeout: Maximum seconds to block waiting for the first event (default: 1.0).
            max_events: Maximum number of buffered events to drain in this call (default: 10).
            owner_id: Optional caller identifier for ownership verification.

        Returns:
            List of `StreamEventDTO` instances (empty if queue was empty within timeout).

        Raises:
            SubscriptionNotFoundError: If subscription_id is not active or recognized.
            PermissionDeniedError: If `owner_id` does not match the subscription owner.
        """
        managed = self._get_managed(subscription_id, owner_id=owner_id)
        events, _ = self._drain_queue_events(
            managed.data_queue, managed, timeout=timeout, max_events=max_events
        )
        return events

    def open_consumer(
        self,
        subscription_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> str:
        """
        Registers an isolated consumer cursor on an active subscription for broadcast delivery.
        """
        managed = self._get_managed(subscription_id, owner_id=owner_id)
        consumer_id = str(uuid.uuid4())
        if isinstance(managed.data_queue, FanoutQueue):
            cq: queue.Queue = managed.data_queue.register_consumer(consumer_id)
        else:
            cq = managed.data_queue
        with self._lock:
            self._consumer_records[consumer_id] = (managed, cq)
        return consumer_id

    def get_consumer_events(
        self,
        subscription_id: str,
        consumer_id: str,
        timeout: float = 0.1,
        max_events: int = 25,
        *,
        owner_id: Optional[str] = None,
    ) -> List[StreamEventDTO]:
        """
        Drains up to max_events from a registered broadcast consumer queue.
        """
        with self._lock:
            entry = self._consumer_records.get(consumer_id)
        if entry is None:
            raise SubscriptionNotFoundError(
                f"Consumer '{consumer_id}' for subscription '{subscription_id}' not found."
            )
        managed, cq = entry
        if managed.subscription_id != subscription_id:
            raise SubscriptionNotFoundError(
                f"Consumer '{consumer_id}' does not belong to subscription '{subscription_id}'."
            )
        if owner_id is not None and managed.owner_id is not None and managed.owner_id != owner_id:
            raise PermissionDeniedError(
                f"Caller '{owner_id}' is not authorized to access subscription '{subscription_id}'."
            )
        if cq.empty() and getattr(cq, "closed", False):
            raise SubscriptionNotFoundError(f"Active subscription '{subscription_id}' closed.")

        events, hit_close = self._drain_queue_events(
            cq, managed, timeout=timeout, max_events=max_events
        )
        if not events and (hit_close or (cq.empty() and getattr(cq, "closed", False))):
            raise SubscriptionNotFoundError(f"Active subscription '{subscription_id}' closed.")
        return events

    def close_consumer(self, consumer_id: str) -> None:
        """Detaches and cleans up a registered broadcast consumer without stopping the subscription."""
        with self._lock:
            entry = self._consumer_records.pop(consumer_id, None)
        if entry is None:
            return
        managed, _ = entry
        if isinstance(managed.data_queue, FanoutQueue):
            managed.data_queue.unregister_consumer(consumer_id)

    def _drain_queue_events(
        self,
        event_queue: queue.Queue,
        managed: ManagedSubscription,
        timeout: float,
        max_events: int,
    ) -> tuple[List[StreamEventDTO], bool]:
        """Drains up to max_events from event_queue and reports whether a close sentinel was hit."""
        events: List[StreamEventDTO] = []
        hit_close = False
        try:
            first_item = event_queue.get(timeout=timeout)
            event_queue.task_done()
            if first_item is _STOP_SENTINEL:
                return [], True
            events.append(self._to_dto(first_item, managed))

            while len(events) < max_events:
                try:
                    item = event_queue.get_nowait()
                    event_queue.task_done()
                    if item is _STOP_SENTINEL:
                        hit_close = True
                        break
                    events.append(self._to_dto(item, managed))
                except queue.Empty:
                    break
        except queue.Empty:
            pass
        return events, hit_close

    def consume_events_generator(
        self,
        subscription_id: str,
        timeout: float = 1.0,
        *,
        owner_id: Optional[str] = None,
    ) -> Iterator[StreamEventDTO]:
        """
        Generator yielding streaming telemetry events while the subscription remains active.

        Yields events as they arrive, and terminates cleanly when the subscription stops
        and its event queue is completely drained.

        Args:
            subscription_id: Unique subscription ID token.
            timeout: Maximum seconds to wait per queue get attempt (default: 1.0).
            owner_id: Optional caller identifier for ownership verification.

        Yields:
            `StreamEventDTO` instances.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not active or recognized.
            PermissionDeniedError: If `owner_id` does not match the subscription owner.
        """
        managed = self._get_managed(subscription_id, owner_id=owner_id)
        while managed.is_alive or not managed.data_queue.empty():
            try:
                item = managed.data_queue.get(timeout=timeout)
                yield self._to_dto(item, managed)
                managed.data_queue.task_done()
            except queue.Empty:
                continue

    def send_command(
        self,
        subscription_id: str,
        command: str,
        *,
        owner_id: Optional[str] = None,
    ) -> None:
        """
        Dispatches control commands ('poll', 'stop') to an active subscription.

        Args:
            subscription_id: Unique subscription ID token.
            command: Control command string ('poll' to trigger an on-demand update, 'stop' to cancel).
            owner_id: Optional caller identifier for ownership verification.

        Raises:
            InvalidRequestError: If command is not recognized ('poll' or 'stop').
            SubscriptionNotFoundError: If subscription_id is not active.
            PermissionDeniedError: If `owner_id` does not match the subscription owner.
        """
        managed = self._get_managed(subscription_id, owner_id=owner_id)
        cmd = command.strip().lower()
        if cmd == "poll":
            managed.worker.trigger_poll()
        elif cmd == "stop":
            self.stop_subscription(subscription_id, owner_id=owner_id)
        else:
            raise InvalidRequestError(f"Unsupported subscription command '{command}'.")

    def stop_subscription(
        self,
        subscription_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> bool:
        """
        Terminates a subscription, cancels underlying streams, and cleans up resources.

        Args:
            subscription_id: Unique subscription ID token to terminate.
            owner_id: Optional caller identifier for ownership verification.

        Returns:
            True if the subscription was found and stopped; False if already terminated or not found.

        Raises:
            PermissionDeniedError: If `owner_id` does not match the subscription owner.
        """
        with self._lock:
            managed = self._active_subscriptions.get(subscription_id)
            if managed is None:
                return False
            if owner_id is not None and managed.owner_id is not None and managed.owner_id != owner_id:
                raise PermissionDeniedError(
                    f"Caller '{owner_id}' is not authorized to stop subscription '{subscription_id}'."
                )
            self._active_subscriptions.pop(subscription_id, None)

        managed.state = SubscriptionState.STOPPING
        if isinstance(managed.data_queue, FanoutQueue):
            managed.data_queue.close()
        managed.worker.stop()
        if hasattr(managed.worker, "is_running"):
            try:
                managed.worker.is_running = False
            except Exception:
                pass
        if managed.thread.is_alive():
            managed.thread.join(timeout=2.0)
        managed.state = SubscriptionState.STOPPED
        managed.stopped_at = time.time()
        return True

    def list_active_subscriptions(
        self,
        *,
        owner_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Returns metadata for all running subscriptions and purges terminated entries.

        Args:
            owner_id: Optional caller identifier to filter returned subscriptions.

        Returns:
            List of dictionaries containing subscription metadata (ID, target, protocol,
            state, creation/stopped timestamps, owner_id, and error diagnostics).
        """
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
                    "owner_id": m.owner_id,
                    "created_at": m.created_at,
                    "stopped_at": m.stopped_at,
                    "is_alive": m.is_alive,
                    "state": m.state.value,
                    "error": m.error,
                    "dropped_count": int(getattr(m.data_queue, "dropped_count", 0)),
                }
                for m in self._active_subscriptions.values()
                if owner_id is None or m.owner_id == owner_id
            ]

    def is_poll_subscription(
        self,
        subscription_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> bool:
        """
        Checks if the subscription operates in `ON_DEMAND (poll)` mode.

        Args:
            subscription_id: Unique subscription ID token.
            owner_id: Optional caller identifier for ownership verification.

        Returns:
            True if configured for polling; False for periodic/on-change streaming.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not found.
            PermissionDeniedError: If `owner_id` does not match the subscription owner.
        """
        managed = self._get_managed(subscription_id, owner_id=owner_id)
        return bool(
            getattr(managed.worker, "is_poll_mode", lambda: False)()
            or getattr(managed.worker, "kwargs", {}).get("mode", "").lower() == "poll"
        )

    def get_subscription_info(
        self,
        subscription_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns detailed status metadata for a specific active subscription.

        Args:
            subscription_id: Unique subscription ID token.
            owner_id: Optional caller identifier for ownership verification.

        Returns:
            Dictionary containing target, protocol, state, thread liveness, owner_id, and error details.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not found.
            PermissionDeniedError: If `owner_id` does not match the subscription owner.
        """
        managed = self._get_managed(subscription_id, owner_id=owner_id)
        return {
            "subscription_id": managed.subscription_id,
            "session_id": getattr(managed.worker, "session_id", managed.subscription_id),
            "target": managed.target,
            "protocol": managed.protocol,
            "subscription_name": managed.subscription_name,
            "owner_id": managed.owner_id,
            "created_at": managed.created_at,
            "stopped_at": managed.stopped_at,
            "is_alive": managed.is_alive,
            "state": managed.state.value,
            "error": managed.error,
            "is_poll_mode": self.is_poll_subscription(subscription_id, owner_id=owner_id),
            "dropped_count": int(getattr(managed.data_queue, "dropped_count", 0)),
        }

    def has_active_threads(self) -> bool:
        """
        Checks whether any managed subscription background thread is currently alive.

        Returns:
            True if at least one subscription thread is running; False otherwise.
        """
        with self._lock:
            return any(m.thread.is_alive() for m in self._active_subscriptions.values())

    def stop_all(self) -> None:
        """
        Terminates all active subscriptions and joins their execution threads.
        """
        with self._lock:
            sub_ids = list(self._active_subscriptions.keys())
        for sid in sub_ids:
            self.stop_subscription(sid)

    def _get_managed(
        self,
        subscription_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> ManagedSubscription:
        """
        Internal lookup for an active `ManagedSubscription` record.

        Args:
            subscription_id: Subscription UUID string.
            owner_id: Optional caller identifier for ownership verification.

        Returns:
            `ManagedSubscription` instance.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not found in active subscriptions.
            PermissionDeniedError: If `owner_id` does not match the subscription owner.
        """
        with self._lock:
            managed = self._active_subscriptions.get(subscription_id)
            if managed is None:
                raise SubscriptionNotFoundError(f"Active subscription '{subscription_id}' not found.")
            if owner_id is not None and managed.owner_id is not None and managed.owner_id != owner_id:
                raise PermissionDeniedError(
                    f"Caller '{owner_id}' is not authorized to access subscription '{subscription_id}'."
                )
            return managed

    def _to_dto(self, item: Dict[str, Any], managed: ManagedSubscription) -> StreamEventDTO:
        """
        Translates a raw event queue item dictionary into a structured `StreamEventDTO`.

        Args:
            item: Raw dictionary popped from the event queue.
            managed: `ManagedSubscription` context.

        Returns:
            `StreamEventDTO` instance.
        """
        return StreamEventDTO(
            session_id=item.get("session_id", managed.subscription_id),
            target=item.get("target", managed.target),
            protocol=item.get("protocol", managed.protocol),
            subscription_name=item.get("subscription_name", managed.subscription_name),
            data=item.get("data"),
            timestamp=item.get("timestamp", time.time()),
            is_sync_marker=item.get("is_sync_marker", False),
            error=str(item.get("error")) if item.get("error") is not None else None,
            sequence=item.get("sequence"),
        )
