# -*- coding: utf-8 -*-
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli.cmd import build_args, config_builder
from cli.runner import CLIApplicationRunner, CLIStreamRunner
from cli.stats_formatter import write_cli_stats
from config.model import OutputConfig, OutputFormat, OutputType
from application.dto import (
    AdhocEndpointDTO,
    ExecutionRequestDTO,
    ExecutionStatsDTO,
    ExecutionStepStatsDTO,
    OperationStepDTO,
    SubscriptionStatsDTO,
)
from application.stats import StatsCollector


class TestCLIStatsPresentation:
    def test_cli_args_parse_stats_flags(self):
        args = build_args([
            "--stats",
            "--stats-format", "json",
            "--stats-file", "/tmp/netmgmt_stats.json",
            "-t", "10.0.0.1:57400",
            "gnmi", "capability",
        ])
        cfg = config_builder(args)
        assert cfg.stats is True
        assert cfg.stats_format == "json"
        assert cfg.stats_file == "/tmp/netmgmt_stats.json"

    def test_write_cli_stats_stderr_and_file_do_not_touch_stdout(self, tmp_path, capsys):
        stats_dto = ExecutionStatsDTO(
            protocol="netconf",
            target="172.20.20.4:830",
            total_duration_ms=125.4,
            connect_duration_ms=40.2,
            validation_duration_ms=3.1,
            validation_passed=True,
            operations_total=1,
            operations_succeeded=1,
            request_payload_bytes=512,
            response_payload_bytes=1024,
            steps=[
                ExecutionStepStatsDTO(
                    step_index=0,
                    op_type="get",
                    success=True,
                    duration_ms=82.0,
                    wire_rpc_ms=79.5,
                )
            ],
        )
        out_file = tmp_path / "exec_stats.json"
        write_cli_stats(
            [stats_dto],
            print_stderr=True,
            fmt="table",
            file_path=str(out_file),
        )

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Execution Statistics (netconf @ 172.20.20.4:830)" in captured.err
        assert "1,024 B received" in captured.err

        loaded = json.loads(out_file.read_text(encoding="utf-8"))
        assert loaded["protocol"] == "netconf"
        assert loaded["operations_succeeded"] == 1

    def test_cli_application_runner_stdout_clean_while_stats_on_stderr(self, tmp_path, capsys):
        stats_file = tmp_path / "cli_run_stats.json"
        parsed = SimpleNamespace(
            outputs=[OutputConfig(format=OutputFormat.JSON, output_type=OutputType.STDOUT)],
            requests=[
                ExecutionRequestDTO(
                    adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
                    operations=[OperationStepDTO(op_type="capabilities")],
                )
            ],
            sessions=[],
            device_registry=None,
            stats=True,
            stats_format="json",
            stats_file=str(stats_file),
        )

        with patch("application.operations.DeviceOperationService._resolve_worker") as mock_resolve:
            mock_worker = MagicMock()
            mock_worker.start.return_value = {
                "session_id": "s1",
                "target": "10.0.0.1:57400",
                "rpc": "capability",
                "data": SimpleNamespace(
                    gNMI_version="0.8.0",
                    supported_encodings=[],
                    supported_models=[],
                ),
                "protocol": "gnmi",
            }
            mock_resolve.return_value = mock_worker

            results = CLIApplicationRunner.execute(parsed)

        assert len(results) == 1
        assert results[0].success is True
        captured = capsys.readouterr()
        # stdout must be pure JSON from OutputHandler, never mixed with stats
        stdout_json = json.loads(captured.out)
        assert stdout_json["gNMI_version"] == "0.8.0"

        stderr_json = json.loads(captured.err)
        assert stderr_json["protocol"] == "gnmi"
        assert stderr_json["target"] == "10.0.0.1:57400"
        assert stats_file.is_file()

    def test_cli_stream_runner_records_stats_on_keyboard_interrupt(self, tmp_path, capsys):
        collector = StatsCollector()
        sub_svc = MagicMock()
        sub_svc.start_subscription.side_effect = lambda request=None, **kw: (
            collector.record_subscription_started("sub-cli-1", "gnmi", "periodic", "10.0.0.1:57400")
            or "sub-cli-1"
        )
        sub_svc.is_poll_subscription.return_value = False
        sub_svc.has_active_threads.return_value = True

        def _stop_all():
            collector.record_subscription_ended("sub-cli-1", "gnmi", "10.0.0.1:57400", "client_stopped")

        sub_svc.stop_all.side_effect = _stop_all

        runner = CLIStreamRunner(subscription_service=sub_svc, stats_collector=collector)
        runner.data_queue = MagicMock()
        runner.data_queue.empty.return_value = False
        runner.data_queue.get.side_effect = KeyboardInterrupt()

        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="10.0.0.1:57400", protocol="gnmi"),
            operations=[OperationStepDTO(op_type="subscribe", paths=["/interfaces"])],
        )
        sub_ids = runner.run([req])

        assert sub_ids == ["sub-cli-1"]
        assert len(runner.last_subscription_stats) == 1
        assert isinstance(runner.last_subscription_stats[0], SubscriptionStatsDTO)
        assert runner.last_subscription_stats[0].disconnect_reason == "client_stopped"

        buf = io.StringIO()
        write_cli_stats(runner.last_subscription_stats, print_stderr=True, fmt="table", stream=buf)
        assert "Subscription Statistics (gnmi @ 10.0.0.1:57400 | sub-cli-1)" in buf.getvalue()
