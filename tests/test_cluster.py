import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.cluster import (
    CLUSTER_SCHEMA_VERSION,
    ClusterConfig,
    ClusterNode,
    NodeReply,
    _aggregate_cluster_usage,
    delete_cluster_config,
    _find_cluster_operation_node,
    _invoke,
    _invoke_idempotent_write,
    _node_command,
    _validate_shared_catalog_update,
    load_cluster_config,
    run_cluster_cli,
    run_node_cli,
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

    def test_help_does_not_require_an_installed_cluster_catalog(self):
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config") as load,
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["--help"]), 0)
        load.assert_not_called()
        self.assertIn("GPUBK cluster federation", output.getvalue())

        for command in (
            "probe",
            "status",
            "check",
            "recommend",
            "usage",
            "history",
            "edit",
            "cancel",
            "tui",
        ):
            with self.subTest(command=command):
                with (
                    mock.patch("bk.cluster.load_cluster_config") as load,
                    redirect_stdout(StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    run_cluster_cli([command, "--help"])
                self.assertEqual(raised.exception.code, 0)
                load.assert_not_called()

    def test_probe_validates_node_and_prints_reviewed_add_command(self):
        discovered = ClusterNode(
            "gpu-b",
            "b" * 20,
            "ssh",
            "gpu-b",
            "/opt/gpubk/bin/bk",
            0,
            12,
        )
        payload = {
            "kind": "context",
            "generated_at": to_iso(utc_now()),
            "software": {"version": "\x1b[31m0.2.1"},
            "node": {"id": discovered.node_id},
            "actor": {"uid": 1001, "username": "alice"},
            "policy": {"gpu_count": 8},
            "capabilities": {
                "federated_node_identity": True,
                "idempotent_booking": True,
                "operation_status": True,
                "preflight_idempotent_replay": True,
            },
        }
        output = StringIO()
        with (
            mock.patch(
                "bk.cluster.probe_ssh_node",
                return_value=NodeReply(discovered, payload, None),
            ) as probe,
            redirect_stdout(output),
        ):
            status = run_cluster_cli(
                [
                    "probe",
                    "gpu-b",
                    "gpu-b",
                    "--executable",
                    "/opt/gpubk/bin/bk",
                    "--timeout",
                    "12",
                ]
            )
        self.assertEqual(status, 0)
        probe.assert_called_once()
        text = output.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertIn("Cluster node probe: ready", text)
        self.assertIn("id=" + "b" * 20, text)
        self.assertIn(
            "sudo bk admin cluster add gpu-b gpu-b " + "b" * 20,
            text,
        )
        self.assertIn("--executable /opt/gpubk/bin/bk", text)
        self.assertIn("--timeout 12", text)

        output = StringIO()
        with (
            mock.patch(
                "bk.cluster.probe_ssh_node",
                return_value=NodeReply(discovered, payload, None),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                run_cluster_cli(
                    [
                        "probe",
                        "gpu-b",
                        "gpu-b",
                        "--executable",
                        "/opt/gpubk/bin/bk",
                        "--timeout",
                        "12",
                        "--json",
                    ]
                ),
                0,
            )
        document = json.loads(output.getvalue())
        self.assertTrue(document["ready"])
        self.assertEqual(document["node"]["id"], "b" * 20)
        self.assertEqual(
            document["add_argv"][:5],
            ["sudo", "bk", "admin", "cluster", "add"],
        )

    def test_default_probe_rejects_a_username_pinned_shared_target(self):
        with (
            mock.patch.dict(os.environ, {"BK_CLUSTER_CONFIG": ""}),
            mock.patch("bk.cluster.probe_ssh_node") as probe,
            self.assertRaisesRegex(BookingError, "must not pin an SSH username"),
        ):
            run_cluster_cli(["probe", "gpu-b", "alice@gpu-b"])
        probe.assert_not_called()

    def test_private_catalog_probe_allows_a_username_qualified_target(self):
        discovered = ClusterNode(
            "gpu-b",
            "b" * 20,
            "ssh",
            "alice@gpu-b",
            "/usr/local/bin/bk",
            0,
            8,
        )
        payload = {
            "kind": "context",
            "generated_at": to_iso(utc_now()),
            "software": {"version": "0.2.1"},
            "node": {"id": discovered.node_id},
            "actor": {"uid": 1001, "username": "alice"},
            "policy": {"gpu_count": 8},
            "capabilities": {
                "federated_node_identity": True,
                "idempotent_booking": True,
                "operation_status": True,
                "preflight_idempotent_replay": True,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {"BK_CLUSTER_CONFIG": str(Path(tmp) / "private.json")},
                ),
                mock.patch(
                    "bk.cluster.probe_ssh_node",
                    return_value=NodeReply(discovered, payload, None),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_cluster_cli(
                        ["probe", "gpu-b", "alice@gpu-b", "--json"]
                    ),
                    0,
                )
        document = json.loads(output.getvalue())
        self.assertTrue(document["ready"])
        self.assertIsNone(document["add_argv"])
        self.assertIn("private per-user catalog", document["warnings"][0])

    def test_probe_fails_closed_before_transport_for_unsafe_target(self):
        with (
            mock.patch("bk.cluster.probe_ssh_node") as probe,
            self.assertRaisesRegex(BookingError, "invalid SSH target"),
        ):
            run_cluster_cli(["probe", "gpu-b", "--", "-oProxyCommand=bad"])
        probe.assert_not_called()

    def test_probe_reports_read_only_legacy_node_without_add_command(self):
        discovered = ClusterNode(
            "legacy",
            "a" * 20,
            "ssh",
            "legacy",
            "/usr/local/bin/bk",
            0,
            8,
        )
        payload = {
            "kind": "context",
            "generated_at": to_iso(utc_now()),
            "software": {"version": "0.1.0"},
            "actor": {"uid": 1001, "username": "alice"},
            "policy": {"gpu_count": 1},
            "capabilities": {"federated_node_identity": True},
        }
        output = StringIO()
        with (
            mock.patch(
                "bk.cluster.probe_ssh_node",
                return_value=NodeReply(discovered, payload, None),
            ),
            redirect_stdout(output),
        ):
            status = run_cluster_cli(["p", "legacy", "legacy"])
        self.assertEqual(status, 3)
        self.assertIn("Cluster node probe: not ready", output.getvalue())
        self.assertIn("missing write capabilities", output.getvalue())
        self.assertNotIn("admin cluster add", output.getvalue())

    def test_missing_catalog_explains_the_first_setup_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            with (
                mock.patch.dict(os.environ, {"BK_CLUSTER_CONFIG": str(path)}),
                self.assertRaisesRegex(
                    BookingError,
                    "sudo bk admin cluster init NODE --yes",
                ),
            ):
                run_cluster_cli(["status"])

    def test_short_booking_syntax_matches_single_node_cli_modes(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        capabilities = {
            "federated_node_identity": True,
            "idempotent_booking": True,
            "operation_status": True,
            "preflight_idempotent_replay": True,
        }
        for arguments, expected_mode in (
            (["1", "30m"], "shared"),
            (["s", "1", "30m"], "shared"),
            (["x", "1", "30m"], "exclusive"),
        ):
            with self.subTest(arguments=arguments):
                reply = NodeReply(
                    node,
                    {
                        "generated_at": to_iso(utc_now()),
                        "capabilities": capabilities,
                        "recommendation": {
                            "gpus": [0],
                            "start_at": "2030-01-01T00:00:00Z",
                            "end_at": "2030-01-01T00:30:00Z",
                        },
                    },
                    None,
                )
                result = NodeReply(
                    node,
                    {
                        "status": "created",
                        "reservation": {
                            "short_id": "12345678",
                            "gpus": [0],
                            "start_at": "2030-01-01T00:00:00Z",
                            "end_at": "2030-01-01T00:30:00Z",
                        },
                    },
                    None,
                )
                with (
                    mock.patch("bk.cluster.load_cluster_config", return_value=config),
                    mock.patch(
                        "bk.cluster._parallel", return_value=[reply]
                    ) as parallel,
                    mock.patch(
                        "bk.cluster._invoke_idempotent_write", return_value=result
                    ) as write,
                    redirect_stdout(StringIO()),
                ):
                    self.assertEqual(run_cluster_cli(arguments), 0)
                recommendation = parallel.call_args.args[1]
                self.assertEqual(
                    recommendation[recommendation.index("--mode") + 1],
                    expected_mode,
                )
                booking = write.call_args.args[1]
                self.assertEqual(booking[0] == "x", expected_mode == "exclusive")

        with self.assertRaisesRegex(BookingError, "specified more than once"):
            run_cluster_cli(["x", "1", "30m", "--mode", "shared"])

    def test_invalid_short_booking_is_rejected_before_node_transport(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        for arguments, message in (
            (["1", "later"], "duration"),
            (["1", "30m", "--start", "tonight"], "isoformat"),
            (["1", "30m", "--gpu", "0,nope"], "GPU indexes"),
            (["1", "30m", "--mem", "large"], "memory"),
        ):
            with self.subTest(arguments=arguments):
                with (
                    mock.patch("bk.cluster.load_cluster_config", return_value=config),
                    mock.patch("bk.cluster._parallel") as parallel,
                    self.assertRaisesRegex(BookingError, message),
                ):
                    run_cluster_cli(arguments)
                parallel.assert_not_called()

    def test_friendly_booking_start_is_normalized_once_before_node_queries(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        normalized = datetime(2030, 1, 1, 12, 30, tzinfo=timezone.utc)
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch(
                "bk.cluster.parse_friendly_start", return_value=normalized
            ) as parse,
            mock.patch("bk.cluster._parallel", return_value=[]) as parallel,
            self.assertRaisesRegex(BookingError, "no cluster node"),
        ):
            run_cluster_cli(["rec", "1", "30m", "-t", "tomorrow 9"])
        parse.assert_called_once_with("tomorrow 9")
        self.assertEqual(
            parallel.call_args.args[1][
                parallel.call_args.args[1].index("--start") + 1
            ],
            "2030-01-01T12:30:00Z",
        )

    def test_automatic_cluster_job_preserves_command_and_skips_legacy_nodes(self):
        legacy = ClusterNode("legacy", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        current = ClusterNode("current", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (legacy, current))
        generated = to_iso(utc_now())
        placement = {
            "generated_at": generated,
            "recommendation": {
                "gpus": [0],
                "start_at": "2030-01-01T00:00:00Z",
                "end_at": "2030-01-01T00:30:00Z",
            },
        }
        base_capabilities = {
            "federated_node_identity": True,
            "idempotent_booking": True,
            "operation_status": True,
            "preflight_idempotent_replay": True,
        }
        replies = [
            NodeReply(
                legacy,
                {**placement, "capabilities": base_capabilities},
                None,
            ),
            NodeReply(
                current,
                {
                    **placement,
                    "capabilities": {
                        **base_capabilities,
                        "scheduled_jobs": True,
                        "scheduled_job_path_snapshot": True,
                        "private_job_specs": True,
                    },
                },
                None,
            ),
        ]
        result = NodeReply(
            current,
            {
                "status": "created",
                "warnings": [
                    "scheduled command worker is \x1b[31mstopped\x1b[0m\nstart it",
                    "scheduled command worker is \x1b[31mstopped\x1b[0m\nstart it",
                    "external allocator unavailable; builtin placement used",
                    42,
                ],
                "reservation": {
                    "short_id": "12345678",
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": "2030-01-01T00:30:00Z",
                },
            },
            None,
        )
        job = [
            "python",
            "train.py",
            "--json",
            "--op-id",
            "job-value",
            "--mode",
            "debug",
            "; rm -rf /",
        ]
        errors = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=replies) as parallel,
            mock.patch(
                "bk.cluster._invoke_idempotent_write", return_value=result
            ) as write,
            redirect_stdout(StringIO()),
            redirect_stderr(errors),
        ):
            self.assertEqual(run_cluster_cli(["x", "1", "30m", "--", *job]), 0)

        self.assertNotIn("--", parallel.call_args.args[1])
        self.assertIs(write.call_args.args[0], current)
        sent = write.call_args.args[1]
        separator = sent.index("--")
        self.assertEqual(sent[0], "x")
        self.assertIn("--op-id", sent[:separator])
        self.assertIn("--json", sent[:separator])
        self.assertEqual(sent[separator + 1 :], job)
        warning_lines = errors.getvalue().splitlines()
        self.assertEqual(len(warning_lines), 2)
        self.assertIn("warning [current]: scheduled command worker", warning_lines[0])
        self.assertNotIn("\x1b", warning_lines[0])
        self.assertIn("external allocator unavailable", warning_lines[1])

    def test_explicit_node_job_injects_internal_options_before_delimiter(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        context = NodeReply(
            node,
            {
                "capabilities": {
                    "federated_node_identity": True,
                    "idempotent_booking": True,
                    "operation_status": True,
                    "preflight_idempotent_replay": True,
                    "scheduled_jobs": True,
                    "scheduled_job_path_snapshot": True,
                    "private_job_specs": True,
                }
            },
            None,
        )
        result = NodeReply(
            node,
            {
                "status": "created",
                "warnings": ["scheduled command worker is not-seen"],
                "reservation": {
                    "short_id": "12345678",
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": "2030-01-01T00:30:00Z",
                },
            },
            None,
        )
        job = ["python", "train.py", "--json", "--op-id", "job-value"]
        errors = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke", return_value=context),
            mock.patch("bk.cluster.uuid.uuid4", return_value="generated-op"),
            mock.patch(
                "bk.cluster._invoke_idempotent_write", return_value=result
            ) as write,
            redirect_stdout(StringIO()),
            redirect_stderr(errors),
        ):
            self.assertEqual(run_node_cli("gpu-a", ["1", "30m", "--", *job]), 0)

        self.assertEqual(write.call_args.args[2], "generated-op")
        sent = write.call_args.args[1]
        separator = sent.index("--")
        self.assertEqual(sent[:separator].count("--op-id"), 1)
        self.assertEqual(sent[:separator].count("--json"), 1)
        self.assertEqual(sent[separator + 1 :], job)
        self.assertIn(
            "warning [gpu-a]: scheduled command worker is not-seen",
            errors.getvalue(),
        )

        output = StringIO()
        errors = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke", return_value=context),
            mock.patch("bk.cluster._invoke_idempotent_write", return_value=result),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            self.assertEqual(
                run_node_cli("gpu-a", ["1", "30m", "--json", "--", *job]),
                0,
            )
        self.assertEqual(
            json.loads(output.getvalue())["warnings"], result.payload["warnings"]
        )
        self.assertEqual(errors.getvalue(), "")

    def test_cluster_jobs_fail_closed_on_missing_capabilities(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        context = NodeReply(
            node,
            {
                "capabilities": {
                    "federated_node_identity": True,
                    "idempotent_booking": True,
                    "operation_status": True,
                    "preflight_idempotent_replay": True,
                }
            },
            None,
        )
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke", return_value=context),
            mock.patch("bk.cluster._invoke_idempotent_write") as write,
            self.assertRaisesRegex(BookingError, "scheduled_jobs"),
        ):
            run_node_cli("gpu-a", ["1", "30m", "--", "python", "train.py"])
        write.assert_not_called()

    def test_cluster_job_delimiter_errors_before_transport(self):
        with (
            mock.patch("bk.cluster.load_cluster_config") as load,
            self.assertRaisesRegex(BookingError, "job command"),
        ):
            run_cluster_cli(["1", "30m", "--"])
        load.assert_not_called()

        with (
            mock.patch("bk.cluster.load_cluster_config") as load,
            self.assertRaisesRegex(BookingError, "does not accept a job command"),
        ):
            run_cluster_cli(["rec", "1", "30m", "--", "echo", "unused"])
        load.assert_not_called()

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

    def test_create_only_catalog_publish_never_replaces_an_existing_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cluster.json"
            first = ClusterNode(
                "first",
                "a" * 20,
                "ssh",
                "first",
                "/usr/local/bin/bk",
                0,
                8,
            )
            second = ClusterNode(
                "second",
                "b" * 20,
                "ssh",
                "second",
                "/usr/local/bin/bk",
                0,
                8,
            )
            write_cluster_config(
                ClusterConfig(path, (first,)),
                require_root=False,
                create_only=True,
            )
            with self.assertRaisesRegex(BookingError, "already exists"):
                write_cluster_config(
                    ClusterConfig(path, (second,)),
                    require_root=False,
                    create_only=True,
                )
            self.assertEqual(load_cluster_config(path).nodes, (first,))

    def test_catalog_delete_refuses_a_changed_file_and_removes_the_reviewed_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cluster.json"
            first = ClusterNode(
                "first",
                "a" * 20,
                "ssh",
                "first",
                "/usr/local/bin/bk",
                0,
                8,
            )
            second = ClusterNode(
                "second",
                "b" * 20,
                "ssh",
                "second",
                "/usr/local/bin/bk",
                0,
                8,
            )
            reviewed = ClusterConfig(path, (first,))
            write_cluster_config(reviewed, require_root=False)
            with self.assertRaisesRegex(BookingError, "changed after review"):
                delete_cluster_config(
                    ClusterConfig(path, (second,)),
                    require_root=False,
                )
            self.assertTrue(path.exists())
            delete_cluster_config(reviewed, require_root=False)
            self.assertFalse(path.exists())

    def test_system_catalog_rejects_a_caller_owned_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.catalog(
                Path(tmp),
                [
                    {
                        "name": "remote",
                        "node_id": "d" * 20,
                        "transport": "ssh",
                        "target": "remote",
                    }
                ],
            )
            real_fstat = os.fstat

            def caller_owned(fd: int) -> os.stat_result:
                values = list(real_fstat(fd))
                values[4] = 12345
                return os.stat_result(values)

            with (
                mock.patch("bk.cluster.SYSTEM_CLUSTER_FILE", path),
                mock.patch("bk.cluster.os.fstat", side_effect=caller_owned),
                self.assertRaisesRegex(BookingError, "must be owned by root"),
            ):
                load_cluster_config(path)

    def test_root_catalog_write_rejects_a_username_pinned_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = ClusterNode(
                "remote",
                "d" * 20,
                "ssh",
                "alice@remote",
                "/usr/local/bin/bk",
                0,
                8,
            )
            config = ClusterConfig(Path(tmp) / "cluster.json", (node,))
            with (
                mock.patch("bk.cluster.os.geteuid", return_value=0),
                self.assertRaisesRegex(BookingError, "must not pin an SSH username"),
            ):
                write_cluster_config(config)

    def test_root_catalog_migration_only_allows_pinned_targets_to_shrink(self):
        path = Path("/etc/gpubk/cluster.json")
        first = ClusterNode(
            "gpu-a", "a" * 20, "ssh", "alice@gpu-a", "/usr/local/bin/bk", 0, 8
        )
        second = ClusterNode(
            "gpu-b", "b" * 20, "ssh", "alice@gpu-b", "/usr/local/bin/bk", 0, 8
        )
        previous = ClusterConfig(path, (first, second))
        repaired_first = ClusterNode(
            "gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/local/bin/bk", 0, 8
        )
        _validate_shared_catalog_update(
            ClusterConfig(path, (repaired_first, second)),
            previous=previous,
        )

        retargeted = ClusterConfig(
            path,
            (
                first,
                ClusterNode(
                    "gpu-b",
                    "b" * 20,
                    "ssh",
                    "bob@gpu-b",
                    "/usr/local/bin/bk",
                    0,
                    8,
                ),
            ),
        )
        for case, updated, prior in (
            ("unchanged", previous, previous),
            ("different pinned user", retargeted, previous),
            ("new root catalog", previous, None),
        ):
            with self.subTest(case=case):
                with self.assertRaisesRegex(
                    BookingError,
                    "must not pin an SSH username",
                ):
                    _validate_shared_catalog_update(updated, previous=prior)

    def test_manually_written_root_catalog_rejects_a_username_pinned_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.catalog(
                Path(tmp),
                [
                    {
                        "name": "remote",
                        "node_id": "d" * 20,
                        "transport": "ssh",
                        "target": "alice@remote",
                    }
                ],
            )
            real_fstat = os.fstat

            def root_owned(fd: int) -> os.stat_result:
                values = list(real_fstat(fd))
                values[4] = 0
                return os.stat_result(values)

            with mock.patch("bk.cluster.os.fstat", side_effect=root_owned):
                with self.assertRaisesRegex(
                    BookingError,
                    "must not pin an SSH username",
                ):
                    load_cluster_config(path)
                loaded = load_cluster_config(
                    path,
                    allow_legacy_pinned_user_for_repair=True,
                )
            self.assertEqual(loaded.node("remote").target, "alice@remote")

    def test_catalog_round_trips_disabled_node_without_changing_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cluster.json"
            node = ClusterNode(
                "maintenance",
                "d" * 20,
                "ssh",
                "user@remote",
                "/usr/local/bin/bk",
                4,
                9,
                False,
            )
            write_cluster_config(ClusterConfig(path, (node,)), require_root=False)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], CLUSTER_SCHEMA_VERSION)
            self.assertIs(document["nodes"][0]["enabled"], False)
            self.assertEqual(load_cluster_config(path).nodes, (node,))

            document["nodes"][0]["enabled"] = "no"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BookingError, "enabled must be true or false"):
                load_cluster_config(path)

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
            ArchivedUsage(
                local.node_id,
                "one",
                now,
                now + timedelta(days=1),
                payload(os.getuid(), 60),
            ),
            ArchivedUsage(
                remote.node_id, "two", now, now + timedelta(days=1), payload(2001, 120)
            ),
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
        node = ClusterNode(
            "gpu-b", "b" * 20, "ssh", "user@gpu-b", "/opt/gpubk/bin/bk", 0, 8
        )
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
        with (
            mock.patch("bk.cluster._parallel", return_value=replies),
            redirect_stdout(output),
        ):
            with mock.patch("bk.cluster.load_cluster_config", return_value=config):
                status = run_cluster_cli(["recommend", "1", "30m"])
        self.assertEqual(status, 0)
        rows = [
            line
            for line in output.getvalue().splitlines()
            if line.startswith(("preferred", "slow-priority"))
        ]
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

    def test_disabled_node_is_not_queried_or_considered_for_recommendation(self):
        disabled = ClusterNode(
            "maintenance",
            "a" * 20,
            "ssh",
            "a",
            "/usr/bin/bk",
            0,
            8,
            False,
        )
        healthy = ClusterNode("healthy", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (disabled, healthy))
        reply = NodeReply(
            healthy,
            {
                "generated_at": to_iso(utc_now()),
                "recommendation": {
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": "2030-01-01T00:30:00Z",
                },
            },
            None,
        )
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=[reply]) as parallel,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(run_cluster_cli(["recommend", "1", "30m"]), 0)
        self.assertEqual(parallel.call_args.args[0], (healthy,))

    def test_all_disabled_nodes_fail_before_any_booking_transport(self):
        disabled = ClusterNode(
            "maintenance",
            "a" * 20,
            "ssh",
            "a",
            "/usr/bin/bk",
            0,
            8,
            False,
        )
        config = ClusterConfig(Path("/cluster.json"), (disabled,))
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel") as parallel,
            self.assertRaisesRegex(BookingError, "no enabled nodes"),
        ):
            run_cluster_cli(["book", "1", "30m"])
        parallel.assert_not_called()

    def test_cluster_status_and_check_report_disabled_nodes_without_contacting_them(
        self,
    ):
        enabled = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        disabled = ClusterNode(
            "gpu-b",
            "b" * 20,
            "ssh",
            "b",
            "/usr/bin/bk",
            0,
            8,
            False,
        )
        config = ClusterConfig(Path("/cluster.json"), (enabled, disabled))
        payload = {
            "generated_at": to_iso(utc_now()),
            "software": {"version": "0.2.1"},
            "actor": {"uid": 1001, "username": "alice"},
            "policy": {
                "gpu_count": 1,
                "monitoring": {"collector": {"state": "running"}},
            },
            "gpu_advice": {"gpus": []},
            "capabilities": {
                "federated_node_identity": True,
                "idempotent_booking": True,
                "preflight_idempotent_replay": True,
                "idempotent_edit": True,
                "idempotent_cancel": True,
                "operation_status": True,
            },
        }

        def invoke(node, _argv, *, cancel_event=None):
            self.assertIs(node, enabled)
            return NodeReply(node, payload, None)

        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke", side_effect=invoke) as remote,
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["status"]), 0)
            self.assertEqual(run_cluster_cli(["check"]), 0)
        self.assertEqual(remote.call_count, 2)
        self.assertIn("disabled", output.getvalue())
        self.assertIn("Cluster check: ready", output.getvalue())
        self.assertIn("skip gpu-b", output.getvalue())

    def test_cluster_check_fails_on_missing_write_capability(self):
        node = ClusterNode("legacy", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        reply = NodeReply(
            node,
            {
                "generated_at": to_iso(utc_now()),
                "software": {"version": "0.1.0"},
                "actor": {"uid": 1001, "username": "alice"},
                "capabilities": {"federated_node_identity": True},
            },
            None,
        )
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster.query_cluster_contexts", return_value=[reply]),
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["check"]), 3)
        self.assertIn("missing write capabilities", output.getvalue())

    def test_cluster_check_jobs_requires_capabilities_and_running_worker(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        capabilities = {
            "federated_node_identity": True,
            "idempotent_booking": True,
            "preflight_idempotent_replay": True,
            "idempotent_edit": True,
            "idempotent_cancel": True,
            "operation_status": True,
            "scheduled_jobs": True,
            "scheduled_job_path_snapshot": True,
            "private_job_specs": True,
        }
        payload = {
            "generated_at": to_iso(utc_now()),
            "software": {"version": "0.2.1"},
            "actor": {"uid": 1001, "username": "alice"},
            "policy": {
                "gpu_count": 1,
                "monitoring": {"collector": {"state": "running"}},
            },
            "capabilities": capabilities,
            "worker": {"state": "not-seen", "running": False},
        }
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch(
                "bk.cluster.query_cluster_contexts",
                return_value=[NodeReply(node, payload, None)],
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["check", "--jobs"]), 3)
        self.assertIn("scheduled-command worker is not running", output.getvalue())
        self.assertIn("systemctl --user enable --now", output.getvalue())

        payload["worker"] = {"state": "running", "running": True}
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch(
                "bk.cluster.query_cluster_contexts",
                return_value=[NodeReply(node, payload, None)],
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["check", "--jobs"]), 0)
        self.assertIn("scheduled commands required", output.getvalue())

    def test_cluster_check_warns_for_pending_job_without_worker(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        payload = {
            "generated_at": to_iso(utc_now()),
            "software": {"version": "0.2.1"},
            "actor": {"uid": 1001, "username": "alice"},
            "policy": {
                "gpu_count": 1,
                "monitoring": {"collector": {"state": "running"}},
            },
            "capabilities": {
                "federated_node_identity": True,
                "idempotent_booking": True,
                "preflight_idempotent_replay": True,
                "idempotent_edit": True,
                "idempotent_cancel": True,
                "operation_status": True,
            },
            "worker": {"state": "stopped", "running": False},
            "reservations": [
                {
                    "mine": True,
                    "job": {"status": "pending"},
                }
            ],
        }
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch(
                "bk.cluster.query_cluster_contexts",
                return_value=[NodeReply(node, payload, None)],
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["check"]), 0)
        self.assertIn("scheduled command is pending", output.getvalue())
        self.assertIn("bk service install worker", output.getvalue())

    def test_status_adds_job_column_only_when_a_scheduled_command_exists(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))

        def payload(reservation):
            return {
                "generated_at": to_iso(utc_now()),
                "software": {"version": "0.2.1"},
                "actor": {"uid": 1001, "username": "alice"},
                "policy": {"gpu_count": 1},
                "reservations": [reservation],
            }

        reservation = {
            "id": "reservation-id",
            "short_id": "12345678",
            "username": "alice",
            "mode": "exclusive",
            "gpus": [0],
            "start_at": "2030-01-01T00:00:00Z",
            "end_at": "2030-01-01T00:30:00Z",
        }
        outputs = []
        for context in (
            payload(reservation),
            payload({**reservation, "job": {"status": "pending"}}),
        ):
            output = StringIO()
            with (
                mock.patch("bk.cluster.load_cluster_config", return_value=config),
                mock.patch(
                    "bk.cluster.query_cluster_contexts",
                    return_value=[NodeReply(node, context, None)],
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(run_cluster_cli(["status"]), 0)
            outputs.append(output.getvalue())
        self.assertNotIn(" Job ", outputs[0])
        self.assertIn(" Job ", outputs[1])
        self.assertIn("pending", outputs[1])

    def test_malformed_node_does_not_block_a_healthy_recommendation(self):
        malformed = ClusterNode("malformed", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        healthy = ClusterNode("healthy", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (malformed, healthy))
        generated = to_iso(utc_now())
        replies = [
            NodeReply(
                malformed,
                {
                    "generated_at": generated,
                    "recommendation": {"end_at": "2030-01-01T00:30:00Z"},
                },
                None,
            ),
            NodeReply(
                healthy,
                {
                    "generated_at": generated,
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
            self.assertEqual(run_cluster_cli(["recommend", "1", "30m"]), 0)
        self.assertIn("healthy", output.getvalue())
        self.assertIn("malformed", output.getvalue())
        self.assertIn("invalid recommendation", output.getvalue())

    def test_recommendation_json_reports_rejections_and_write_compatibility(self):
        malformed = ClusterNode("malformed", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        healthy = ClusterNode("healthy", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (malformed, healthy))
        generated = to_iso(utc_now())
        replies = [
            NodeReply(
                malformed,
                {
                    "generated_at": generated,
                    "recommendation": {"end_at": "2030-01-01T00:30:00Z"},
                },
                None,
            ),
            NodeReply(
                healthy,
                {
                    "generated_at": generated,
                    "capabilities": {
                        "federated_node_identity": True,
                        "idempotent_booking": True,
                        "operation_status": True,
                        "preflight_idempotent_replay": True,
                    },
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
            self.assertEqual(run_cluster_cli(["recommend", "1", "30m", "--json"]), 0)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["selected_node"], "healthy")
        nodes = {item["name"]: item for item in payload["nodes"]}
        self.assertEqual(
            nodes["malformed"]["rejected_reason"], "invalid recommendation"
        )
        self.assertFalse(nodes["malformed"]["write_compatible"])
        self.assertIsNone(nodes["healthy"]["rejected_reason"])
        self.assertTrue(nodes["healthy"]["write_compatible"])

    def test_recommendation_rejects_wrong_or_duplicate_gpu_placement(self):
        malformed = ClusterNode("malformed", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        healthy = ClusterNode("healthy", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (malformed, healthy))
        generated = to_iso(utc_now())
        replies = [
            NodeReply(
                malformed,
                {
                    "generated_at": generated,
                    "recommendation": {
                        "gpus": [0, 0],
                        "start_at": "2030-01-01T00:00:00Z",
                        "end_at": "2030-01-01T00:30:00Z",
                    },
                },
                None,
            ),
            NodeReply(
                healthy,
                {
                    "generated_at": generated,
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
            self.assertEqual(run_cluster_cli(["recommend", "1", "30m"]), 0)
        self.assertIn("healthy", output.getvalue())
        self.assertIn("malformed", output.getvalue())
        self.assertIn("invalid recommendation", output.getvalue())

    def test_recommendation_rejects_wrong_duration_and_request_echo(self):
        wrong_duration = ClusterNode(
            "duration", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8
        )
        wrong_echo = ClusterNode("echo", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        healthy = ClusterNode("healthy", "c" * 20, "ssh", "c", "/usr/bin/bk", 2, 8)
        config = ClusterConfig(
            Path("/cluster.json"), (wrong_duration, wrong_echo, healthy)
        )
        generated = to_iso(utc_now())

        def payload(end: str, *, count: int = 1):
            return {
                "generated_at": generated,
                "request": {
                    "count": count,
                    "duration_seconds": 1800,
                    "mode": "shared",
                    "allow_queue": False,
                    "start_at": "2030-01-01T00:00:00Z",
                },
                "recommendation": {
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": end,
                },
            }

        replies = [
            NodeReply(wrong_duration, payload("2030-01-01T00:25:00Z"), None),
            NodeReply(
                wrong_echo,
                payload("2030-01-01T00:30:00Z", count=2),
                None,
            ),
            NodeReply(healthy, payload("2030-01-01T00:30:00Z"), None),
        ]
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=replies),
            redirect_stdout(output),
        ):
            self.assertEqual(
                run_cluster_cli(
                    [
                        "recommend",
                        "1",
                        "30m",
                        "--start",
                        "2030-01-01T00:00:00Z",
                    ]
                ),
                0,
            )
        rendered = output.getvalue()
        self.assertIn("duration", rendered)
        self.assertIn("echo", rendered)
        self.assertIn("healthy", rendered)
        self.assertIn("request echo does not match", rendered)

    def test_malformed_context_fields_do_not_break_cluster_status(self):
        malformed = ClusterNode("malformed", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        healthy = ClusterNode("healthy", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (malformed, healthy))
        replies = [
            NodeReply(
                malformed,
                {
                    "generated_at": to_iso(utc_now()),
                    "software": [],
                    "policy": [],
                    "gpu_advice": {"gpus": [None, {"live": []}]},
                    "reservations": [None],
                    "actor": [],
                },
                None,
            ),
            NodeReply(
                healthy,
                {
                    "generated_at": to_iso(utc_now()),
                    "software": {"version": "0.2.1"},
                    "policy": {"gpu_count": 1},
                    "gpu_advice": {"gpus": []},
                    "reservations": [],
                    "actor": {"uid": 1001, "username": "alice"},
                },
                None,
            ),
        ]
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster.query_cluster_contexts", return_value=replies),
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["status"]), 0)
        self.assertIn("malformed", output.getvalue())
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

    def test_recommendation_selects_the_node_that_booking_can_safely_write(self):
        legacy = ClusterNode("legacy", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        current = ClusterNode("current", "b" * 20, "ssh", "b", "/usr/bin/bk", 1, 8)
        config = ClusterConfig(Path("/cluster.json"), (legacy, current))
        generated = to_iso(utc_now())
        replies = [
            NodeReply(
                legacy,
                {
                    "generated_at": generated,
                    "recommendation": {
                        "gpus": [0],
                        "start_at": "2030-01-01T00:00:00Z",
                        "end_at": "2030-01-01T00:30:00Z",
                    },
                },
                None,
            ),
            NodeReply(
                current,
                {
                    "generated_at": generated,
                    "capabilities": {
                        "federated_node_identity": True,
                        "idempotent_booking": True,
                        "operation_status": True,
                        "preflight_idempotent_replay": True,
                    },
                    "recommendation": {
                        "gpus": [0],
                        "start_at": "2030-01-01T00:30:00Z",
                        "end_at": "2030-01-01T01:00:00Z",
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
            status = run_cluster_cli(["recommend", "1", "30m", "--json"])

        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["selected_node"], "current")
        self.assertTrue(payload["selected_write_compatible"])

    def test_cluster_request_options_are_normalized_once_for_read_and_write(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        reply = NodeReply(
            node,
            {
                "generated_at": to_iso(utc_now()),
                "capabilities": {
                    "federated_node_identity": True,
                    "idempotent_booking": True,
                    "operation_status": True,
                    "preflight_idempotent_replay": True,
                },
                "recommendation": {
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": "2030-01-01T00:30:00Z",
                },
            },
            None,
        )
        result = NodeReply(
            node,
            {
                "status": "created",
                "reservation": {
                    "short_id": "12345678",
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": "2030-01-01T00:30:00Z",
                },
            },
            None,
        )
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=[reply]) as parallel,
            mock.patch(
                "bk.cluster._invoke_idempotent_write", return_value=result
            ) as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_cluster_cli(
                    ["book", "1", "30m", "5g", "--mem", "6g", "--share", "2"]
                ),
                0,
            )
        recommendation_argv = parallel.call_args.args[1]
        booking_argv = write.call_args.args[1]
        for command in (recommendation_argv, booking_argv):
            self.assertEqual(command.count("--mem"), 1)
            self.assertEqual(command[command.index("--mem") + 1], "6g")
            self.assertNotIn("5g", command)
            self.assertEqual(command[command.index("--share") + 1], "2")

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

        disabled = ClusterNode(
            second.name,
            second.node_id,
            second.transport,
            second.target,
            second.executable,
            second.priority,
            second.timeout_seconds,
            False,
        )
        config = ClusterConfig(Path("/cluster.json"), (first, disabled))
        with mock.patch(
            "bk.cluster._parallel",
            return_value=[NodeReply(first, missing, None)],
        ) as parallel:
            with self.assertRaisesRegex(BookingError, "disabled by administrator"):
                _find_cluster_operation_node(config, "operation-1")
        self.assertEqual(parallel.call_args.args[0], (first,))

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
                        {
                            "uid": 30,
                            "username": "duplicate-name",
                            "active_gpu_seconds": 5,
                        },
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
                        {
                            "uid": 30,
                            "username": "duplicate-name",
                            "active_gpu_seconds": 7,
                        },
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

    def test_usage_short_json_flags_are_local_and_forward_query_options(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        reply = NodeReply(node, {"users": []}, None)
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=[reply]) as parallel,
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["usage", "-s", "7d", "-j", "-c"]), 0)
        self.assertEqual(
            parallel.call_args.args[1],
            ["usage", "me", "-s", "7d", "--json", "--compact"],
        )
        self.assertEqual(json.loads(output.getvalue())["kind"], "cluster-usage")
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_usage_skips_disabled_nodes_and_ignores_malformed_user_values(self):
        enabled = ClusterNode("gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/bin/bk", 0, 8)
        disabled = ClusterNode(
            "gpu-b",
            "b" * 20,
            "ssh",
            "gpu-b",
            "/usr/bin/bk",
            0,
            8,
            False,
        )
        config = ClusterConfig(Path("/cluster.json"), (enabled, disabled))
        reply = NodeReply(
            enabled,
            {
                "users": [
                    None,
                    {
                        "uid": 1001,
                        "username": "alice",
                        "active_gpu_seconds": "broken",
                        "reserved_gpu_seconds": float("nan"),
                        "max_gpu_memory_mb": True,
                        "avg_sm_percent": float("inf"),
                    },
                ]
            },
            None,
        )
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=[reply]) as parallel,
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["usage", "-j"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(parallel.call_args.args[0], (enabled,))
        self.assertEqual(result["principals"][0]["active_gpu_seconds"], 0)
        self.assertFalse(result["nodes"][1]["enabled"])

    def test_cancel_accepts_stable_operation_id_and_returns_cluster_json(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        context = NodeReply(
            node,
            {
                "capabilities": {
                    "federated_node_identity": True,
                    "idempotent_cancel": True,
                    "operation_status": True,
                }
            },
            None,
        )
        result = NodeReply(
            node,
            {"status": "canceled", "reservation": {"short_id": "12345678"}},
            None,
        )
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke", return_value=context),
            mock.patch(
                "bk.cluster._invoke_idempotent_write",
                return_value=result,
            ) as write,
            redirect_stdout(output),
        ):
            self.assertEqual(
                run_cluster_cli(
                    ["cancel", "gpu-a/12345678", "--op-id", "cancel-1", "-j"]
                ),
                0,
            )
        self.assertEqual(write.call_args.args[2], "cancel-1")
        self.assertEqual(write.call_args.kwargs["expected_action"], "cancel")
        self.assertIn("cancel-1", write.call_args.args[1])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["kind"], "cluster-mutation-result")
        self.assertEqual(payload["node"], "gpu-a")
        self.assertEqual(payload["operation_id"], "cancel-1")

    def test_friendly_edit_start_is_normalized_before_remote_write(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        context = NodeReply(
            node,
            {
                "capabilities": {
                    "federated_node_identity": True,
                    "idempotent_edit": True,
                    "operation_status": True,
                }
            },
            None,
        )
        result = NodeReply(
            node,
            {"status": "updated", "reservation": {"short_id": "12345678"}},
            None,
        )
        normalized = datetime(2030, 1, 2, 1, 0, tzinfo=timezone.utc)
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke", return_value=context),
            mock.patch(
                "bk.cluster.parse_friendly_start", return_value=normalized
            ) as parse,
            mock.patch(
                "bk.cluster._invoke_idempotent_write", return_value=result
            ) as write,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                run_cluster_cli(
                    ["edit", "gpu-a/12345678", "-t", "tomorrow 9"]
                ),
                0,
            )
        parse.assert_called_once_with("tomorrow 9")
        arguments = write.call_args.args[1]
        self.assertEqual(
            arguments[arguments.index("--start") + 1],
            "2030-01-02T01:00:00Z",
        )

    def test_disabled_node_rejects_edit_and_cancel_without_transport(self):
        node = ClusterNode(
            "gpu-a",
            "a" * 20,
            "ssh",
            "gpu-a",
            "/usr/bin/bk",
            0,
            8,
            False,
        )
        config = ClusterConfig(Path("/cluster.json"), (node,))
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke") as invoke,
        ):
            with self.assertRaisesRegex(BookingError, "disabled"):
                run_cluster_cli(["edit", "gpu-a/123456", "-d", "1h"])
            with self.assertRaisesRegex(BookingError, "disabled"):
                run_cluster_cli(["cancel", "gpu-a/123456"])
        invoke.assert_not_called()

    def test_uncertain_cancel_reports_the_exact_retry_operation_id(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        context = NodeReply(
            node,
            {
                "capabilities": {
                    "federated_node_identity": True,
                    "idempotent_cancel": True,
                    "operation_status": True,
                }
            },
            None,
        )
        uncertain = NodeReply(
            node,
            None,
            "operation cancel-1 status is unknown",
            error_code="uncertain",
        )
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._invoke", return_value=context),
            mock.patch(
                "bk.cluster._invoke_idempotent_write",
                return_value=uncertain,
            ),
            self.assertRaisesRegex(BookingError, "retry.*--op-id cancel-1"),
        ):
            run_cluster_cli(["cancel", "gpu-a/12345678", "--op-id", "cancel-1"])

    def test_usage_rejects_invalid_limit_before_loading_cluster_state(self):
        with (
            mock.patch("bk.cluster.load_cluster_config") as load,
            self.assertRaisesRegex(BookingError, "--limit must be >= 1"),
        ):
            run_cluster_cli(["usage", "--limit", "0"])
        load.assert_not_called()

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
            status = run_cluster_cli(["status", "-j"])
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["schema_version"], CLUSTER_SCHEMA_VERSION)
        self.assertEqual(payload["nodes"][0]["node_id"], node.node_id)

    def test_status_human_table_shows_owner_slots_and_expected_vram(self):
        node = ClusterNode("gpu-a", "a" * 20, "ssh", "gpu-a", "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        reply = NodeReply(
            node,
            {
                "generated_at": to_iso(utc_now()),
                "actor": {"uid": 10, "username": "user"},
                "policy": {"gpu_count": 1},
                "reservations": [
                    {
                        "id": "12345678-1234-1234-1234-123456789abc",
                        "short_id": "12345678",
                        "username": "user",
                        "mine": True,
                        "mode": "shared",
                        "share_units_per_gpu": 3,
                        "share_capacity_units_per_gpu": 4,
                        "expected_memory_mb_per_gpu": 12288,
                        "gpus": [0],
                        "start_at": "2030-01-01T00:00:00Z",
                        "end_at": "2030-01-01T01:00:00Z",
                    }
                ],
            },
            None,
        )
        output = StringIO()
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=config),
            mock.patch("bk.cluster._parallel", return_value=[reply]),
            redirect_stdout(output),
        ):
            self.assertEqual(run_cluster_cli(["status"]), 0)
        rendered = output.getvalue()
        self.assertIn("Own", rendered)
        self.assertIn("Req", rendered)
        self.assertIn("VRAM", rendered)
        self.assertIn("gpu-a/12345678", rendered)
        self.assertIn("3/4", rendered)
        self.assertIn("12G", rendered)


if __name__ == "__main__":
    unittest.main()
