import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bk.config import Config
from bk.diagnostics import probes_ready, run_deployment_probes
from bk.gpu import GpuSnapshot


class DeploymentDiagnosticsTests(unittest.TestCase):
    def test_preflight_verifies_storage_lock_and_nvml_without_leaving_probe_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            config = Config(data_dir=data_dir, gpu_count=2)
            devices = [
                GpuSnapshot(
                    0,
                    "gpu0",
                    memory_total_mb=24000,
                    source="nvml",
                    device_uuid="GPU-00000000-0000-0000-0000-000000000000",
                ),
                GpuSnapshot(
                    1,
                    "gpu1",
                    memory_total_mb=24000,
                    source="nvml",
                    device_uuid="GPU-00000000-0000-0000-0000-000000000001",
                ),
            ]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            self.assertTrue(probes_ready(checks), checks)
            self.assertEqual(
                [item["name"] for item in checks],
                ["data-directory", "atomic-replace", "process-lock", "disk-space", "gpu-telemetry"],
            )
            self.assertEqual(list(data_dir.glob(".gpubk-probe-*")), [])

    def test_simulation_is_reported_as_warning_not_real_gpu_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp) / "data", gpu_count=1)
            devices = [GpuSnapshot(0, "sim", source="simulation")]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            gpu = next(item for item in checks if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "warn")
            self.assertFalse(probes_ready(checks))

    def test_configured_gpu_count_must_match_detected_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp) / "data", gpu_count=2)
            devices = [GpuSnapshot(0, "gpu0", memory_total_mb=24000, source="nvml")]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            gpu = next(item for item in checks if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "fail")
            self.assertIn("topology", gpu["message"])
            self.assertEqual(gpu["configured_device_count"], 2)
            self.assertEqual(gpu["indices"], [0])
            self.assertEqual(gpu["expected_indices"], [0, 1])
            self.assertFalse(probes_ready(checks))

    def test_nvml_requires_usable_memory_capacity_for_every_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp) / "data", gpu_count=1)
            devices = [GpuSnapshot(0, "gpu0", memory_total_mb=0, source="nvml")]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            gpu = next(item for item in checks if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "fail")
            self.assertEqual(gpu["invalid_memory_indices"], [0])

    def test_nvml_requires_process_telemetry_for_deployment_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp) / "data", gpu_count=1)
            devices = [
                GpuSnapshot(
                    0,
                    "gpu0",
                    memory_total_mb=24000,
                    source="nvml",
                    process_telemetry_available=False,
                )
            ]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            gpu = next(item for item in checks if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "fail")
            self.assertEqual(gpu["process_telemetry_unavailable_indices"], [0])

    def test_nvml_without_per_process_utilization_is_not_strictly_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp) / "data", gpu_count=1)
            devices = [
                GpuSnapshot(
                    0,
                    "gpu0",
                    memory_total_mb=24000,
                    source="nvml",
                    process_telemetry_available=True,
                    process_utilization_available=False,
                    device_uuid="GPU-00000000-0000-0000-0000-000000000000",
                )
            ]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            gpu = next(item for item in checks if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "warn")
            self.assertEqual(gpu["process_utilization_unavailable_indices"], [0])
            self.assertFalse(probes_ready(checks))

    def test_nvml_requires_stable_identifiers_for_scheduled_command_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp) / "data", gpu_count=1)
            devices = [
                GpuSnapshot(0, "gpu0", memory_total_mb=24000, source="nvml")
            ]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            gpu = next(item for item in checks if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "fail")
            self.assertEqual(gpu["stable_identifier_unavailable_indices"], [0])
            self.assertEqual(gpu["stable_device_identifiers"], [False])
            self.assertFalse(probes_ready(checks))

    def test_nvidia_smi_fallback_with_matching_topology_remains_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp) / "data", gpu_count=1)
            devices = [
                GpuSnapshot(0, "gpu0", memory_total_mb=24000, source="nvidia-smi")
            ]

            with mock.patch("bk.diagnostics.snapshot", return_value=devices):
                checks = run_deployment_probes(config)

            gpu = next(item for item in checks if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "warn")
            self.assertFalse(probes_ready(checks))

    def test_wrong_existing_directory_mode_blocks_storage_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            data_dir.mkdir(mode=0o755)
            data_dir.chmod(0o755)
            config = Config(data_dir=data_dir, dir_mode=0o700)

            with mock.patch(
                "bk.diagnostics.snapshot",
                return_value=[GpuSnapshot(0, "gpu0", source="nvml")],
            ):
                checks = run_deployment_probes(config)

            self.assertEqual(checks[0]["status"], "fail")
            self.assertEqual(checks[0]["actual_mode"], "0755")
            self.assertEqual(checks[1]["message"], "data directory is not ready")

    def test_atomic_probe_rejects_missing_setgid_group_inheritance(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            config = Config(
                data_dir=data_dir,
                gpu_count=1,
                file_mode=0o660,
                dir_mode=0o2770,
            )
            device = GpuSnapshot(
                0,
                "gpu0",
                memory_total_mb=24000,
                source="nvml",
                device_uuid="GPU-00000000-0000-0000-0000-000000000000",
            )
            original_lstat = Path.lstat

            def drifted_lstat(path):
                metadata = original_lstat(path)
                if path == data_dir:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_uid=metadata.st_uid,
                        st_gid=metadata.st_gid + 1,
                    )
                return metadata

            with (
                mock.patch.object(Path, "lstat", autospec=True, side_effect=drifted_lstat),
                mock.patch("bk.diagnostics.snapshot", return_value=[device]),
            ):
                checks = run_deployment_probes(config)

            atomic = next(item for item in checks if item["name"] == "atomic-replace")
            self.assertEqual(atomic["status"], "fail")
            self.assertIn("did not inherit setgid data-directory GID", atomic["message"])
            self.assertFalse(probes_ready(checks))
            self.assertEqual(list(data_dir.glob(".gpubk-probe-*")), [])

    def test_atomic_probe_confirms_setgid_group_inheritance(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            config = Config(
                data_dir=data_dir,
                gpu_count=1,
                file_mode=0o660,
                dir_mode=0o2770,
            )
            device = GpuSnapshot(
                0,
                "gpu0",
                memory_total_mb=24000,
                source="nvml",
                device_uuid="GPU-00000000-0000-0000-0000-000000000000",
            )

            with mock.patch("bk.diagnostics.snapshot", return_value=[device]):
                checks = run_deployment_probes(config)

            atomic = next(item for item in checks if item["name"] == "atomic-replace")
            self.assertEqual(atomic["status"], "pass")
            self.assertTrue(atomic["setgid_inheritance_checked"])
            self.assertEqual(atomic["directory_gid"], atomic["file_gid"])
            self.assertTrue(probes_ready(checks), checks)

    def test_preflight_rejects_data_directory_outside_configured_storage_gid(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            data_dir.mkdir(mode=0o700)
            data_dir.chmod(0o2770)
            actual_gid = data_dir.stat().st_gid
            config = Config(
                data_dir=data_dir,
                gpu_count=1,
                file_mode=0o660,
                dir_mode=0o2770,
                storage_gid=actual_gid + 1,
            )

            with mock.patch(
                "bk.diagnostics.snapshot",
                return_value=[GpuSnapshot(0, "gpu0", source="simulation")],
            ):
                checks = run_deployment_probes(config)

            directory = checks[0]
            self.assertEqual(directory["status"], "fail")
            self.assertEqual(directory["expected_gid"], actual_gid + 1)
            self.assertEqual(directory["actual_gid"], actual_gid)
            self.assertEqual(checks[1]["message"], "data directory is not ready")
            self.assertEqual(list(data_dir.glob(".gpubk-probe-*")), [])


if __name__ == "__main__":
    unittest.main()
