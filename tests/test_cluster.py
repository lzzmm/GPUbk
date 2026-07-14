import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.cluster import (
    CLUSTER_SCHEMA_VERSION,
    ClusterConfig,
    ClusterNode,
    NodeReply,
    _aggregate_cluster_usage,
    _find_cluster_operation_node,
    _invoke,
    _invoke_idempotent_write,
    _node_command,
    load_cluster_config,
    run_cluster_cli,
    write_cluster_config,
)
from bk.models import BookingError
from bk.node_identity import stable_node_identity
from bk.timeparse import to_iso, utc_now


class ClusterTests(unittest.TestCase):
    def catalog(self, root: Path, nodes: list[dict]) -> Path:
        path = root / "cluster.json"
        path.write_text(
            json.dumps({"schema_version": CLUSTER_SCHEMA_VERSION, "nodes": nodes}),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return path

    def test_loads_safe_catalog_and_validates_local_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.catalog(
                Path(tmp),
                [
                    {
                        "name": "here",
                        "node_id": stable_node_identity()["id"],
                        "transport": "local",
                    },
                    {
                        "name": "gpu-b",
                        "node_id": "a" * 20,
                        "transport": "ssh",
                        "target": "user@gpu-b",
                        "priority": 10,
                    },
                ],
            )
            with mock.patch.dict(os.environ, {"BK_CLUSTER_CONFIG": str(path)}):
                config = load_cluster_config()
            self.assertEqual([node.name for node in config.nodes], ["here", "gpu-b"])
            self.assertEqual(config.node("gpu-b").priority, 10)

    def test_catalog_write_round_trips_principal_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cluster.json"
            node = ClusterNode(
                "remote",
                "d" * 20,
                "ssh",
                "user@remote",
                "/usr/local/bin/bk",
                4,
                9,
            )
            config = ClusterConfig(
                path,
                (node,),
                ({"id": "person", "members": [{"node_id": node.node_id, "uid": 42}]},),
            )
            write_cluster_config(config, require_root=False)
            loaded = load_cluster_config(path)
            self.assertEqual(loaded.nodes, config.nodes)
            self.assertEqual(loaded.principals, config.principals)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_catalog_round_trips_optional_history_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.catalog(
                root,
                [
                    {
                        "name": "here",
                        "node_id": stable_node_identity()["id"],
                        "transport": "local",
                    }
                ],
            )
            document = json.loads(path.read_text(encoding="utf-8"))
            document["history_root"] = "/srv/gpubk-cluster-history"
            path.write_text(json.dumps(document), encoding="utf-8")

            loaded = load_cluster_config(path)

            self.assertEqual(
                loaded.history_root,
                Path("/srv/gpubk-cluster-history"),
            )
            document["history_root"] = "relative/archive"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BookingError, "absolute safe path"):
                load_cluster_config(path)

    def test_cluster_history_aggregates_archived_global_principal(self):
        from bk.cluster_history import ArchivedUsage

        now = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        local = ClusterNode("gpu-a", "a" * 20, "local", None, "/usr/bin/bk", 0, 8)
        remote = ClusterNode("gpu-b", "b" * 20, "ssh", "gpu-b", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(
            Path("/cluster.json"),
            (local, remote),
            (
                {
                    "id": "alice",
                    "members": [
                        {"node_id": local.node_id, "uid": os.getuid()},
                        {"node_id": remote.node_id, "uid": 2001},
                    ],
                },
            ),
            Path("/archive"),
        )

        def payload(uid, seconds):
            return {
                "users": [
                    {
                        "uid": uid,
                        "username": "alice",
                        "active_gpu_seconds": seconds,
                        "reserved_gpu_seconds": seconds * 2,
                        "idle_reserved_gpu_seconds": seconds,
                        "violation_gpu_seconds": 0,
                        "sampled_gpu_seconds": seconds * 2,
                        "max_gpu_memory_mb": 1024,
                        "avg_sm_percent": 50,
                    }
                ]
            }

        archived = [
            ArchivedUsage(local.node_id, "one", now, now + timedelta(days=1), payload(os.getuid(), 60)),
            ArchivedUsage(remote.node_id, "two", now, now + timedelta(days=1), payload(2001, 120)),
        ]
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch(
                "bk.cluster_history.resolve_history_window",
                return_value=(now, now + timedelta(days=1)),
            ),
            mock.patch(
                "bk.cluster_history.load_archived_user_usage",
                return_value=(
                    archived,
                    {
                        "root": "/archive",
                        "generations": 2,
                        "chunks": 2,
                        "start_at": to_iso(now),
                        "end_at": to_iso(now + timedelta(days=1)),
                    },
                ),
            ),
            redirect_stdout(output),
        ):
            status = run_cluster_cli(["history", "--since", "1d", "--json"])

        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result["scope"], "alice")
        self.assertEqual(result["principals"][0]["active_gpu_seconds"], 180)
        self.assertEqual(len(result["principals"][0]["members"]), 2)

    def test_rejects_writable_catalog_and_unsafe_ssh_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.catalog(
                root,
                [
                    {
                        "name": "bad",
                        "node_id": "b" * 20,
                        "transport": "ssh",
                        "target": "-oProxyCommand=bad",
                    }
                ],
            )
            with (
                mock.patch.dict(os.environ, {"BK_CLUSTER_CONFIG": str(path)}),
                self.assertRaisesRegex(BookingError, "invalid SSH target"),
            ):
                load_cluster_config()
            os.chmod(path, 0o666)
            with (
                mock.patch.dict(os.environ, {"BK_CLUSTER_CONFIG": str(path)}),
                self.assertRaisesRegex(BookingError, "must not be writable"),
            ):
                load_cluster_config()

    def test_rejects_duplicate_nodes_and_ambiguous_principal_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate_nodes = self.catalog(
                root,
                [
                    {
                        "name": "same",
                        "node_id": "a" * 20,
                        "transport": "ssh",
                        "target": "gpu-a",
                    },
                    {
                        "name": "same",
                        "node_id": "b" * 20,
                        "transport": "ssh",
                        "target": "gpu-b",
                    },
                ],
            )
            with self.assertRaisesRegex(BookingError, "names must be unique"):
                load_cluster_config(duplicate_nodes)

            document = {
                "schema_version": CLUSTER_SCHEMA_VERSION,
                "nodes": [
                    {
                        "name": "gpu-a",
                        "node_id": "a" * 20,
                        "transport": "ssh",
                        "target": "gpu-a",
                    }
                ],
                "principals": [
                    {
                        "id": "first",
                        "members": [{"node_id": "a" * 20, "uid": 1001}],
                    },
                    {
                        "id": "second",
                        "members": [{"node_id": "a" * 20, "uid": 1001}],
                    },
                ],
            }
            duplicate_nodes.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BookingError, "multiple cluster principals"):
                load_cluster_config(duplicate_nodes)

    def test_ssh_command_is_noninteractive_and_shell_quotes_remote_arguments(self):
        node = ClusterNode("gpu-b", "b" * 20, "ssh", "user@gpu-b", "/opt/gpubk/bin/bk", 0, 8)
        with mock.patch(
            "bk.cluster_transport.shutil.which",
            return_value="/usr/bin/ssh",
        ):
            command, environment = _node_command(
                node,
                ["agent", "recommend", "1", "30m", "--mem", "12g; touch /tmp/x"],
            )
        self.assertIsNone(environment)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ClearAllForwardings=yes", command)
        self.assertEqual(command[-2], "user@gpu-b")
        self.assertIn("'12g; touch /tmp/x'", command[-1])

    def test_invoke_rejects_response_from_wrong_node(self):
        node = ClusterNode("gpu-b", "b" * 20, "local", None, "/usr/local/bin/bk", 0, 8)
        completed = (
            0,
            json.dumps({"node": {"id": "c" * 20}}).encode(),
            b"",
        )
        with mock.patch(
            "bk.cluster_transport._run_node_process",
            return_value=completed,
        ):
            reply = _invoke(node, ["agent", "context", "--compact"])
        self.assertIn("does not match", reply.error)

    def test_recommendation_ranks_start_then_priority_then_name(self):
        first = ClusterNode("slow-priority", "a" * 20, "ssh", "a", "/usr/bin/bk", 10, 8)
        second = ClusterNode("preferred", "b" * 20, "ssh", "b", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (first, second))
        payload = {
            "node": {"id": "ignored"},
            "generated_at": to_iso(utc_now()),
            "available": True,
            "recommendation": {
                "gpus": [0],
                "start_at": "2030-01-01T00:00:00Z",
                "end_at": "2030-01-01T00:30:00Z",
            },
        }
        replies = [
            NodeReply(first, payload, None),
            NodeReply(second, payload, None),
        ]
        output = StringIO()
        with mock.patch("bk.cluster._parallel", return_value=replies), redirect_stdout(output):
            with mock.patch("bk.cluster.load_cluster_config", return_value=config):
                status = run_cluster_cli(["recommend", "1", "30m"])
        self.assertEqual(status, 0)
        rows = [line for line in output.getvalue().splitlines() if line.startswith(("preferred", "slow-priority"))]
        self.assertTrue(rows[0].startswith("preferred"))

    def test_unreachable_node_does_not_block_a_healthy_recommendation(self):
        offline = ClusterNode("offline", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        healthy = ClusterNode("healthy", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (offline, healthy))
        replies = [
            NodeReply(
                offline,
                None,
                "connection failed",
                error_code="transport",
            ),
            NodeReply(
                healthy,
                {
                    "node": {"id": healthy.node_id},
                    "generated_at": to_iso(utc_now()),
                    "available": True,
                    "recommendation": {
                        "gpus": [0],
                        "start_at": "2030-01-01T00:00:00Z",
                        "end_at": "2030-01-01T00:30:00Z",
                    },
                },
                None,
            ),
        ]
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=replies),
            redirect_stdout(output),
        ):
            status = run_cluster_cli(["recommend", "1", "30m"])
        self.assertEqual(status, 0)
        self.assertIn("healthy", output.getvalue())

    def test_stale_preflight_rejection_never_fails_over_to_another_node(self):
        first = ClusterNode("first", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        second = ClusterNode("second", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (first, second))
        capabilities = {
            "federated_node_identity": True,
            "idempotent_booking": True,
            "operation_status": True,
            "preflight_idempotent_replay": True,
        }
        replies = []
        for node in (first, second):
            replies.append(
                NodeReply(
                    node,
                    {
                        "node": {"id": node.node_id},
                        "generated_at": to_iso(utc_now()),
                        "available": True,
                        "capabilities": capabilities,
                        "recommendation": {
                            "gpus": [0],
                            "start_at": "2030-01-01T00:00:00Z",
                            "end_at": "2030-01-01T00:30:00Z",
                        },
                    },
                    None,
                )
            )
        rejected = NodeReply(
            first,
            None,
            "exclusive conflict",
            error_code="remote",
        )
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=replies),
            mock.patch(
                "bk.cluster._invoke_idempotent_write",
                return_value=rejected,
            ) as write,
            self.assertRaisesRegex(BookingError, "was rejected"),
        ):
            run_cluster_cli(["book", "1", "30m"])
        self.assertEqual(write.call_count, 1)
        self.assertIs(write.call_args.args[0], first)

    def test_implicit_recommendation_tolerates_skew_but_exact_start_rejects_it(self):
        node = ClusterNode("skewed", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        generated = utc_now() + timedelta(minutes=5)
        reply = NodeReply(
            node,
            {
                "node": {"id": node.node_id},
                "generated_at": to_iso(generated),
                "available": True,
                "recommendation": {
                    "gpus": [0],
                    "start_at": to_iso(generated + timedelta(minutes=30)),
                    "end_at": to_iso(generated + timedelta(hours=1)),
                },
            },
            None,
        )
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=[reply]),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(run_cluster_cli(["recommend", "1", "30m"]), 0)
            with self.assertRaisesRegex(BookingError, "clock skew"):
                run_cluster_cli(
                    [
                        "recommend",
                        "1",
                        "30m",
                        "--start",
                        "2030-01-01T00:00:00Z",
                    ]
                )

    def test_cluster_booking_skips_read_only_legacy_node(self):
        legacy = ClusterNode("legacy", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        current = ClusterNode("current", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (legacy, current))
        base = {
            "generated_at": to_iso(utc_now()),
            "available": True,
            "recommendation": {
                "gpus": [0],
                "start_at": "2030-01-01T00:00:00Z",
                "end_at": "2030-01-01T00:30:00Z",
            },
        }
        replies = [
            NodeReply(legacy, {**base, "node": {"id": legacy.node_id}}, None),
            NodeReply(
                current,
                {
                    **base,
                    "node": {"id": current.node_id},
                    "capabilities": {
                        "federated_node_identity": True,
                        "idempotent_booking": True,
                        "operation_status": True,
                        "preflight_idempotent_replay": True,
                    },
                },
                None,
            ),
        ]
        result = NodeReply(
            current,
            {
                "status": "created",
                "reservation": {
                    "short_id": "123456",
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": "2030-01-01T00:30:00Z",
                },
            },
            None,
        )
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=replies),
            mock.patch(
                "bk.cluster._invoke_idempotent_write",
                return_value=result,
            ) as write,
            redirect_stdout(output),
        ):
            status = run_cluster_cli(["book", "1", "30m"])
        self.assertEqual(status, 0)
        self.assertIs(write.call_args.args[0], current)
        self.assertIn("created on current", output.getvalue())

    def test_idempotent_write_probes_operation_after_timeout(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        timed_out = NodeReply(
            node,
            None,
            "timed out",
            timed_out=True,
            error_code="timeout",
        )
        recovered = NodeReply(
            node,
            {
                "kind": "operation_status",
                "found": True,
                "action": "create",
                "reservation": {"id": "reservation-id"},
            },
            None,
        )
        with mock.patch(
            "bk.cluster._invoke",
            side_effect=[timed_out, recovered],
        ) as invoke:
            reply = _invoke_idempotent_write(
                node,
                ["1", "30m", "--op-id", "operation-1", "--json"],
                "operation-1",
            )
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(reply.payload["status"], "exists")
        self.assertEqual(reply.payload["reservation"]["id"], "reservation-id")

    def test_idempotent_write_rejects_recovered_operation_of_another_kind(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        timed_out = NodeReply(
            node,
            None,
            "timed out",
            timed_out=True,
            error_code="timeout",
        )
        recovered = NodeReply(
            node,
            {
                "kind": "operation_status",
                "found": True,
                "action": "cancel",
                "reservation": {"id": "reservation-id"},
            },
            None,
        )
        with mock.patch(
            "bk.cluster._invoke",
            side_effect=[timed_out, recovered],
        ) as invoke:
            reply = _invoke_idempotent_write(
                node,
                ["1", "30m", "--op-id", "operation-1", "--json"],
                "operation-1",
                expected_action="create",
            )
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(reply.error_code, "operation-conflict")
        self.assertIn("not create", reply.error)

    def test_cluster_operation_preflight_rejects_duplicate_or_unknown_state(self):
        first = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        second = ClusterNode("gpu-b", "b" * 20, "ssh", "b", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (first, second))
        found = {
            "kind": "operation_status",
            "found": True,
            "action": "create",
            "reservation": {"id": "reservation-id"},
        }
        with mock.patch(
            "bk.cluster._parallel",
            return_value=[
                NodeReply(first, found, None),
                NodeReply(second, found, None),
            ],
        ):
            with self.assertRaisesRegex(BookingError, "multiple cluster nodes"):
                _find_cluster_operation_node(config, "operation-1")

        missing = {"kind": "operation_status", "found": False, "reservation": None}
        with mock.patch(
            "bk.cluster._parallel",
            return_value=[
                NodeReply(first, missing, None),
                NodeReply(second, None, "timed out", error_code="timeout"),
            ],
        ):
            with self.assertRaisesRegex(BookingError, "cannot safely route"):
                _find_cluster_operation_node(config, "operation-1")

    def test_usage_merges_only_explicitly_mapped_node_uids(self):
        first = ClusterNode("a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        second = ClusterNode("b", "b" * 20, "ssh", "b", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(
            Path("/cluster.json"),
            (first, second),
            (
                {
                    "id": "person",
                    "members": [
                        {"node_id": first.node_id, "uid": 10},
                        {"node_id": second.node_id, "uid": 20},
                    ],
                },
            ),
        )
        replies = [
            NodeReply(
                first,
                {
                    "users": [
                        {
                            "uid": 10,
                            "username": "same",
                            "active_gpu_seconds": 60,
                            "reserved_gpu_seconds": 120,
                            "sampled_gpu_seconds": 120,
                            "avg_sm_percent": 50,
                        },
                        {"uid": 30, "username": "duplicate-name", "active_gpu_seconds": 5},
                    ]
                },
                None,
            ),
            NodeReply(
                second,
                {
                    "users": [
                        {
                            "uid": 20,
                            "username": "other",
                            "active_gpu_seconds": 180,
                            "reserved_gpu_seconds": 240,
                            "sampled_gpu_seconds": 240,
                            "avg_sm_percent": 25,
                        },
                        {"uid": 30, "username": "duplicate-name", "active_gpu_seconds": 7},
                    ]
                },
                None,
            ),
        ]
        groups = _aggregate_cluster_usage(config, replies)
        mapped = next(item for item in groups if item["id"] == "person")
        self.assertEqual(mapped["active_gpu_seconds"], 240)
        self.assertEqual(mapped["nodes"], ["a", "b"])
        self.assertEqual(mapped["avg_sm_percent"], 33.333)
        unmapped = [item for item in groups if not item["mapped"]]
        self.assertEqual(len(unmapped), 2)

    def test_status_json_is_versioned_and_node_qualified(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        reply = NodeReply(
            node,
            {
                "node": {"id": node.node_id},
                "generated_at": to_iso(utc_now()),
                "actor": {"uid": 10, "username": "user"},
                "policy": {"gpu_count": 1},
                "reservations": [],
            },
            None,
        )
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=[reply]),
            redirect_stdout(output),
        ):
            status = run_cluster_cli(["status", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["schema_version"], CLUSTER_SCHEMA_VERSION)
        self.assertEqual(payload["nodes"][0]["node_id"], node.node_id)


if __name__ == "__main__":
    unittest.main()
