import argparse
import importlib.util
import io
import json
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bk import __version__


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LOCAL = load_script("gpubk_remote_acceptance", ROOT / "tools" / "remote_acceptance.py")
REMOTE = load_script("gpubk_acceptance_remote", ROOT / "tools" / "acceptance_remote.py")


def fake_wheel(directory: Path, *, name: str = "gpubk", version: str = "1.2.3") -> Path:
    normalized = name.replace("-", "_")
    path = directory / f"{normalized}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{normalized}-{version}.dist-info/METADATA", metadata)
    return path


class LocalAcceptanceRunnerTests(unittest.TestCase):
    def test_reads_source_version_without_importing_package(self):
        self.assertEqual(LOCAL.source_version(), __version__)

    def test_ssh_target_accepts_alias_and_user_host_only(self):
        self.assertEqual(LOCAL.validate_target("5090-2"), "5090-2")
        self.assertEqual(LOCAL.validate_target("chenyuhan@5090-2"), "chenyuhan@5090-2")
        for value in (
            "-oProxyCommand=x",
            "host name",
            "user@@host",
            "host/path",
            "host;id",
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                LOCAL.validate_target(value)

    def test_verifies_exact_gpubk_wheel_metadata(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            wheelhouse = Path(raw_directory)
            wheel = fake_wheel(wheelhouse)

            self.assertEqual(
                LOCAL.verify_wheelhouse(wheelhouse, "1.2.3", verify_index=False),
                [wheel],
            )
            with self.assertRaisesRegex(LOCAL.AcceptanceError, "expected 9.9.9"):
                LOCAL.verify_wheelhouse(wheelhouse, "9.9.9", verify_index=False)

    def test_command_timeout_becomes_a_short_actionable_error(self):
        expired = LOCAL.subprocess.TimeoutExpired(["python", "-m", "pip"], 3)
        with mock.patch.object(LOCAL.subprocess, "run", side_effect=expired):
            with self.assertRaisesRegex(
                LOCAL.AcceptanceError,
                "timed out after 3s.*check local PyPI connectivity",
            ):
                LOCAL.run_checked(["python", "-m", "pip"], timeout=3, visible=True)

    def test_pypi_download_avoids_cache_and_bounds_network_retries(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            destination = Path(raw_directory) / "wheelhouse"
            with (
                mock.patch.object(LOCAL, "run_checked") as run_checked,
                mock.patch.object(
                    LOCAL, "verify_wheelhouse", return_value=[]
                ) as verify,
            ):
                result = LOCAL.prepare_wheelhouse(
                    destination,
                    "1.2.3",
                    None,
                    verify_index=True,
                )

            self.assertEqual(result, [])
            argv = run_checked.call_args.args[0]
            self.assertIn("--no-cache-dir", argv)
            self.assertEqual(argv[argv.index("--timeout") + 1], "20")
            self.assertEqual(argv[argv.index("--retries") + 1], "2")
            self.assertTrue(run_checked.call_args.kwargs["visible"])
            verify.assert_called_once_with(destination, "1.2.3", verify_index=True)

    def test_manifest_covers_runner_and_rejects_modified_wheel(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            stage = Path(raw_directory)
            wheelhouse = stage / "wheelhouse"
            wheelhouse.mkdir()
            wheel = fake_wheel(wheelhouse)
            runner = stage / "acceptance_remote.py"
            runner.write_text("print('runner')\n", encoding="utf-8")
            manifest = LOCAL.build_manifest("run-1", "1.2.3", runner, [wheel])
            (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            loaded = REMOTE.load_and_verify_bundle(stage, "1.2.3", "run-1")
            self.assertEqual(loaded["run_id"], "run-1")
            self.assertEqual(
                set(loaded["files"]),
                {"acceptance_remote.py", f"wheelhouse/{wheel.name}"},
            )

            with wheel.open("ab") as handle:
                handle.write(b"changed")
            with self.assertRaisesRegex(ValueError, "bundle size mismatch"):
                REMOTE.load_and_verify_bundle(stage, "1.2.3", "run-1")

    def test_report_round_trip_keeps_machine_readable_summary(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            stage = root / "stage"
            output = root / "output"
            stage.mkdir()
            output.mkdir()
            report = REMOTE.AcceptanceReport(run_id="run-1", version="1.2.3")
            report.bundle_manifest = {"schema_version": REMOTE.MANIFEST_SCHEMA}
            report.add(
                "candidate.version",
                status="pass",
                critical=True,
                summary="candidate version matched",
            )
            report.add(
                "system.worker-health",
                status="warn",
                critical=False,
                summary="optional worker is absent",
            )

            archive, digest = REMOTE.write_report(stage, report)
            self.assertEqual(REMOTE.sha256_file(archive), digest)
            payload = LOCAL.extract_report(archive, output)

            self.assertEqual(payload["schema_version"], REMOTE.SCHEMA_VERSION)
            self.assertEqual(payload["result"], "warn")
            self.assertEqual(payload["counts"]["pass"], 1)
            self.assertEqual(payload["counts"]["warn"], 1)
            self.assertFalse(payload["privacy"]["raw_ledger_included"])

    def test_report_extraction_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            archive = root / "report.tar.gz"
            output = root / "output"
            output.mkdir()
            content = b"bad"
            info = tarfile.TarInfo("../outside")
            info.size = len(content)
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.addfile(info, io.BytesIO(content))

            with self.assertRaisesRegex(
                LOCAL.AcceptanceError, "unsafe report archive member"
            ):
                LOCAL.extract_report(archive, output)

    def test_report_output_does_not_chmod_an_existing_parent(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            root.chmod(0o755)

            LOCAL.prepare_report_output(root / "private-run")

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            self.assertEqual(
                stat.S_IMODE((root / "private-run").stat().st_mode),
                0o700,
            )

    def test_dry_run_does_not_require_ssh_or_network(self):
        result = LOCAL.main(["chenyuhan@5090-2", "--dry-run"])
        self.assertEqual(result, 0)

    def test_transport_has_bounded_connection_and_keepalive_defaults(self):
        settings = LOCAL.SshSettings("host", None, None, ())
        self.assertIn("ConnectTimeout=20", settings.ssh_argv())
        self.assertIn("ServerAliveInterval=15", settings.scp_argv())

        overridden = LOCAL.SshSettings("host", None, None, ("ConnectTimeout=60",))
        self.assertNotIn("ConnectTimeout=20", overridden.ssh_argv())
        self.assertIn("ConnectTimeout=60", overridden.ssh_argv())

    def test_orchestrator_downloads_failed_report_and_cleans_remote_stage(self):
        completed = LOCAL.subprocess.CompletedProcess(["ssh"], 0)
        failed_checks = LOCAL.subprocess.CompletedProcess(["ssh"], 2)
        payload = {
            "schema_version": "gpubk.acceptance.v1",
            "result": "fail",
            "counts": {"pass": 1, "warn": 0, "fail": 1, "skip": 0},
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            output = Path(raw_directory) / "reports"
            with (
                mock.patch.object(LOCAL, "require_local_commands"),
                mock.patch.object(LOCAL, "prepare_wheelhouse", return_value=[]),
                mock.patch.object(LOCAL, "build_manifest", return_value={}),
                mock.patch.object(
                    LOCAL, "build_bundle", return_value=LOCAL.REMOTE_RUNNER
                ),
                mock.patch.object(LOCAL, "sha256_file", return_value="a" * 64),
                mock.patch.object(
                    LOCAL,
                    "run_ssh",
                    side_effect=[completed, completed, failed_checks, completed],
                ) as run_ssh,
                mock.patch.object(LOCAL, "upload_bundle"),
                mock.patch.object(
                    LOCAL,
                    "download_report",
                    return_value=(output / "report.tar.gz", payload),
                ) as download_report,
            ):
                result = LOCAL.main(["host", "--output-dir", str(output)])

            self.assertEqual(result, 2)
            self.assertEqual(run_ssh.call_count, 4)
            download_report.assert_called_once()
            self.assertIn(LOCAL.CLEANUP_CODE, run_ssh.call_args_list[-1].args[1])

    def test_orchestrator_falls_back_to_remote_pypi(self):
        completed = LOCAL.subprocess.CompletedProcess(["ssh"], 0)
        payload = {
            "schema_version": "gpubk.acceptance.v1",
            "result": "pass",
            "counts": {"pass": 1, "warn": 0, "fail": 0, "skip": 0},
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            output = Path(raw_directory) / "reports"
            with (
                mock.patch.object(LOCAL, "require_local_commands"),
                mock.patch.object(
                    LOCAL,
                    "prepare_wheelhouse",
                    side_effect=LOCAL.DownloadUnavailable("DNS unavailable"),
                ),
                mock.patch.object(
                    LOCAL,
                    "run_ssh",
                    side_effect=[completed, completed, completed],
                ) as run_ssh,
                mock.patch.object(LOCAL, "upload_bootstrap") as upload,
                mock.patch.object(
                    LOCAL,
                    "download_report",
                    return_value=(output / "report.tar.gz", payload),
                ),
            ):
                result = LOCAL.main(["host", "--output-dir", str(output)])

            self.assertEqual(result, 0)
            upload.assert_called_once()
            self.assertIn("--download-wheelhouse", run_ssh.call_args_list[1].args[1])

    def test_orchestrator_cleans_stage_after_upload_failure(self):
        completed = LOCAL.subprocess.CompletedProcess(["ssh"], 0)
        with tempfile.TemporaryDirectory() as raw_directory:
            with (
                mock.patch.object(LOCAL, "require_local_commands"),
                mock.patch.object(LOCAL, "prepare_wheelhouse", return_value=[]),
                mock.patch.object(LOCAL, "build_manifest", return_value={}),
                mock.patch.object(
                    LOCAL, "build_bundle", return_value=LOCAL.REMOTE_RUNNER
                ),
                mock.patch.object(LOCAL, "sha256_file", return_value="a" * 64),
                mock.patch.object(
                    LOCAL, "run_ssh", side_effect=[completed, completed]
                ) as run_ssh,
                mock.patch.object(
                    LOCAL,
                    "upload_bundle",
                    side_effect=LOCAL.AcceptanceError("upload interrupted"),
                ),
            ):
                with self.assertRaisesRegex(
                    LOCAL.AcceptanceError, "upload interrupted"
                ):
                    LOCAL.main(
                        ["host", "--output-dir", str(Path(raw_directory) / "reports")]
                    )

            self.assertEqual(run_ssh.call_count, 2)
            self.assertIn(LOCAL.CLEANUP_CODE, run_ssh.call_args_list[-1].args[1])


class RemoteAcceptanceRunnerTests(unittest.TestCase):
    def test_remote_download_creates_a_verifiable_manifest(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            stage = Path(raw_directory)
            runner = stage / "acceptance_remote.py"
            runner.write_text("# runner\n", encoding="utf-8")
            downloader = stage / "remote_acceptance.py"
            downloader.write_text(
                """
import hashlib

def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare_wheelhouse(destination, version, supplied, *, verify_index, python_executable):
    destination.mkdir(mode=0o700)
    wheel = destination / f'gpubk-{version}-py3-none-any.whl'
    wheel.write_bytes(b'verified wheel')
    return [wheel]

def build_manifest(run_id, version, runner, wheels):
    paths = [runner, *wheels]
    files = {}
    for path in paths:
        name = 'acceptance_remote.py' if path == runner else f'wheelhouse/{path.name}'
        files[name] = {'sha256': _digest(path), 'size': path.stat().st_size}
    return {'schema_version': 'gpubk.acceptance-bundle.v1', 'run_id': run_id,
            'version': version, 'files': files}
""".lstrip(),
                encoding="utf-8",
            )
            report = REMOTE.AcceptanceReport(run_id="run-1", version="1.2.3")

            REMOTE.download_verified_wheelhouse(
                report,
                stage,
                version="1.2.3",
                run_id="run-1",
                remote_python="python3",
            )

            manifest = REMOTE.load_and_verify_bundle(stage, "1.2.3", "run-1")
            self.assertEqual(manifest["source"], "public-pypi-on-gpu-host")
            self.assertFalse(downloader.exists())
            self.assertEqual(report.checks[0]["status"], "pass")

    def test_isolated_environment_drops_all_inherited_bk_configuration(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            with mock.patch.dict(
                REMOTE.os.environ,
                {
                    "BK_CONFIG_FILE": "/etc/gpubk/config.json",
                    "BK_ALLOCATOR_COMMAND": "unsafe allocator",
                    "UNRELATED": "kept",
                },
                clear=True,
            ):
                environment = REMOTE.isolated_environment(
                    root, root / "site", gpu_count=8
                )

            self.assertNotIn("BK_CONFIG_FILE", environment)
            self.assertNotIn("BK_ALLOCATOR_COMMAND", environment)
            self.assertEqual(environment["BK_DATA_DIR"], str(root / "isolated-data"))
            self.assertEqual(environment["BK_GPU_COUNT"], "8")
            self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
            self.assertEqual(environment["UNRELATED"], "kept")

    def test_deployment_paths_follow_effective_custom_configuration(self):
        self.assertEqual(
            REMOTE.deployment_paths(
                {
                    "config_file": "/etc/gpubk/config.json",
                    "data_dir": "/data2/shared/gpubk",
                    "broker_socket": "/run/custom-gpubk/broker.sock",
                }
            ),
            [
                "/etc/gpubk",
                "/etc/gpubk/config.json",
                "/data2/shared/gpubk",
                "/run/custom-gpubk",
                "/run/custom-gpubk/broker.sock",
            ],
        )

        with self.assertRaisesRegex(ValueError, "data_dir"):
            REMOTE.deployment_paths(
                {"config_file": "/etc/gpubk/config.json", "data_dir": "relative"}
            )


if __name__ == "__main__":
    unittest.main()
