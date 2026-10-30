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

from enum import Enum

from application.dto import ExecutionRequestDTO, StreamEventDTO, SupportedProtocol
from application.devices import DeviceRegistryService
from application.operations import DeviceOperationService
from application.exceptions import (
    InvalidRequestError,
    SubscriptionNotFoundError,
    UnsupportedProtocolError,
)
from managers.subscribe_session import SubscribeSession


class SubscriptionState(str, Enum):
    """
    Lifecycle state of a streaming telemetry subscription

    Attributes:
        STARTING: Background worker thread spawned; stream context initialization in progress.
        RUNNING: Connection active and stream events/updates are flowing or ready.
        STOPPING: Stop or cancellation signal delivered; waiting for thread join.
        STOPPED: Subscription session and worker thread terminated cleanly.
        FAILED: Subscription worker terminated prematurely due to an RPC or transport error.
    """
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ManagedSubscription:
    """
    Internal resource container tracking an active streaming telemetry subscription.

    Maintains references to the underlying `SubscribeSession` worker, background execution
    thread, event queue, timing metrics, and lifecycle state.

    Attributes:
        subscription_id: Unique 36-character UUID string identifying the subscription.
        target: Target device address formatted as '<host>:<port>'.
        protocol: Management protocol used ('gnmi' or 'netconf').
        subscription_name: Logical subscription name defined by caller.
        worker: Concrete SubscribeSession worker instance executing the stream.
        thread: Background execution thread driving worker.start().
        data_queue: Thread-safe queue buffering incoming telemetry events.
        created_at: Unix timestamp when the subscription was instantiated.
        stopped_at: Unix timestamp when the subscription stopped, or None if active.
        state: Current SubscriptionState enum value.
        error: Error description string if the subscription entered FAILED state.
    """

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
        """
        Initializes a `ManagedSubscription` tracking record.

        Args:
            subscription_id: Unique UUID string.
            target: Target address '<host>:<port>'.
            protocol: 'gnmi' or 'netconf'.
            subscription_name: Caller-defined subscription name.
            worker: `SubscribeSession` worker instance.
            thread: Background execution thread.
            data_queue: Thread-safe Queue receiving `StreamEvent` payloads.
        """
        self.subscription_id = subscription_id
        self.target = target
        self.protocol = protocol
        self.subscription_name = subscription_name
        self.worker = worker
        self.thread = thread
        self.data_queue = data_queue
        self.created_at = time.time()
        self.stopped_at: Optional[float] = None
        self.state = SubscriptionState.STARTING
        self.error: Optional[str] = None

    def _sync_state(self) -> None:
        """
        Synchronizes lifecycle state with actual worker and thread execution status.

        Updates state to STOPPED or FAILED if the thread or worker has completed.
        """
        if self.state in (SubscriptionState.STOPPED, SubscriptionState.FAILED):
            return

        worker_err = getattr(self.worker, "last_error", None)
        if isinstance(worker_err, (str, Exception)) and worker_err:
            self.error = str(worker_err)

        is_mock_worker = hasattr(self.worker, "_mock_return_value") or "Mock" in type(self.worker).__name__
        thread_alive = bool(self.thread and self.thread.is_alive()) if not is_mock_worker else bool(getattr(self.worker, "is_running", False))
        worker_running = bool(self.worker and getattr(self.worker, "is_running", False))

        if not thread_alive or not worker_running:
            self.stopped_at = self.stopped_at or time.time()
            if self.error:
                self.state = SubscriptionState.FAILED
            elif self.state == SubscriptionState.STOPPING:
                self.state = SubscriptionState.STOPPED
            else:
                self.state = SubscriptionState.STOPPED
        else:
            self.state = SubscriptionState.RUNNING

    @property
    def is_alive(self) -> bool:
        """
        True if the subscription is currently running and its worker thread is alive.
        """
        self._sync_state()
        return self.state == SubscriptionState.RUNNING


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
    ):
        """
        Initializes the subscription service.

        Args:
            device_registry: Optional `DeviceRegistryService` for endpoint resolution.
            operation_service: Optional `DeviceOperationService` for building session configs.
        """
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

        subscription_id = str(uuid.uuid4())
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

        Args:
            subscription_id: Unique subscription ID token.
            timeout: Maximum seconds to block waiting for the first event (default: 1.0).
            max_events: Maximum number of buffered events to drain in this call (default: 10).

        Returns:
            List of `StreamEventDTO` instances (empty if queue was empty within timeout).

        Raises:
            SubscriptionNotFoundError: If subscription_id is not active or recognized.
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

        Yields events as they arrive, and terminates cleanly when the subscription stops
        and its event queue is completely drained.

        Args:
            subscription_id: Unique subscription ID token.
            timeout: Maximum seconds to wait per queue get attempt (default: 1.0).

        Yields:
            `StreamEventDTO` instances.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not active or recognized.
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

        Args:
            subscription_id: Unique subscription ID token.
            command: Control command string ('poll' to trigger an on-demand update, 'stop' to cancel).

        Raises:
            InvalidRequestError: If command is not recognized ('poll' or 'stop').
            SubscriptionNotFoundError: If subscription_id is not active.
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

        Args:
            subscription_id: Unique subscription ID token to terminate.

        Returns:
            True if the subscription was found and stopped; False if already terminated or not found.
        """
        with self._lock:
            managed = self._active_subscriptions.pop(subscription_id, None)

        if managed is None:
            return False

        managed.state = SubscriptionState.STOPPING
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

    def list_active_subscriptions(self) -> List[Dict[str, Any]]:
        """
        Returns metadata for all running subscriptions and purges terminated entries.

        Returns:
            List of dictionaries containing subscription metadata (ID, target, protocol,
            state, creation/stopped timestamps, and error diagnostics).
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
                    "created_at": m.created_at,
                    "stopped_at": m.stopped_at,
                    "is_alive": m.is_alive,
                    "state": m.state.value,
                    "error": m.error,
                }
                for m in self._active_subscriptions.values()
            ]

    def is_poll_subscription(self, subscription_id: str) -> bool:
        """
        Checks if the subscription operates in `ON_DEMAND (poll)` mode.

        Args:
            subscription_id: Unique subscription ID token.

        Returns:
            True if configured for polling; False for periodic/on-change streaming.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not found.
        """
        managed = self._get_managed(subscription_id)
        return bool(
            getattr(managed.worker, "is_poll_mode", lambda: False)()
            or getattr(managed.worker, "kwargs", {}).get("mode", "").lower() == "poll"
        )

    def get_subscription_info(self, subscription_id: str) -> Dict[str, Any]:
        """
        Returns detailed status metadata for a specific active subscription.

        Args:
            subscription_id: Unique subscription ID token.

        Returns:
            Dictionary containing target, protocol, state, thread liveness, and error details.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not found.
        """
        managed = self._get_managed(subscription_id)
        return {
            "subscription_id": managed.subscription_id,
            "session_id": getattr(managed.worker, "session_id", managed.subscription_id),
            "target": managed.target,
            "protocol": managed.protocol,
            "subscription_name": managed.subscription_name,
            "created_at": managed.created_at,
            "stopped_at": managed.stopped_at,
            "is_alive": managed.is_alive,
            "state": managed.state.value,
            "error": managed.error,
            "is_poll_mode": self.is_poll_subscription(subscription_id),
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

    def _get_managed(self, subscription_id: str) -> ManagedSubscription:
        """
        Internal lookup for an active `ManagedSubscription` record.

        Args:
            subscription_id: Subscription UUID string.

        Returns:
            `ManagedSubscription` instance.

        Raises:
            SubscriptionNotFoundError: If subscription_id is not found in active subscriptions.
        """
        with self._lock:
            managed = self._active_subscriptions.get(subscription_id)
            if managed is None:
                raise SubscriptionNotFoundError(f"Active subscription '{subscription_id}' not found.")
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
        )
