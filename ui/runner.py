# -*- coding: utf-8 -*-
"""
Presentation Layer CLI Execution Runners.
Bridges CLI/YAML ParsedConfig and OutputHandlers with the Application Service Layer
(DeviceOperationService and SubscriptionService).
"""

import logging
import queue
import threading
import time
from typing import Any, List, Optional, Sequence

from application.devices import DeviceRegistryService
from application.dto import ExecutionRequestDTO, ExecutionResultDTO
from application.operations import DeviceOperationService
from application.subscriptions import SubscriptionService
from modules.output import OutputHandler

logger = logging.getLogger(__name__)


class CLIStreamRunner:
    """
    Presentation-layer streaming telemetry runner for CLI sessions.
    Consumes events from SubscriptionService, routes to OutputHandlers,
    manages interactive gNMI POLL prompts, and handles Ctrl+C shutdown.
    """

    def __init__(
        self,
        subscription_service: Optional[SubscriptionService] = None,
        device_registry: Optional[DeviceRegistryService] = None,
    ):
        self.subscription_service = subscription_service or SubscriptionService(
            device_registry=device_registry
        )
        self.data_queue: queue.Queue = queue.Queue()

    def run(
        self,
        requests: Sequence[ExecutionRequestDTO],
        output_handlers: Optional[Sequence[OutputHandler]] = None,
    ) -> List[str]:
        """
        Starts streaming subscriptions for the given requests and blocks until completion
        or KeyboardInterrupt, routing received telemetry events to output_handlers.
        """
        expanded_requests: List[ExecutionRequestDTO] = []
        for req in requests:
            repeat = max(getattr(req, "times", 1), 1)
            for _ in range(repeat):
                expanded_requests.append(req)

        if not expanded_requests:
            logger.error("[CLIStreamRunner] No valid subscription requests to start. Exiting.")
            return []

        logger.info(f"[CLIStreamRunner] Starting {len(expanded_requests)} concurrent streaming sessions...")

        sub_ids: List[str] = []
        for req in expanded_requests:
            sid = self.subscription_service.start_subscription(
                request=req,
                shared_queue=self.data_queue,
            )
            sub_ids.append(sid)

        poll_sub_ids = [
            sid for sid in sub_ids if self.subscription_service.is_poll_subscription(sid)
        ]
        if poll_sub_ids:
            controller_t = threading.Thread(
                target=self._interactive_poll_controller,
                args=(poll_sub_ids,),
                daemon=True,
            )
            controller_t.start()

        handlers = list(output_handlers) if output_handlers else []

        try:
            while self.subscription_service.has_active_threads() or not self.data_queue.empty():
                try:
                    message = self.data_queue.get(timeout=1.0)
                    logger.debug(
                        f"\n--- [Telemetry from Session: {message.get('session_id')} | "
                        f"Target: {message.get('target')}] ---"
                    )
                    for handler in handlers:
                        handler.write(message)
                    self.data_queue.task_done()
                except queue.Empty:
                    if not self.subscription_service.has_active_threads():
                        break
                    continue
        except KeyboardInterrupt:
            logger.info("\n[CLIStreamRunner] Ctrl+C Detected! Initiating graceful shutdown...")
        finally:
            self.subscription_service.stop_all()
            logger.info("[CLIStreamRunner] All streaming sessions cleanly terminated. Goodbye!")

        return sub_ids

    def _interactive_poll_controller(self, poll_sub_ids: List[str]) -> None:
        """Interactive background CLI controller for triggering ON_DEMAND (poll) subscriptions."""
        time.sleep(2)

        while True:
            print("\n" + "=" * 40)
            print(" Interactive POLL Controller")
            print("=" * 40)
            infos = []
            for idx, sid in enumerate(poll_sub_ids):
                try:
                    info = self.subscription_service.get_subscription_info(sid)
                    infos.append(info)
                    print(
                        f"  [{idx}] {info['session_id']} - {info['target']} ({info['subscription_name']})"
                    )
                except Exception:
                    continue

            if not infos:
                break

            try:
                choice = (
                    input("\nType a session index to trigger, 'all', or press Enter to refresh: ")
                    .strip()
                    .lower()
                )
                if choice == "all":
                    for info in infos:
                        self.subscription_service.send_command(info["subscription_id"], "poll")
                        print(f"-> Sent POLL to {info['session_id']}")
                elif choice.isdigit():
                    idx = int(choice)
                    if 0 <= idx < len(infos):
                        target_info = infos[idx]
                        self.subscription_service.send_command(target_info["subscription_id"], "poll")
                        print(f"-> Sent POLL to {target_info['session_id']}")
                    else:
                        print("Invalid index.")
            except EOFError:
                break
            except KeyboardInterrupt:
                break


class CLIApplicationRunner:
    """
    Top-level presentation runner for CLI and YAML execution pipelines.
    Routes ParsedConfig requests to DeviceOperationService and SubscriptionService
    while managing OutputHandler lifecycles.
    """

    @staticmethod
    def is_stream_request(req: ExecutionRequestDTO) -> bool:
        return any(step.op_type.lower().strip() == "subscribe" for step in req.operations)

    @staticmethod
    def is_unary_request(req: ExecutionRequestDTO) -> bool:
        return bool(req.operations) and not CLIApplicationRunner.is_stream_request(req)

    @staticmethod
    def execute(cfg: Any) -> List[ExecutionResultDTO]:
        """
        Executes all unary and streaming requests in ParsedConfig via the Application Service Layer.
        """
        from ui.cmd import session_to_execution_request

        handlers: List[OutputHandler] = [
            OutputHandler(out_cfg) for out_cfg in getattr(cfg, "outputs", [])
        ]

        requests: List[ExecutionRequestDTO] = list(getattr(cfg, "requests", []) or [])
        if not requests and getattr(cfg, "sessions", None):
            requests = [session_to_execution_request(s) for s in cfg.sessions]

        device_registry: Optional[DeviceRegistryService] = getattr(cfg, "device_registry", None)

        unary_requests = [r for r in requests if CLIApplicationRunner.is_unary_request(r)]
        stream_requests = [r for r in requests if CLIApplicationRunner.is_stream_request(r)]

        results: List[ExecutionResultDTO] = []
        try:
            if unary_requests:
                op_service = DeviceOperationService(device_registry=device_registry)
                results.extend(
                    op_service.execute_batch(
                        requests=unary_requests,
                        output_handlers=handlers,
                    )
                )
            if stream_requests:
                stream_runner = CLIStreamRunner(device_registry=device_registry)
                stream_runner.run(
                    requests=stream_requests,
                    output_handlers=handlers,
                )
        finally:
            for handler in handlers:
                handler.close()

        return results

