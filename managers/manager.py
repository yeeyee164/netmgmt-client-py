import warnings

warnings.warn(
    """
    This module has been deprecated. But you can still use it for
    build some requests simply.

    But I recommend to use `CLIApplicationRunner` or `DeviceOperationService`/`SubscriptionService` instead.
    """,
    DeprecationWarning,
    stacklevel=2,
)
import time
import threading
import queue
import concurrent.futures
from typing import List, Optional, Tuple, Type, Any

from managers.subscribe_session import SubscribeSession
from managers.unary_worker import (
    BaseUnaryWorker,
    GetWorker,
    SetWorker,
    CapabilityWorker,
    SequentialWorker,
    TransactionWorker,
)
from modules.output import OutputHandler
from config.operations import (
    CapabilitiesOperation,
    GetOperation,
    SetOperation,
    SubscribeOperation,
    GetSchemaOperation,
    TransactionOperation,
)

import logging

logger = logging.getLogger(__name__)

class BaseRPCManager:
    """
    Base class holding common utilities for all RPC Managers.
    """

    def __init__(
        self,
        sessions: Optional[List[Any]] = None,
        output_handlers: Optional[List[OutputHandler]] = None,
        debug: bool = False,
        cfg: Optional[Any] = None
    ):
        if cfg is not None:
            self.session_configs = sessions if sessions is not None else cfg.sessions
            self.debug = cfg.debug
            self.outputs = cfg.outputs
            if output_handlers is None:
                self.output_handlers = []
                for out_cfg in self.outputs:
                    self.output_handlers.append(OutputHandler(out_cfg))
                self._owns_handlers = True
            else:
                self.output_handlers = output_handlers
                self._owns_handlers = False
        else:
            self.session_configs = sessions or []
            self.output_handlers = output_handlers or []
            self.debug = debug
            self.outputs = {}
            self._owns_handlers = False

        self.sessions = []  # Holds the worker/session objects

    def shutdown(self):
        """Safely closes output file handles only if manager created them itself"""
        if getattr(self, '_owns_handlers', False):
            for handler in self.output_handlers:
                handler.close()
            logger.info("[Manager] Outputs cleanly closed. Goodbye!")

class SubscriptionManager(BaseRPCManager):
    """
    Orchestrates multiple SubscribeSession workers using Python threading package.
    """

    def __init__(
        self,
        sessions: Optional[List[Any]] = None,
        output_handlers: Optional[List[OutputHandler]] = None,
        debug: bool = False,
        cfg: Optional[Any] = None
    ):
        super().__init__(sessions=sessions, output_handlers=output_handlers, debug=debug, cfg=cfg)
        self.threads = []            # Holds the active Thread objects
        self.data_queue = queue.Queue()

    def build_sessions(self):
        """Parses the targets and instantiates the Worker objects."""
        for sc in self.session_configs:
            for _ in range(getattr(sc, 'times', 1)):
                session = SubscribeSession(
                    data_queue=self.data_queue,
                    config=sc
                )
                self.sessions.append(session)

    def run_all(self):
        """Delegates streaming execution to CLIStreamRunner and SubscriptionService."""
        if not self.session_configs:
            logger.error("[Manager] No valid sessions to start. Exiting.")
            self.shutdown()
            return

        from ui.cmd import session_to_execution_request
        from ui.runner import CLIStreamRunner

        requests = [session_to_execution_request(sc) for sc in self.session_configs]
        runner = CLIStreamRunner()
        try:
            runner.run(requests=requests, output_handlers=self.output_handlers)
        finally:
            self.shutdown()

    def _interactive_poll_controller(self):
        """A simple background CLI to allow users to trigger polls manually."""
        poll_sessions = [
            s for s in self.sessions
            if (getattr(s, 'is_poll_mode', lambda: False)() or getattr(s, 'kwargs', {}).get('mode', '').lower() == 'poll')
        ]
        time.sleep(2)
        
        while True:
            print("\n" + "="*40)
            print(" Interactive POLL Controller")
            print("="*40)
            for idx, s in enumerate(poll_sessions):
                print(f"  [{idx}] {s.session_id} - {s.target_ip} ({s.subscription_name})")
            
            try:
                choice = input("\nType a session index to trigger, 'all', or press Enter to refresh: ").strip().lower()
                if choice == 'all':
                    for s in poll_sessions:
                        s.trigger_poll()
                        print(f"-> Sent POLL to {s.session_id}")
                elif choice.isdigit():
                    idx = int(choice)
                    if 0 <= idx < len(poll_sessions):
                        poll_sessions[idx].trigger_poll()
                        print(f"-> Sent POLL to {poll_sessions[idx].session_id}")
                    else:
                        print("Invalid index.")
            except EOFError:
                break
            except KeyboardInterrupt:
                break       

class UnaryManager(BaseRPCManager):
    """
    Orchestrates RPCs except specific session-required using a ThreadPoolExecutor
    These tasks execute only once. It returns their data and exit.
    """

    def __init__(
        self,
        sessions: Optional[List[Any]] = None,
        output_handlers: Optional[List[OutputHandler]] = None,
        debug: bool = False,
        worker_class: Optional[Type[BaseUnaryWorker]] = None,
        cfg: Optional[Any] = None
    ):
        # Support legacy UnaryManager(cfg, worker_class) signature
        if hasattr(sessions, 'sessions') and cfg is None:
            cfg = sessions
            sessions = None
        super().__init__(sessions=sessions, output_handlers=output_handlers, debug=debug, cfg=cfg)
        self.worker_class = worker_class

    def _resolve_worker(self, sc: Any) -> BaseUnaryWorker:
        if self.worker_class is not None:
            return self.worker_class(config=sc)

        operations = getattr(sc, 'operations', ())
        if len(operations) > 1:
            return SequentialWorker(config=sc)

        op = operations[0] if len(operations) == 1 else getattr(sc, 'operation', None)
        if isinstance(op, (GetOperation, GetSchemaOperation)):
            return GetWorker(config=sc)
        elif isinstance(op, SetOperation):
            return SetWorker(config=sc)
        elif isinstance(op, CapabilitiesOperation):
            return CapabilityWorker(config=sc)
        elif isinstance(op, TransactionOperation):
            return TransactionWorker(config=sc)
        else:
            raise ValueError(f"Unsupported unary operation: {op}")

    def build_sessions(self):
        for sc in self.session_configs:
            for _ in range(getattr(sc, 'times', 1)):
                worker = self._resolve_worker(sc)
                self.sessions.append(worker)

    def run_all(self):
        """
        Delegates standard unary execution to DeviceOperationService.execute_batch(),
        retaining a direct fallback if a custom worker_class was injected.
        """
        if self.worker_class is None:
            if not self.session_configs:
                logger.warning("[Manager] No valid sessions to start. Exiting.")
                self.shutdown()
                return []

            from application.operations import DeviceOperationService
            from ui.cmd import session_to_execution_request

            requests = [session_to_execution_request(sc) for sc in self.session_configs]
            op_service = DeviceOperationService()
            try:
                return op_service.execute_batch(
                    requests=requests,
                    output_handlers=self.output_handlers,
                )
            finally:
                self.shutdown()

        self.build_sessions()
        if not self.sessions:
            logger.warning("[Manager] No valid sessions to start. Exiting.")
            self.shutdown()
            return []
        
        logger.info(f"[Manager] Starting {len(self.sessions)} concurrent unary tasks...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(self.sessions), 1)) as executor:
            future_to_session = {executor.submit(session.start): session for session in self.sessions}

            for future in concurrent.futures.as_completed(future_to_session):
                session = future_to_session[future]
                try:
                    result = future.result()
                    if result:
                        items = result if isinstance(result, list) else [result]
                        for item in items:
                            for handler in self.output_handlers:
                                handler.write(item)
                except Exception as exc:
                    worker_name = session.__class__.__name__
                    logger.error(f"[{worker_name} {session.target_ip}] generated an exception: {exc}")
        self.shutdown()

class ManagerFactory:
    """
    The main entry point for the execution engine.
    Routes the ParsedConfig to the appropriate protocol-agnostic manager based on the
    requested RPC.
    """

    @staticmethod
    def _is_unary_op(op: Any) -> bool:
        return isinstance(op, (CapabilitiesOperation, GetOperation, SetOperation, GetSchemaOperation, TransactionOperation))

    @staticmethod
    def _is_unary(session: Any) -> bool:
        op = getattr(session, 'operation', None)
        if ManagerFactory._is_unary_op(op):
            return True
        operations = getattr(session, 'operations', ())
        if operations and all(ManagerFactory._is_unary_op(o) for o in operations):
            return True
        return False

    @staticmethod
    def _is_stream(session: Any) -> bool:
        op = getattr(session, 'operation', None)
        if isinstance(op, SubscribeOperation):
            return True
        return False

    @staticmethod
    def create_managers(cfg: Any) -> Tuple[List[BaseRPCManager], List[OutputHandler]]:
        """
        Partitions sessions from ParsedConfig and instantiates the proper managers
        along with centralized OutputHandlers.
        """
        handlers = []
        for out_cfg in cfg.outputs:
            handlers.append(OutputHandler(out_cfg))

        # Validate that all operations are supported
        for s in cfg.sessions:
            if not ManagerFactory._is_unary(s) and not ManagerFactory._is_stream(s):
                raise ValueError(f"failed to pick a manager for operation: {getattr(s, 'operation', None)}")

        # Partition sessions strictly by operation type
        unary_sessions = [s for s in cfg.sessions if ManagerFactory._is_unary(s)]
        stream_sessions = [s for s in cfg.sessions if ManagerFactory._is_stream(s)]

        managers: List[BaseRPCManager] = []
        if unary_sessions:
            managers.append(UnaryManager(
                sessions=unary_sessions,
                output_handlers=handlers,
                debug=cfg.debug
            ))
        if stream_sessions:
            managers.append(SubscriptionManager(
                sessions=stream_sessions,
                output_handlers=handlers,
                debug=cfg.debug
            ))

        return managers, handlers

    @staticmethod
    def create_manager(cfg: Any) -> List[BaseRPCManager]:
        """
        Backward-compatible method returning only the list of managers.
        """
        managers, _ = ManagerFactory.create_managers(cfg)
        return managers

    @staticmethod
    def execute(cfg: Any):
        """
        Instantiates and executes managers for the given ParsedConfig with centralized output lifecycle.
        """
        managers, handlers = ManagerFactory.create_managers(cfg)
        try:
            for mgr in managers:
                mgr.run_all()
        finally:
            for handler in handlers:
                handler.close()
