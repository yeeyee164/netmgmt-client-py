# -*- coding: utf-8 -*-
"""
Execution configuration domain models and base classes for the UI layer.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import logging
from typing import Any, List, Optional

from config.model import OutputConfig, SessionConfig

logger = logging.getLogger(__name__)


class FileConfigError(Exception):
    """Custom exception raised when given config violates some criteria"""
    pass


@dataclass
class ParsedConfig:
    """
    Canonical normalized execution configuration produced by UI builders.
    Consumed by runner/application services or legacy managers.
    """
    sessions: List[SessionConfig] = field(default_factory=list)  # List of all individual sessions to spawn
    targets: List[str] = field(default_factory=list)  # List of "IP:PORT" strings
    outputs: List[OutputConfig] = field(default_factory=list)  # Outputs definitions
    debug: bool = False           # Global debug flag

    # for logging this script
    log_level: str = "ERROR"
    syslog_server: str = ""
    log_file: str = ""

    # Application Service Layer DTOs & Device Inventory
    requests: List[Any] = field(default_factory=list)
    device_registry: Optional[Any] = None

    # Statistics & Observability presentation options
    stats: bool = False
    stats_format: str = "table"
    stats_file: Optional[str] = None

    def __post_init__(self):
        if self.sessions and not self.requests:
            for s in self.sessions:
                try:
                    from cli.adapters import session_to_execution_request
                    self.requests.append(session_to_execution_request(s))
                except Exception as exc:
                    logger.debug("Skipping session->request adaptation in ParsedConfig: %s", exc)
        elif self.requests and not self.sessions:
            for r in self.requests:
                try:
                    from cli.adapters import execution_request_to_session
                    self.sessions.append(execution_request_to_session(r, registry=self.device_registry))
                except Exception as exc:
                    logger.debug("Skipping request->session adaptation in ParsedConfig: %s", exc)

    def __str__(self):
        session_strs = "\n".join([str(s) for s in self.sessions])
        return f"""ParsedConfig(debug={self.debug}, outputs={self.outputs},
sessions=[
{session_strs}
]
)
"""


class ConfigBuilder(ABC):
    @abstractmethod
    def build(self) -> ParsedConfig:
        """Parse input and return `ParsedConfig` object"""
        pass

