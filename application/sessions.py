# -*- coding: utf-8 -*-
"""
Managed Logical Session Service.
Owns cross-request logical sessions, retains live protocol resources (e.g. NETCONF SSH sessions
or gNMI gRPC channels), enforces caller ownership, serializes session-scoped operations via
context-managed leases, and performs safe idle expiration cleanup.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Set
import uuid

from application.devices import DeviceRegistryService
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    OperationStepDTO,
    SessionInfoDTO,
)
from application.exceptions import (
    ApplicationError,
    InvalidRequestError,
    PermissionDeniedError,
    SessionConflictError,
    SessionNotFoundError,
)
from config.model import SecurityProfile
from config.operations import NetconfTransactionOperation, TransactionType
from managers.factory import ClientFactory

logger = logging.getLogger(__name__)


def build_security_profile(endpoint: AdhocEndpointDTO) -> Optional[SecurityProfile]:
    """Builds a domain SecurityProfile from an AdhocEndpointDTO when security fields are set."""
    if any([
        endpoint.tls_ca,
        endpoint.tls_cert,
        endpoint.tls_key,
        endpoint.skip_verify,
        endpoint.tls_server_name,
        endpoint.tls_version,
        endpoint.ssh_key,
    ]):
        return SecurityProfile(
            tls_ca=endpoint.tls_ca or "",
            tls_cert=endpoint.tls_cert or "",
            tls_key=endpoint.tls_key or "",
            skip_verify=endpoint.skip_verify,
            tls_server_name=endpoint.tls_server_name or "",
            tls_version=endpoint.tls_version or "1.3",
            ssh_key=endpoint.ssh_key,
        )
    return None


class LogicalSessionState(str, Enum):
    """Lifecycle states for a managed logical device session."""

    CREATING = "creating"
    ACTIVE = "active"
    IN_USE = "in_use"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class SessionLease:
    """
    Active operation lease granted by `DeviceSessionManager.acquire_session`.

    Holds a reference to the retained protocol client and tracks protocol-specific
    transaction metadata across steps within the lease.
    """

    session_id: str
    device_id: Optional[str]
    endpoint: AdhocEndpointDTO
    protocol: str
    owner_id: str
    client: Any
    locked_datastores: Set[str] = field(default_factory=set)
    has_uncommitted_changes: bool = False

    def record_step_outcome(self, step: OperationStepDTO, success: bool) -> None:
        """
        Updates transaction/lock metadata after an operation step completes.

        Only tracks NETCONF datastore locks and candidate changes when `protocol == 'netconf'`.
        gNMI sessions intentionally do not inherit NETCONF transaction semantics.
        """
        if not success or self.protocol != "netconf":
            return

        norm_op = step.op_type.strip().lower().replace("-", "_")
        if norm_op == "lock":
            ds = step.datastore if step.datastore != "all" else "candidate"
            self.locked_datastores.add(ds)
        elif norm_op == "unlock":
            ds = step.datastore if step.datastore != "all" else "candidate"
            self.locked_datastores.discard(ds)
        elif norm_op in ("set", "edit_config", "edit"):
            target_ds = (
                step.datastore
                if step.datastore != "all"
                else step.options.get("target_datastore", "candidate")
            )
            default_commit = step.xml_payload is not None
            committed = bool(step.options.get("commit", default_commit))
            if committed:
                self.has_uncommitted_changes = False
            elif target_ds == "candidate":
                self.has_uncommitted_changes = True
        elif norm_op == "commit":
            self.has_uncommitted_changes = False
        elif norm_op == "discard_changes":
            self.has_uncommitted_changes = False


@dataclass
class _ManagedSessionRecord:
    """Internal mutable session state owned by `DeviceSessionManager`."""

    session_id: str
    device_id: Optional[str]
    endpoint: AdhocEndpointDTO
    protocol: str
    owner_id: str
    client: Any
    ttl_seconds: float
    created_at: float
    last_activity: float
    expires_at: float
    state: LogicalSessionState = LogicalSessionState.ACTIVE
    lease_count: int = 0
    locked_datastores: Set[str] = field(default_factory=set)
    has_uncommitted_changes: bool = False
    op_lock: threading.RLock = field(default_factory=threading.RLock)

    def to_dto(self) -> SessionInfoDTO:
        return SessionInfoDTO(
            session_id=self.session_id,
            device_id=self.device_id,
            target=self.endpoint.target,
            protocol=self.protocol,
            owner_id=self.owner_id,
            state=self.state.value,
            created_at=self.created_at,
            last_activity=self.last_activity,
            expires_at=self.expires_at,
            in_use=self.lease_count > 0,
            locked_datastores=sorted(self.locked_datastores),
            has_uncommitted_changes=self.has_uncommitted_changes,
        )


class DeviceSessionManager:
    """
    Application service owning managed logical device sessions and their protocol resources.

    Distinct from `DeviceRegistryService` (inventory) and `SubscriptionService` (telemetry).
    Retains live protocol clients (such as an open `NetconfClient` SSH session or `GNMIClient`
    channel) across requests, serializes concurrent operations per session, binds sessions to
    an authenticated owner, and enforces lease-safe idle expiration.
    """

    def __init__(
        self,
        device_registry: Optional[DeviceRegistryService] = None,
        default_ttl_seconds: float = 300.0,
        client_factory: Optional[Callable[..., Any]] = None,
    ):
        if default_ttl_seconds <= 0:
            raise InvalidRequestError(
                "default_ttl_seconds must be positive.",
                error_code="INVALID_TIMEOUT",
            )
        self._registry = device_registry or DeviceRegistryService()
        self._default_ttl_seconds = float(default_ttl_seconds)
        self._client_factory = client_factory or ClientFactory.get_client
        self._lock = threading.RLock()
        self._sessions: Dict[str, _ManagedSessionRecord] = {}

    def create_session(
        self,
        *,
        owner_id: str,
        device_id: Optional[str] = None,
        adhoc_endpoint: Optional[AdhocEndpointDTO] = None,
        protocol: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
    ) -> SessionInfoDTO:
        """
        Opens a protocol resource and registers a new logical session bound to `owner_id`.
        """
        if not owner_id or not isinstance(owner_id, str) or not owner_id.strip():
            raise InvalidRequestError(
                "Logical session requires a non-empty 'owner_id'.",
                error_code="INVALID_OWNER",
            )
        ttl = self._default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            raise InvalidRequestError(
                "Session 'ttl_seconds' must be positive.",
                error_code="INVALID_TIMEOUT",
            )

        endpoint = self._registry.resolve_endpoint(
            ExecutionRequestDTO(
                device_id=device_id,
                adhoc_endpoint=adhoc_endpoint,
                protocol=protocol,
            )
        )
        security = build_security_profile(endpoint)

        client = self._client_factory(
            protocol=endpoint.protocol,
            target=endpoint.target,
            username=endpoint.username or "",
            password=endpoint.password or "",
            security=security,
            insecure=bool(endpoint.insecure or endpoint.skip_verify),
        )
        try:
            client.__enter__()
        except Exception as exc:
            try:
                client.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            raise ApplicationError(
                f"Failed to establish logical session to '{endpoint.target}' ({endpoint.protocol}).",
                error_code="SESSION_CONNECT_ERROR",
            ) from exc

        now = time.time()
        session_id = str(uuid.uuid4())
        record = _ManagedSessionRecord(
            session_id=session_id,
            device_id=device_id,
            endpoint=endpoint,
            protocol=endpoint.protocol,
            owner_id=owner_id.strip(),
            client=client,
            ttl_seconds=ttl,
            created_at=now,
            last_activity=now,
            expires_at=now + ttl,
            state=LogicalSessionState.ACTIVE,
        )
        with self._lock:
            self._sessions[session_id] = record
            return record.to_dto()

    def get_session_info(
        self,
        session_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> SessionInfoDTO:
        """Returns safe metadata for a managed session after validating ownership."""
        self.sweep_expired_sessions()
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None or record.state == LogicalSessionState.CLOSED:
                raise SessionNotFoundError(f"Logical session '{session_id}' not found.")
            if owner_id is not None and record.owner_id != owner_id:
                raise PermissionDeniedError(
                    f"Caller '{owner_id}' is not authorized to access logical session '{session_id}'."
                )
            return record.to_dto()

    def list_sessions(self, *, owner_id: Optional[str] = None) -> List[SessionInfoDTO]:
        """Lists active managed sessions, optionally filtered by `owner_id`."""
        self.sweep_expired_sessions()
        with self._lock:
            records = list(self._sessions.values())
            return [
                r.to_dto()
                for r in records
                if r.state != LogicalSessionState.CLOSED
                and (owner_id is None or r.owner_id == owner_id)
            ]

    @contextmanager
    def acquire_session(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> Iterator[SessionLease]:
        """
        Acquires an exclusive operation lease on the logical session.

        Verifies ownership, blocks concurrent operations on the same session via `op_lock`,
        prevents idle expiration while leased, and updates activity/transaction metadata on release.
        """
        if not owner_id or not isinstance(owner_id, str) or not owner_id.strip():
            raise InvalidRequestError(
                "Session acquisition requires a non-empty 'owner_id'.",
                error_code="INVALID_OWNER",
            )

        expired_record: Optional[_ManagedSessionRecord] = None
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None or record.state in (
                LogicalSessionState.CLOSING,
                LogicalSessionState.CLOSED,
            ):
                raise SessionNotFoundError(f"Logical session '{session_id}' not found.")
            if record.owner_id != owner_id:
                raise PermissionDeniedError(
                    f"Caller '{owner_id}' is not authorized to use logical session '{session_id}'."
                )
            if record.lease_count == 0 and time.time() >= record.expires_at:
                record.state = LogicalSessionState.CLOSING
                self._sessions.pop(session_id, None)
                expired_record = record
            else:
                record.lease_count += 1

        if expired_record is not None:
            self._cleanup_record(expired_record, reason="idle_expiry")
            raise SessionNotFoundError(f"Logical session '{session_id}' has expired.")

        record.op_lock.acquire()
        lease_released = False
        try:
            with self._lock:
                if record.state in (
                    LogicalSessionState.CLOSING,
                    LogicalSessionState.CLOSED,
                ):
                    raise SessionNotFoundError(f"Logical session '{session_id}' is closed.")
                record.state = LogicalSessionState.IN_USE
                record.last_activity = time.time()
                lease = SessionLease(
                    session_id=record.session_id,
                    device_id=record.device_id,
                    endpoint=record.endpoint,
                    protocol=record.protocol,
                    owner_id=record.owner_id,
                    client=record.client,
                    locked_datastores=set(record.locked_datastores),
                    has_uncommitted_changes=record.has_uncommitted_changes,
                )

            yield lease
        finally:
            now = time.time()
            with self._lock:
                if "lease" in locals():
                    record.locked_datastores = set(lease.locked_datastores)
                    record.has_uncommitted_changes = lease.has_uncommitted_changes
                record.last_activity = now
                record.expires_at = now + record.ttl_seconds
                record.lease_count = max(0, record.lease_count - 1)
                if record.lease_count == 0 and record.state == LogicalSessionState.IN_USE:
                    record.state = LogicalSessionState.ACTIVE
            if not lease_released:
                record.op_lock.release()
                lease_released = True

    def close_session(
        self,
        session_id: str,
        *,
        owner_id: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """
        Explicitly closes a logical session and cleans up its retained protocol resources.
        Raises `SessionConflictError` if an operation lease is active and `force` is False.
        """
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            if owner_id is not None and record.owner_id != owner_id:
                raise PermissionDeniedError(
                    f"Caller '{owner_id}' is not authorized to close logical session '{session_id}'."
                )
            if record.lease_count > 0 and not force:
                raise SessionConflictError(
                    f"Cannot close logical session '{session_id}' while an operation lease is active."
                )
            record.state = LogicalSessionState.CLOSING
            self._sessions.pop(session_id, None)

        self._cleanup_record(record, reason="explicit_close")
        return True

    def sweep_expired_sessions(self, now: Optional[float] = None) -> List[str]:
        """
        Closes idle sessions whose `expires_at` timestamp has passed.

        Active sessions holding an operation lease (`lease_count > 0`) are never closed.
        """
        current_time = time.time() if now is None else now
        to_close: List[_ManagedSessionRecord] = []

        with self._lock:
            for sid, record in list(self._sessions.items()):
                if record.lease_count == 0 and current_time >= record.expires_at:
                    record.state = LogicalSessionState.CLOSING
                    self._sessions.pop(sid, None)
                    to_close.append(record)

        closed_ids: List[str] = []
        for record in to_close:
            self._cleanup_record(record, reason="idle_expiry")
            closed_ids.append(record.session_id)
        return closed_ids

    def close_all(self) -> None:
        """Deterministically closes all managed sessions during service shutdown."""
        with self._lock:
            records = list(self._sessions.values())
            for record in records:
                record.state = LogicalSessionState.CLOSING
            self._sessions.clear()

        for record in records:
            self._cleanup_record(record, reason="shutdown")

    def _cleanup_record(self, record: _ManagedSessionRecord, *, reason: str) -> None:
        """
        Performs safe transaction-lock and transport cleanup for a closing session.

        Rules:
        - Never issues `<discard-changes>` automatically on generic session expiration or close.
        - Only issues `<unlock>` when the expiring NETCONF session holds recorded lock ownership.
        - Always closes the underlying transport client via `__exit__`.
        """
        with record.op_lock:
            try:
                if (
                    record.protocol == "netconf"
                    and record.locked_datastores
                    and record.client is not None
                    and hasattr(record.client, "execute_transaction")
                ):
                    for ds in sorted(record.locked_datastores):
                        try:
                            unlock_op = NetconfTransactionOperation(
                                operation=TransactionType.UNLOCK,
                                target_datastore=ds,
                            )
                            record.client.execute_transaction(unlock_op)
                        except Exception as exc:
                            logger.warning(
                                f"[DeviceSessionManager] Best-effort unlock '{ds}' failed on session "
                                f"'{record.session_id}' ({reason}): {exc}"
                            )
                    record.locked_datastores.clear()
            finally:
                if record.client is not None:
                    try:
                        record.client.__exit__(None, None, None)
                    except Exception as exc:
                        logger.warning(
                            f"[DeviceSessionManager] Transport close error on session "
                            f"'{record.session_id}' ({reason}): {exc}"
                        )
                with self._lock:
                    record.state = LogicalSessionState.CLOSED
