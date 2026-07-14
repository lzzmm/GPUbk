import argparse
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location(
        "gpubk_cluster_acceptance",
        ROOT / "tools" / "cluster_acceptance.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ACCEPTANCE = load_script()


def fake_wheel(directory: Path, *, name: str = "gpubk", version: str = "1.2.3") -> Path:
    path = directory / f"{name}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
    return path


class ClusterAcceptanceTests(unittest.TestCase):
    def test_remote_programs_compile_and_dry_run_is_non_networked(self):
        compile(ACCEPTANCE.REMOTE_SETUP, "setup", "exec")
        compile(ACCEPTANCE.REMOTE_INSTALL, "install", "exec")
        compile(ACCEPTANCE.REMOTE_CLEANUP, "cleanup", "exec")
        output = io.StringIO()
        with (
            mock.patch.object(ACCEPTANCE.shutil, "which", return_value="/usr/bin/tool"),
            redirect_stdout(output),
        ):
            self.assertEqual(ACCEPTANCE.main(["gpu-a", "gpu-b", "--dry-run"]), 0)
        self.assertIn("production NVML and ledgers are not used", output.getvalue())

    def test_target_and_ssh_safety_policy(self):
        self.assertEqual(ACCEPTANCE.target_value("user@gpu-a"), "user@gpu-a")
        with self.assertRaises(argparse.ArgumentTypeError):
            ACCEPTANCE.target_value("gpu-a;id")
        with self.assertRaises(argparse.ArgumentTypeError):
            ACCEPTANCE.ssh_option("StrictHostKeyChecking=no")
        target = ACCEPTANCE.SshTarget("gpu-a", ("ProxyJump=bastion",))
        command = target.ssh_argv()
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ProxyJump=bastion", command)

    def test_candidate_wheel_must_be_gpubk_with_a_safe_name(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            wheel = fake_wheel(directory)
            self.assertEqual(ACCEPTANCE.wheel_metadata(wheel), ("gpubk", "1.2.3"))
            other = fake_wheel(directory, name="other")
            with self.assertRaisesRegex(ACCEPTANCE.ClusterAcceptanceError, "not GPUBK"):
                ACCEPTANCE.wheel_metadata(other)

    def test_remote_install_rejects_digest_mismatch_before_creating_venv(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            stage = Path(raw_directory)
            wheel = fake_wheel(stage)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    ACCEPTANCE.REMOTE_INSTALL,
                    str(stage),
                    wheel.name,
                    "0" * 64,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("candidate wheel digest mismatch", result.stderr)
            self.assertFalse((stage / "venv").exists())

    def test_catalog_is_private_and_contains_only_bounded_endpoints(self):
        target = ACCEPTANCE.SshTarget("user@gpu-a", ())
        node = ACCEPTANCE.RemoteNode(
            "node-1",
            target,
            "/home/user/.cache/gpubk/cluster-acceptance/run",
            "/home/user/.cache/gpubk/cluster-acceptance/run/bk-node",
            "a" * 20,
            "1.2.3",
            {"uid": 1001, "username": "user"},
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "cluster.json"
            ACCEPTANCE.write_catalog(path, [node])
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(document["nodes"][0]["node_id"], "a" * 20)
            self.assertNotIn("actor", str(document))

    def test_cluster_exercise_checks_two_nodes_and_idempotent_replay(self):
        status = {
            "nodes": [
                {"available": True, "context": {"reservations": []}},
                {"available": True, "context": {"reservations": []}},
            ]
        }
        recommendation = {"selected_node": "node-1"}

        def booking(node, reservation_id):
            return {
                "node": {"name": node},
                "result": {
                    "reservation": {
                        "id": reservation_id,
                        "short_id": reservation_id[:8],
                    }
                },
            }

        first = booking("node-1", "reservation-a")
        second = booking("node-2", "reservation-b")
        final = {
            "nodes": [
                {"context": {"reservations": []}},
                {"context": {"reservations": []}},
            ]
        }
        side_effect = [status, recommendation, first, second, first, "cancelled", "cancelled", final]
        with mock.patch.object(ACCEPTANCE, "run_client", side_effect=side_effect) as client:
            result = ACCEPTANCE.exercise_cluster(
                Path("/tmp/bk"),
                Path("/tmp/cluster.json"),
                [mock.Mock(), mock.Mock()],
            )
        self.assertEqual(result["replay"]["node"]["name"], "node-1")
        self.assertEqual(client.call_count, 8)

    def test_failed_run_still_writes_a_private_report(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            output = Path(raw_directory) / "reports"
            with (
                mock.patch.object(ACCEPTANCE.shutil, "which", return_value="/usr/bin/tool"),
                mock.patch.object(
                    ACCEPTANCE,
                    "candidate_wheel",
                    side_effect=ACCEPTANCE.ClusterAcceptanceError("build failed"),
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(ACCEPTANCE.ClusterAcceptanceError, "build failed"),
            ):
                ACCEPTANCE.main(
                    ["gpu-a", "gpu-b", "--output-dir", str(output)]
                )
            reports = list(output.glob("cluster-*.json"))
            self.assertEqual(len(reports), 1)
            payload = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["result"], "fail")
            self.assertEqual(payload["error"], "build failed")
            self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
