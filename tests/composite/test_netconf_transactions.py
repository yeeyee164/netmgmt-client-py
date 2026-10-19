import unittest
from unittest.mock import MagicMock, patch

from config.protocol_options.netconf import NetconfOptions
from config.operations import (
    SetOperation,
    Change,
    ChangeType,
    NetconfTransactionOperation,
    TransactionType,
)
from specs.base_client import UnsupportedOperationError
from specs.netconf_client import NetconfClient
from managers.unary_worker import TransactionWorker, SequentialWorker
from ui.cmd import build_args, config_builder, parse_operation_item


class TestNetconfTransactionOptions(unittest.TestCase):
    def test_options_validation_success(self):
        opts = NetconfOptions(
            target_datastore="candidate",
            lock_target=True,
            validate_candidate=True,
            confirmed=True,
            confirm_timeout=60,
            persist="token1",
            persist_id="token2",
        )
        # Should not raise
        opts.validate()
        self.assertTrue(opts.lock_target)
        self.assertTrue(opts.validate_candidate)
        self.assertTrue(opts.confirmed)
        self.assertEqual(opts.confirm_timeout, 60)

    def test_options_validation_negative_timeout(self):
        opts = NetconfOptions(target_datastore="candidate", confirm_timeout=-5)
        with self.assertRaises(ValueError):
            opts.validate()

    def test_options_validation_zero_timeout(self):
        opts = NetconfOptions(target_datastore="candidate", confirm_timeout=0)
        with self.assertRaises(ValueError):
            opts.validate()

    def test_options_validation_confirmed_on_running_fails(self):
        opts = NetconfOptions(target_datastore="running", confirmed=True)
        with self.assertRaises(ValueError):
            opts.validate()

    def test_options_validation_validate_on_running_fails(self):
        opts = NetconfOptions(target_datastore="running", validate_candidate=True)
        with self.assertRaises(ValueError):
            opts.validate()


class TestNetconfTransactionOperation(unittest.TestCase):
    def test_operation_validation(self):
        op = NetconfTransactionOperation(
            operation=TransactionType.COMMIT,
            confirmed=True,
            confirm_timeout=30,
        )
        op.validate()
        self.assertEqual(op.operation, TransactionType.COMMIT)

    def test_operation_invalid_timeout(self):
        op = NetconfTransactionOperation(
            operation=TransactionType.COMMIT,
            confirm_timeout=-1,
        )
        with self.assertRaises(ValueError):
            op.validate()


class TestNetconfClientTransactions(unittest.TestCase):
    def setUp(self):
        self.client = NetconfClient(target="192.0.2.1:830", username="admin", password="password")
        self.mock_session = MagicMock()
        self.mock_session.server_capabilities = [
            "urn:ietf:params:netconf:base:1.1",
            "urn:ietf:params:netconf:capability:candidate:1.0",
            "urn:ietf:params:netconf:capability:validate:1.1",
            "urn:ietf:params:netconf:capability:confirmed-commit:1.1",
        ]
        self.client.session = self.mock_session

    def test_compound_transaction_success(self):
        opts = NetconfOptions(
            target_datastore="candidate",
            lock_target=True,
            validate_candidate=True,
            commit=True,
            config="<config><system/></config>",
        )
        op = SetOperation(changes=(), protocol_options=opts)

        res = self.client.execute_set(op)

        # Verify correct ordering
        self.mock_session.lock.assert_called_once_with(target="candidate")
        self.mock_session.edit_config.assert_called_once()
        self.mock_session.validate.assert_called_once_with(source="candidate")
        self.mock_session.commit.assert_called_once_with()
        self.mock_session.unlock.assert_called_once_with(target="candidate")
        self.assertEqual(res, self.mock_session.edit_config.return_value)

    def test_compound_transaction_confirmed_commit(self):
        opts = NetconfOptions(
            target_datastore="candidate",
            commit=True,
            confirmed=True,
            confirm_timeout=120,
            persist="p1",
            config="<config><system/></config>",
        )
        op = SetOperation(changes=(), protocol_options=opts)

        self.client.execute_set(op)
        self.mock_session.commit.assert_called_once_with(
            confirmed=True, timeout="120", persist="p1"
        )

    def test_compound_transaction_no_commit(self):
        opts = NetconfOptions(
            target_datastore="candidate",
            commit=False,
            lock_target=True,
            config="<config><system/></config>",
        )
        op = SetOperation(changes=(), protocol_options=opts)

        self.client.execute_set(op)
        self.mock_session.lock.assert_called_once_with(target="candidate")
        self.mock_session.edit_config.assert_called_once()
        self.mock_session.commit.assert_not_called()
        self.mock_session.unlock.assert_called_once_with(target="candidate")

    def test_compound_transaction_failure_on_edit_config_triggers_rollback(self):
        self.mock_session.edit_config.side_effect = RuntimeError("Syntax error in XML")
        opts = NetconfOptions(
            target_datastore="candidate",
            lock_target=True,
            validate_candidate=True,
            commit=True,
            config="<bad-xml>",
        )
        op = SetOperation(changes=(), protocol_options=opts)

        with self.assertRaises(RuntimeError):
            self.client.execute_set(op)

        self.mock_session.lock.assert_called_once_with(target="candidate")
        self.mock_session.discard_changes.assert_called_once()
        self.mock_session.unlock.assert_called_once_with(target="candidate")
        self.mock_session.validate.assert_not_called()
        self.mock_session.commit.assert_not_called()

    def test_compound_transaction_failure_on_validate_triggers_rollback(self):
        self.mock_session.validate.side_effect = RuntimeError("Validation failed")
        opts = NetconfOptions(
            target_datastore="candidate",
            lock_target=True,
            validate_candidate=True,
            commit=True,
            config="<config><system/></config>",
        )
        op = SetOperation(changes=(), protocol_options=opts)

        with self.assertRaises(RuntimeError):
            self.client.execute_set(op)

        self.mock_session.edit_config.assert_called_once()
        self.mock_session.validate.assert_called_once()
        self.mock_session.discard_changes.assert_called_once()
        self.mock_session.unlock.assert_called_once_with(target="candidate")
        self.mock_session.commit.assert_not_called()

    def test_compound_transaction_failure_on_commit_triggers_rollback(self):
        self.mock_session.commit.side_effect = RuntimeError("Commit rejected")
        opts = NetconfOptions(
            target_datastore="candidate",
            lock_target=True,
            commit=True,
            config="<config><system/></config>",
        )
        op = SetOperation(changes=(), protocol_options=opts)

        with self.assertRaises(RuntimeError):
            self.client.execute_set(op)

        self.mock_session.edit_config.assert_called_once()
        self.mock_session.commit.assert_called_once()
        self.mock_session.discard_changes.assert_called_once()
        self.mock_session.unlock.assert_called_once_with(target="candidate")

    def test_missing_confirmed_commit_capability(self):
        # Remove confirmed-commit capability
        self.mock_session.server_capabilities = [
            "urn:ietf:params:netconf:base:1.1",
            "urn:ietf:params:netconf:capability:candidate:1.0",
        ]
        with self.assertRaises(UnsupportedOperationError):
            self.client.commit(confirmed=True, confirm_timeout=60)

    def test_missing_validate_capability(self):
        # Remove validate capability
        self.mock_session.server_capabilities = [
            "urn:ietf:params:netconf:base:1.1",
            "urn:ietf:params:netconf:capability:candidate:1.0",
        ]
        with self.assertRaises(UnsupportedOperationError):
            self.client.validate(source="candidate")

    def test_standalone_execute_transaction_dispatch(self):
        # Lock
        op_lock = NetconfTransactionOperation(operation=TransactionType.LOCK, target_datastore="candidate")
        self.client.execute_transaction(op_lock)
        self.mock_session.lock.assert_called_with(target="candidate")

        # Unlock
        op_unlock = NetconfTransactionOperation(operation=TransactionType.UNLOCK, target_datastore="candidate")
        self.client.execute_transaction(op_unlock)
        self.mock_session.unlock.assert_called_with(target="candidate")

        # Commit
        op_commit = NetconfTransactionOperation(operation=TransactionType.COMMIT)
        self.client.execute_transaction(op_commit)
        self.mock_session.commit.assert_called_with()

        # Cancel-commit
        op_cancel = NetconfTransactionOperation(operation=TransactionType.CANCEL_COMMIT, persist_id="t1")
        self.client.execute_transaction(op_cancel)
        self.mock_session.cancel_commit.assert_called_with(persist_id="t1")

        # Discard-changes
        op_discard = NetconfTransactionOperation(operation=TransactionType.DISCARD_CHANGES)
        self.client.execute_transaction(op_discard)
        self.mock_session.discard_changes.assert_called_with()

        # Validate
        op_val = NetconfTransactionOperation(operation=TransactionType.VALIDATE, source_datastore="candidate")
        self.client.execute_transaction(op_val)
        self.mock_session.validate.assert_called_with(source="candidate")


class TestWorkersAndShortCircuit(unittest.TestCase):
    def test_sequential_worker_short_circuit_on_error(self):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.execute.side_effect = [
            "LOCK_OK",
            RuntimeError("Edit failed"),
            "SHOULD_NOT_EXECUTE",
        ]

        worker = SequentialWorker(
            target_ip="10.0.0.1",
            target_port=830,
            protocol="netconf",
            operations=(
                NetconfTransactionOperation(operation=TransactionType.LOCK),
                SetOperation(changes=()),
                NetconfTransactionOperation(operation=TransactionType.COMMIT),
            ),
        )
        worker._get_client = MagicMock(return_value=mock_client)

        results = worker.start()

        # Should only execute 2 operations, then short-circuit
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["data"], "LOCK_OK")
        self.assertIsInstance(results[1]["data"], RuntimeError)
        self.assertEqual(mock_client.execute.call_count, 2)


class TestCliAndYamlParsing(unittest.TestCase):
    def test_cli_edit_config_flags(self):
        raw_args = [
            "netconf",
            "edit-config",
            "--target", "candidate",
            "-C", "<config><test/></config>",
            "--lock",
            "--validate",
            "--confirmed",
            "--confirm-timeout", "300",
            "--persist", "my_token",
        ]
        parsed = build_args(raw_args)
        self.assertTrue(parsed.lock_target)
        self.assertTrue(parsed.validate_candidate)
        self.assertTrue(parsed.confirmed)
        self.assertEqual(parsed.confirm_timeout, 300)
        self.assertEqual(parsed.persist, "my_token")

    def test_cli_commit_subcommand(self):
        raw_args = [
            "netconf",
            "commit",
            "--confirmed",
            "--confirm-timeout", "60",
            "--persist-id", "pid1",
        ]
        parsed = build_args(raw_args)
        self.assertEqual(parsed.operation, "commit")
        self.assertTrue(parsed.confirmed)
        self.assertEqual(parsed.confirm_timeout, 60)
        self.assertEqual(parsed.persist_id, "pid1")

    def test_cli_cancel_commit_subcommand(self):
        raw_args = [
            "netconf",
            "cancel-commit",
            "--persist-id", "pid1",
        ]
        parsed = build_args(raw_args)
        self.assertEqual(parsed.operation, "cancel-commit")
        self.assertEqual(parsed.persist_id, "pid1")

    def test_cli_discard_changes_subcommand(self):
        raw_args = ["netconf", "discard-changes"]
        parsed = build_args(raw_args)
        self.assertEqual(parsed.operation, "discard-changes")

    def test_yaml_operation_entry_parsing(self):
        from config.model import Protocol
        op_lock = parse_operation_item({"lock": {"target": "candidate"}}, Protocol.NETCONF)
        self.assertIsInstance(op_lock, NetconfTransactionOperation)
        self.assertEqual(op_lock.operation, TransactionType.LOCK)

        op_commit = parse_operation_item({"commit": {"confirmed": True, "confirm_timeout": 30}}, Protocol.NETCONF)
        self.assertIsInstance(op_commit, NetconfTransactionOperation)
        self.assertEqual(op_commit.operation, TransactionType.COMMIT)
        self.assertTrue(op_commit.confirmed)
        self.assertEqual(op_commit.confirm_timeout, 30)


if __name__ == "__main__":
    unittest.main()