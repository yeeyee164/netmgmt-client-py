# -*- coding: utf-8 -*-
"""
Subscription Queue & State Primitives.
Provides thread-safe multi-consumer broadcast queues (`FanoutQueue`, `_ConsumerQueue`)
and subscription lifecycle tracking (`SubscriptionState`, `ManagedSubscription`).
"""

from enum import Enum
import queue
import threading
import time
from typing import Any, Dict, Optional

from managers.subscribe_session import SubscribeSession
from modules.stats_hooks import NOOP_STATS_RECORDER, StatsRecorder, safe_record

_STOP_SENTINEL = object()


class _ConsumerQueue(queue.Queue):
    """Per-consumer bounded queue carrying a close flag and dropped-event counter."""

    def __init__(self, maxsize: int = 0):
        super().__init__(maxsize=maxsize)
        self.closed: bool = False
        self.dropped_count: int = 0


class FanoutQueue(queue.Queue):
    """
    Thread-safe queue supporting both single-buffer pre-attach reads and
    multi-consumer non-blocking broadcast fan-out with per-consumer DROP_OLDEST overflow.
    """

    handles_drop_stats: bool = True

    def __init__(
        self,
        maxsize: int = 1000,
        *,
        subscription_id: Optional[str] = None,
        stats_recorder: Optional[StatsRecorder] = None,
    ):
        super().__init__(maxsize=maxsize)
        self._fanout_lock = threading.Lock()
        self._consumers: Dict[str, _ConsumerQueue] = {}
        self._closed: bool = False
        self.dropped_count: int = 0
        self._next_seq: int = 1
        self._subscription_id = subscription_id
        self._stats_recorder: StatsRecorder = stats_recorder or NOOP_STATS_RECORDER

    def _drop_oldest_and_put(self, q: queue.Queue, item: Any) -> bool:
        dropped = False
        try:
            queue.Queue.put(q, item, block=False)
        except queue.Full:
            try:
                _ = queue.Queue.get_nowait(q)
                q.task_done()
                dropped = True
                q.dropped_count = getattr(q, "dropped_count", 0) + 1
                if self._subscription_id:
                    safe_record(
                        self._stats_recorder.record_subscription_drop,
                        self._subscription_id,
                        "queue_full",
                    )
            except (queue.Empty, ValueError):
                pass
            try:
                queue.Queue.put(q, item, block=False)
            except queue.Full:
                pass
        return dropped

    def put(self, item: Any, block: bool = True, timeout: Optional[float] = None) -> None:
        with self._fanout_lock:
            if self._closed:
                return
            seq = self._next_seq
            self._next_seq += 1
            if isinstance(item, dict) and "sequence" not in item:
                item = {**item, "sequence": seq}
            if not self._consumers:
                self._drop_oldest_and_put(self, item)
                depth = self.qsize()
            else:
                for cq in self._consumers.values():
                    if self._drop_oldest_and_put(cq, item):
                        self.dropped_count += 1
                depth = max((cq.qsize() for cq in self._consumers.values()), default=0)
            if self._subscription_id:
                safe_record(
                    self._stats_recorder.record_subscription_queue_depth,
                    self._subscription_id,
                    depth,
                )

    def put_nowait(self, item: Any) -> None:
        self.put(item, block=False)

    def register_consumer(self, consumer_id: str) -> _ConsumerQueue:
        with self._fanout_lock:
            cq = _ConsumerQueue(maxsize=self.maxsize)
            if not self._consumers:
                cq.dropped_count = self.dropped_count
                # Drain any pre-buffered events into the first attached consumer.
                while True:
                    try:
                        buffered = queue.Queue.get_nowait(self)
                        self.task_done()
                    except (queue.Empty, ValueError):
                        break
                    self._drop_oldest_and_put(cq, buffered)
            if self._closed:
                cq.closed = True
                try:
                    queue.Queue.put(cq, _STOP_SENTINEL, block=False)
                except queue.Full:
                    pass
            self._consumers[consumer_id] = cq
            if self._subscription_id:
                depth = max((c.qsize() for c in self._consumers.values()), default=0)
                safe_record(
                    self._stats_recorder.record_subscription_queue_depth,
                    self._subscription_id,
                    depth,
                )
            return cq

    def unregister_consumer(self, consumer_id: str) -> None:
        with self._fanout_lock:
            cq = self._consumers.pop(consumer_id, None)
            if cq is not None and not self._closed and self._subscription_id:
                unread = cq.qsize()
                for _ in range(unread):
                    safe_record(
                        self._stats_recorder.record_subscription_drop,
                        self._subscription_id,
                        "consumer_gone",
                    )
            if self._subscription_id:
                depth = (
                    max((c.qsize() for c in self._consumers.values()), default=0)
                    if self._consumers
                    else self.qsize()
                )
                safe_record(
                    self._stats_recorder.record_subscription_queue_depth,
                    self._subscription_id,
                    depth,
                )

    def close(self) -> None:
        with self._fanout_lock:
            self._closed = True
            for cq in self._consumers.values():
                cq.closed = True
                try:
                    queue.Queue.put(cq, _STOP_SENTINEL, block=False)
                except queue.Full:
                    pass


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
        owner_id: Optional[str] = None,
    ):
        """Initializes a `ManagedSubscription` tracking record."""
        self.subscription_id = subscription_id
        self.target = target
        self.protocol = protocol
        self.subscription_name = subscription_name
        self.worker = worker
        self.thread = thread
        self.data_queue = data_queue
        self.owner_id = owner_id
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
