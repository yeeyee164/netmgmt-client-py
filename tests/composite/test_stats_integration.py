# -*- coding: utf-8 -*-
import time
from unittest.mock import MagicMock, patch

import pytest

from application.devices import DeviceRegistryService
from application.dto import (
    AdhocEndpointDTO,
    DeliveryModeDTO,
    ExecutionRequestDTO,
    MutationChangeDTO,
    OperationStepDTO,
    TelemetryPolicyDTO,
)
from application.operations import DeviceOperationService
from application.schemas import SchemaCatalogService
from application.sessions import DeviceSessionManager
from application.stats import StatsCollector, StatsService
from application.subscriptions import SubscriptionService
from core.schema_db import SchemaDB
from core.schema_manager import SchemaManager
from core.schema_validator import SchemaValidator
from specs.stream_types import StreamEvent


class _FakeNetconfSession:
    def __init__(self, fail_on_validate: bool = False):
        self.server_capabilities = [
            "urn:ietf:params:netconf:base:1.1",
            "urn:ietf:params:netconf:capability:candidate:1.0",
            "urn:ietf:params:netconf:capability:validate:1.0",
        ]
        self.fail_on_validate = fail_on_validate
        self.calls = []

    def lock(self, target="candidate"):
        self.calls.append(("lock", target))
        return MagicMock(xml="<ok/>")

    def unlock(self, target="candidate"):
        self.calls.append(("unlock", target))
        return MagicMock(xml="<ok/>")

    def edit_config(self, **kwargs):
        self.calls.append(("edit-config", kwargs.get("target")))
        return MagicMock(xml="<ok/>")

    def validate(self, source="candidate"):
        self.calls.append(("validate", source))
        if self.fail_on_validate:
            raise RuntimeError("candidate validation failed")
        return MagicMock(xml="<ok/>")

    def discard_changes(self):
        self.calls.append(("discard-changes",))
        return MagicMock(xml="<ok/>")

    def commit(self, **kwargs):
        self.calls.append(("commit",))
        return MagicMock(xml="<ok/>")

    def close_session(self):
        self.calls.append(("close-session",))


class TestCompositeStatsIntegration:
    def test_netconf_session_pipeline_and_orphaned_lock_reclaim(self):
        collector = StatsCollector()
        fake_nc = _FakeNetconfSession(fail_on_validate=True)

        reg = DeviceRegistryService()
        reg.register_device(
            "nc-dev",
            AdhocEndpointDTO(target="172.20.20.4:830", protocol="netconf", username="u", password="p"),
        )

        with patch("specs.netconf_client.manager") as mock_mgr:
            mock_mgr.connect.return_value = fake_nc
            sess_mgr = DeviceSessionManager(
                device_registry=reg,
                default_ttl_seconds=10.0,
                stats_recorder=collector,
            )
            op_svc = DeviceOperationService(
                device_registry=reg,
                session_manager=sess_mgr,
                stats_recorder=collector,
            )

            sess = sess_mgr.create_session(owner_id="alice", device_id="nc-dev")

            # Pipeline that fails on validate and aborts subsequent commit
            req = ExecutionRequestDTO(
                device_id="nc-dev",
                operations=[
                    OperationStepDTO(op_type="lock", datastore="candidate"),
                    OperationStepDTO(
                        op_type="edit_config",
                        datastore="candidate",
                        xml_payload="<interfaces/>",
                        options={"commit": False},
                    ),
                    OperationStepDTO(op_type="validate", datastore="candidate"),
                    OperationStepDTO(op_type="commit"),
                ],
                continue_on_error=False,
            )
            res = op_svc.execute_in_session(sess.session_id, req, owner_id="alice")
            assert res.success is False
            assert res.stats is not None
            assert res.stats.connection_reused is True
            assert res.stats.connect_duration_ms == 0.0
            assert res.stats.operations_succeeded == 2
            assert res.stats.operations_failed == 1
            assert res.stats.operations_aborted == 1

            # Explicit discard-changes inside session
            discard_req = ExecutionRequestDTO(
                device_id="nc-dev",
                operations=[OperationStepDTO(op_type="discard_changes")],
            )
            res_discard = op_svc.execute_in_session(sess.session_id, discard_req, owner_id="alice")
            assert res_discard.success is True
            assert res_discard.stats.rollbacks.get("discard_changes") == 1

            # Expire session while candidate lock is still held -> forced unlock (no discard-changes)
            sess_mgr.sweep_expired_sessions(now=sess.expires_at + 5.0)

        snap = StatsService(collector).get_snapshot()
        assert snap.operations.aborted == 1
        assert snap.operations.netconf_rollbacks.get("discard_changes") == 1
        assert snap.operations.netconf_rollbacks.get("forced_unlock") == 1
        assert snap.sessions.orphaned_lock_reclaims_total == 1
        assert snap.sessions.operations_per_session_avg >= 1.0
        # Verify _cleanup_record issued unlock without an automatic discard-changes after discard_req
        op_names = [c[0] for c in fake_nc.calls]
        last_discard_idx = len(op_names) - 1 - op_names[::-1].index("discard-changes")
        assert "unlock" in op_names[last_discard_idx + 1:]

    def test_gnmi_streaming_backpressure_and_event_kind_classification(self):
        collector = StatsCollector()
        reg = DeviceRegistryService()
        sub_svc = SubscriptionService(device_registry=reg, stats_recorder=collector)

        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.execute_subscribe.return_value = iter([
            StreamEvent(protocol="gnmi", timestamp=time.time(), raw_payload={"a": 1}, event_kind="update"),
            StreamEvent(protocol="gnmi", timestamp=time.time(), raw_payload={"b": 2}, event_kind="delete"),
            StreamEvent(protocol="gnmi", timestamp=time.time(), raw_payload=None, is_sync_marker=True, event_kind="sync_response"),
            StreamEvent(protocol="gnmi", timestamp=time.time(), raw_payload={"c": 3}, event_kind="update"),
            StreamEvent(protocol="gnmi", timestamp=time.time(), raw_payload={"d": 4}, event_kind="update"),
        ])

        req = ExecutionRequestDTO(
            adhoc_endpoint=AdhocEndpointDTO(target="172.20.20.2:57401", protocol="gnmi", insecure=True),
            operations=[
                OperationStepDTO(
                    op_type="subscribe",
                    paths=["/interfaces"],
                    telemetry=TelemetryPolicyDTO(mode=DeliveryModeDTO.PERIODIC, sample_interval_sec=1.0),
                )
            ],
        )

        with patch("managers.subscribe_session.ClientFactory.get_client", return_value=fake_client):
            sub_id = sub_svc.start_subscription(req, queue_size=2, owner_id="alice")
            # Wait for background worker thread to finish consuming 5 events
            managed = sub_svc._get_managed(sub_id)
            managed.thread.join(timeout=2.0)

        sub_stats = StatsService(collector).get_subscription_stats(sub_id, is_admin=True)
        assert sub_stats.events_total == 5
        assert sub_stats.events_by_kind == {"update": 3, "delete": 1, "sync_response": 1}
        assert sub_stats.time_to_sync_ms is not None
        assert sub_stats.queue_depth_peak == 2
        assert sub_stats.events_dropped.get("queue_full") == 3
        assert sub_stats.disconnect_reason == "server_eof"

    def test_schema_db_cache_hit_miss_and_validator_metrics(self, tmp_path):
        collector = StatsCollector()
        db = SchemaDB(cache_dir=tmp_path / "cache", stats_recorder=collector)

        # First lookup misses
        assert db.find_module("ietf-interfaces", revision="2018-02-20") is None

        # Store and look up again -> hits disk_db
        db.store_module_content(
            name="ietf-interfaces",
            revision="2018-02-20",
            content="module ietf-interfaces { yang-version 1.1; }",
            source="local_dir",
            verify_with_libyang=False,
        )
        found = db.find_module("ietf-interfaces", revision="2018-02-20")
        assert found is not None and found.exists()
        db.close()

        # Per-call validator isolation
        validator = SchemaValidator(stats_recorder=collector)
        fake_ctx = MagicMock()
        fake_node = MagicMock()
        fake_node.config_false.return_value = False
        fake_ctx.find_path.return_value = [fake_node]
        validator.validate_path("/ietf-interfaces:interfaces", fake_ctx)

        snap = StatsService(collector).get_snapshot()
        assert snap.schemas.cache_misses.get("local_dir") == 1
        assert snap.schemas.cache_hits.get("disk_db") == 1
        assert snap.schemas.cache_hit_ratio == 0.5
        assert snap.schemas.validations_passed == 1
        assert snap.schemas.validations_failed == 0

    def test_schema_manager_double_compile_hits_memory_context(self, tmp_path):
        pytest.importorskip("libyang")
        collector = StatsCollector()
        SchemaManager.reset_instance()
        try:
            sm = SchemaManager(global_cache_dir=tmp_path / "sm_cache")
            svc = SchemaCatalogService(schema_manager=sm, stats_recorder=collector)
            _ = svc

            ctx1 = sm.get_or_create_context(profile="test-vendor-profile")
            ctx2 = sm.get_or_create_context(profile="test-vendor-profile")
            assert ctx1 is ctx2

            snap = StatsService(collector).get_snapshot()
            assert snap.schemas.compile_latency.count == 1
            assert snap.schemas.cache_hits.get("memory_context") == 1
            assert snap.schemas.profiles_loaded == 1
        finally:
            SchemaManager.reset_instance()
