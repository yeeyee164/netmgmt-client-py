from dataclasses import dataclass, field
from enum import Enum, auto
import queue
import threading
import time
from typing import Any, Dict, Optional


class StreamCommandType(Enum):
    """Commands dispatched from worker to the stream generator."""
    POLL = auto()
    CANCEL = auto()
    HEARTBEAT = auto()
    CUSTOM = auto()


@dataclass(frozen=True)
class StreamCommand:
    """Upstream command sent into an active stream channel."""
    command_type: StreamCommandType
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class StreamContext:
    """Bi-directional control context managing stream lifecycle and signal dispatch."""
    command_queue: queue.Queue = field(default_factory=queue.Queue)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def request_poll(self) -> None:
        """Signal the stream generator to execute a poll cycle."""
        self.command_queue.put(
            StreamCommand(command_type=StreamCommandType.POLL)
        )

    def cancel(self) -> None:
        """Signal the stream generator to stop and release underlying connections."""
        self.stop_event.set()
        self.command_queue.put(
            StreamCommand(command_type=StreamCommandType.CANCEL)
        )

    @property
    def is_cancelled(self) -> bool:
        return self.stop_event.is_set()


@dataclass
class StreamEvent:
    """Protocol-agnostic representation of an incoming telemetry notification or event."""
    protocol: str
    timestamp: float
    raw_payload: Any
    data: Optional[Dict[str, Any]] = None
    is_sync_marker: bool = False
    error: Optional[Exception] = None
    event_kind: str = "update"
