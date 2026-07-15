import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.admin import run_admin_cli
from bk.cluster import ClusterConfig, ClusterNode
from bk.config import Config
from bk.models import BookingError


class AdminClusterTests(unittest.TestCase):
    def local_node(self) -> ClusterNode:
        return ClusterNode(
            "gpu-a",
            "a" * 20,
            "local",
            None,
            "/usr/local/bin/bk",
            0,
            8,
        )

    def remote_node(self) -> ClusterNode:
        return ClusterNode(
            "gpu-b",
            "b" * 20,
            "ssh",
            "operator@gpu-b",
            "/usr/local/bin/bk",
            7,
            8,
        )

    def test_status_is_machine_readable_and_requires_administrator(self):
        config = ClusterConfig(Path("/etc/gpubk/cluster.json"), (self.local_node(),))
        output = StringIO()
        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=config),
            redirect_stdout(output),
        ):
            self.assertEqual(run_admin_cli(["cluster", "status", "--json"]), 0)
        document = json.loads(output.getvalue())
        self.assertEqual(document["schema_version"], "gpubk.cluster.v1")
        self.assertEqual(document["nodes"][0]["name"], "gpu-a")

        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=1001),
            mock.patch("bk.admin_cluster.load_cluster_config") as load,
            self.assertRaisesRegex(BookingError, "must run as root"),
        ):
            run_admin_cli(["cluster", "status"])
        load.assert_not_called()

    def test_add_map_and_remove_preserve_catalog_invariants(self):
        path = Path("/etc/gpubk/cluster.json")
        current = ClusterConfig(path, (self.local_node(),))
        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_admin_cli(
                    [
                        "cluster",
                        "add",
                        "gpu-b",
                        "operator@gpu-b",
                        "b" * 20,
                        "--priority",
                        "7",
                        "--yes",
                    ]
                ),
                0,
            )
        added = write.call_args.args[0]
        self.assertEqual([node.name for node in added.nodes], ["gpu-a", "gpu-b"])
        self.assertEqual(added.node("gpu-b").priority, 7)

        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=added),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_admin_cli(
                    ["cluster", "map", "alice", "gpu-b", "1002", "--yes"]
                ),
                0,
            )
        mapped = write.call_args.args[0]
        self.assertEqual(
            mapped.principals,
            (
                {
                    "id": "alice",
                    "members": [{"node_id": "b" * 20, "uid": 1002}],
                },
            ),
        )

        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=mapped),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_admin_cli(["cluster", "unmap", "gpu-b", "1002", "--yes"]),
                0,
            )
        unmapped = write.call_args.args[0]
        self.assertEqual(unmapped.principals, ())

        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=mapped),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_admin_cli(["cluster", "remove", "gpu-b", "--yes"]),
                0,
            )
        removed = write.call_args.args[0]
        self.assertEqual(removed.nodes, (self.local_node(),))
        self.assertEqual(removed.principals, ())

    def test_disable_and_enable_preserve_node_identity_mappings_and_history(self):
        path = Path("/etc/gpubk/cluster.json")
        remote = self.remote_node()
        current = ClusterConfig(
            path,
            (self.local_node(), remote),
            (
                {
                    "id": "alice",
                    "members": [{"node_id": remote.node_id, "uid": 1002}],
                },
            ),
            Path("/srv/gpubk-history"),
        )
        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_admin_cli(["cluster", "disable", "gpu-b", "--yes"]),
                0,
            )
        disabled = write.call_args.args[0]
        self.assertFalse(disabled.node("gpu-b").enabled)
        self.assertEqual(disabled.principals, current.principals)
        self.assertEqual(disabled.history_root, current.history_root)

        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=disabled),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_admin_cli(["cluster", "enable", "gpu-b", "--yes"]),
                0,
            )
        enabled = write.call_args.args[0]
        self.assertTrue(enabled.node("gpu-b").enabled)
        self.assertEqual(enabled.principals, current.principals)

    def test_status_lists_identity_members_and_unmap_rejects_unknown_pair(self):
        local = self.local_node()
        remote = self.remote_node()
        config = ClusterConfig(
            Path("/etc/gpubk/cluster.json"),
            (local, remote),
            (
                {
                    "id": "alice",
                    "members": [
                        {"node_id": local.node_id, "uid": 1001},
                        {"node_id": remote.node_id, "uid": 1002},
                    ],
                },
            ),
        )
        output = StringIO()
        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=config),
            redirect_stdout(output),
        ):
            self.assertEqual(run_admin_cli(["cluster", "status"]), 0)
        self.assertIn("alice: gpu-a:1001, gpu-b:1002", output.getvalue())

        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=config),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            self.assertRaisesRegex(BookingError, "is not mapped"),
        ):
            run_admin_cli(["cluster", "unmap", "gpu-b", "9999", "--yes"])
        write.assert_not_called()

    def test_mapping_rejects_one_node_uid_assigned_to_two_people(self):
        path = Path("/etc/gpubk/cluster.json")
        remote = self.remote_node()
        current = ClusterConfig(
            path,
            (self.local_node(), remote),
            (
                {
                    "id": "alice",
                    "members": [{"node_id": remote.node_id, "uid": 1002}],
                },
            ),
        )
        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            self.assertRaisesRegex(BookingError, "already mapped to principal alice"),
        ):
            run_admin_cli(["cluster", "map", "bob", "gpu-b", "1002", "--yes"])
        write.assert_not_called()

    def test_unconfirmed_noninteractive_update_never_writes(self):
        current = ClusterConfig(
            Path("/etc/gpubk/cluster.json"),
            (self.local_node(), self.remote_node()),
        )
        stdout = StringIO()
        stderr = StringIO()
        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
            mock.patch("bk.admin_cluster.write_cluster_config") as write,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(run_admin_cli(["cluster", "remove", "gpu-b"]), 1)
        write.assert_not_called()
        self.assertIn("pass --yes", stderr.getvalue())

    def test_up_to_date_history_keeps_human_output_and_json_schema_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_root = root / "history"
            history_root.mkdir()
            data_dir = root / "data"
            current = ClusterConfig(
                root / "cluster.json",
                (self.local_node(),),
                history_root=history_root,
            )
            end = datetime(2030, 1, 2, tzinfo=timezone.utc)
            runtime = Config(data_dir=data_dir, gpu_count=1, monitor_uid=1003)

            output = StringIO()
            with (
                mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
                mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
                mock.patch("bk.admin_cluster.load_config", return_value=runtime),
                mock.patch(
                    "bk.cluster_history.resolve_history_window",
                    return_value=(end, end),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_admin_cli(
                        [
                            "cluster",
                            "export-history",
                            "--cluster-file",
                            str(current.path),
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                output.getvalue().strip(),
                f"cluster history up-to-date: node=gpu-a through=2030-01-02 "
                f"root={history_root}",
            )

            output = StringIO()
            with (
                mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
                mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
                mock.patch("bk.admin_cluster.load_config", return_value=runtime),
                mock.patch(
                    "bk.cluster_history.resolve_history_window",
                    return_value=(end, end),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_admin_cli(
                        [
                            "cluster",
                            "export-history",
                            "--cluster-file",
                            str(current.path),
                            "--json",
                        ]
                    ),
                    0,
                )
            document = json.loads(output.getvalue())
            self.assertEqual(
                set(document),
                {
                    "schema_version",
                    "status",
                    "root",
                    "node_id",
                    "generations",
                    "files",
                    "bytes",
                },
            )

    def test_history_preview_does_not_export(self):
        root = Path("/srv/gpubk-history")
        current = ClusterConfig(
            Path("/etc/gpubk/cluster.json"),
            (self.local_node(),),
            history_root=root,
        )
        start = datetime(2030, 1, 1, tzinfo=timezone.utc)
        runtime = Config(data_dir=Path("/var/lib/gpubk"), gpu_count=1)
        with (
            mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
            mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
            mock.patch("bk.admin_cluster.load_config", return_value=runtime),
            mock.patch(
                "bk.cluster_history.resolve_history_window",
                return_value=(start, start + timedelta(days=1)),
            ),
            mock.patch("bk.cluster_history.export_cluster_history") as export,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(run_admin_cli(["cluster", "export-history"]), 1)
        export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
