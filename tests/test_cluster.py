import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.cluster import (
    CLUSTER_SCHEMA_VERSION,
    ClusterConfig,
    ClusterNode,
    NodeReply,
    _aggregate_cluster_usage,
    _invoke,
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
