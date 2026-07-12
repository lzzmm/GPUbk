import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bk.config import Config
from bk import gpu
from bk.gpu import (
    GpuSnapshot,
    cuda_visible_device_tokens,
    detect_gpu_count,
    has_stable_device_identifier,
    snapshot,
)


class GpuSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._nvml_state = (
            gpu._NVML_SAMPLER,
            gpu._NVML_UNAVAILABLE,
            gpu._NVML_RETRY_AT,
        )
        gpu._NVML_SAMPLER = None
        gpu._NVML_UNAVAILABLE = False
        gpu._NVML_RETRY_AT = 0.0
        gpu._IDENTITY_CACHE.clear()

    def tearDown(self):
        current = gpu._NVML_SAMPLER
        if current is not None and current is not self._nvml_state[0]:
            current.close()
        gpu._NVML_SAMPLER, gpu._NVML_UNAVAILABLE, gpu._NVML_RETRY_AT = self._nvml_state
        gpu._IDENTITY_CACHE.clear()

    def test_simulation_file_auto_detects_gpu_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text(
                json.dumps({"gpus": [{"index": 0}, {"index": 1}, {"index": 2}]}),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"BK_GPU_SIM_FILE": str(path)}, clear=True):
                self.assertEqual(detect_gpu_count(), 3)

    def test_procfs_count_avoids_hardware_process_probe(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("bk.gpu._procfs_gpu_count", return_value=8):
                with patch("bk.gpu._nvml_sampler") as nvml, patch(
                    "bk.gpu._nvidia_smi_snapshot"
                ) as nvidia_smi:
                    self.assertEqual(detect_gpu_count(), 8)

        nvml.assert_not_called()
        nvidia_smi.assert_not_called()

    def test_simulation_file_supplies_device_and_process_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "uuid": "GPU-00000000-0000-0000-0000-000000000001",
                                "name": "Sim Pro 6000",
                                "memory_used_mb": 4096,
                                "memory_total_mb": 98304,
                                "utilization_percent": 72,
                                "temperature_c": 61,
                                "processes": [
                                    {
                                        "pid": 4321,
                                        "uid": 1001,
                                        "username": "alice",
                                        "command": "python train.py",
                                        "gpu_memory_mb": 3072,
                                        "sm_utilization_percent": 68,
                                        "kind": "C",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config = Config(data_dir=Path(tmp), gpu_count=1)

            with patch.dict(os.environ, {"BK_GPU_SIM_FILE": str(path)}):
                devices = snapshot(config)

            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0].source, "simulation")
            self.assertEqual(devices[0].utilization_percent, 72)
            self.assertEqual(devices[0].processes[0].uid, 1001)
            self.assertEqual(devices[0].processes[0].sm_utilization_percent, 68)
            self.assertTrue(has_stable_device_identifier(devices[0]))

    def test_invalid_simulation_file_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text("not-json", encoding="utf-8")
            config = Config(data_dir=Path(tmp), gpu_count=2)

            with patch.dict(os.environ, {"BK_GPU_SIM_FILE": str(path)}):
                devices = snapshot(config)

            self.assertEqual([device.index for device in devices], [0, 1])
            self.assertTrue(all(device.source == "none" for device in devices))

    def test_explicit_gpu_count_limits_simulated_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text(
                json.dumps({"gpus": [{"index": 0}, {"index": 1}]}),
                encoding="utf-8",
            )
            config = Config(data_dir=Path(tmp), gpu_count=1)

            with patch.dict(os.environ, {"BK_GPU_SIM_FILE": str(path)}):
                devices = snapshot(config)

            self.assertEqual([device.index for device in devices], [0])

    def test_invalid_simulation_topology_falls_back_to_unknown_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text(
                json.dumps({"gpus": [{"index": 0}, {"index": 2}]}),
                encoding="utf-8",
            )
            config = Config(data_dir=Path(tmp), gpu_count=2)

            with patch.dict(os.environ, {"BK_GPU_SIM_FILE": str(path)}):
                devices = snapshot(config)

            self.assertEqual([device.index for device in devices], [0, 1])
            self.assertTrue(all(device.source == "none" for device in devices))

    def test_invalid_simulation_metrics_fall_back_to_unknown_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text(
                json.dumps({"gpus": [{"index": 0, "utilization_percent": 101}]}),
                encoding="utf-8",
            )
            config = Config(data_dir=Path(tmp), gpu_count=1)

            with patch.dict(os.environ, {"BK_GPU_SIM_FILE": str(path)}):
                devices = snapshot(config)

            self.assertEqual(devices, [GpuSnapshot(index=0, name="unknown")])

    def test_nvml_initialization_retries_after_backoff(self):
        sampler = Mock()
        constructor = Mock(side_effect=[RuntimeError("driver not ready"), sampler])

        with patch("bk.gpu._NvmlSampler", constructor):
            with patch("bk.gpu.time.monotonic", return_value=100.0):
                self.assertIsNone(gpu._nvml_sampler())
            with patch("bk.gpu.time.monotonic", return_value=129.9):
                self.assertIsNone(gpu._nvml_sampler())
            with patch("bk.gpu.time.monotonic", return_value=130.0):
                self.assertIs(gpu._nvml_sampler(), sampler)

        self.assertEqual(constructor.call_count, 2)
        self.assertFalse(gpu._NVML_UNAVAILABLE)
        self.assertEqual(gpu._NVML_RETRY_AT, 0.0)

    def test_snapshot_invalidates_failed_nvml_sampler_before_fallback(self):
        sampler = Mock()
        sampler.snapshots.side_effect = RuntimeError("GPU handle is stale")
        gpu._NVML_SAMPLER = sampler
        fallback = [GpuSnapshot(0, "fallback", memory_total_mb=24576, source="nvidia-smi")]
        config = Config(data_dir=Path("/tmp/gpubk-test"), gpu_count=1)

        with patch.dict(os.environ, {}, clear=True), patch(
            "bk.gpu._nvidia_smi_snapshot", return_value=fallback
        ), patch("bk.gpu.time.monotonic", return_value=50.0):
            devices = snapshot(config)

        self.assertEqual(devices, fallback)
        sampler.close.assert_called_once_with()
        self.assertIsNone(gpu._NVML_SAMPLER)
        self.assertTrue(gpu._NVML_UNAVAILABLE)
        self.assertEqual(gpu._NVML_RETRY_AT, 80.0)

    def test_partial_nvml_initialization_is_shutdown(self):
        fake_nvml = SimpleNamespace(
            nvmlInit=Mock(),
            nvmlShutdown=Mock(),
            nvmlDeviceGetCount=Mock(return_value=2),
            nvmlDeviceGetHandleByIndex=Mock(side_effect=[object(), RuntimeError("lost")]),
        )

        with patch.dict(sys.modules, {"pynvml": fake_nvml}):
            with self.assertRaisesRegex(RuntimeError, "lost"):
                gpu._NvmlSampler()

        fake_nvml.nvmlShutdown.assert_called_once_with()

    def test_nvml_zero_device_result_is_shutdown_and_rejected(self):
        fake_nvml = SimpleNamespace(
            nvmlInit=Mock(),
            nvmlShutdown=Mock(),
            nvmlDeviceGetCount=Mock(return_value=0),
        )

        with patch.dict(sys.modules, {"pynvml": fake_nvml}):
            with self.assertRaisesRegex(ValueError, "invalid device count"):
                gpu._NvmlSampler()

        fake_nvml.nvmlShutdown.assert_called_once_with()

    def test_nvml_snapshot_reports_process_capabilities_and_samples(self):
        handle = object()
        fake_nvml = SimpleNamespace(
            NVML_TEMPERATURE_GPU=0,
            NVML_VALUE_NOT_AVAILABLE=-1,
            nvmlInit=Mock(),
            nvmlShutdown=Mock(),
            nvmlDeviceGetCount=Mock(return_value=1),
            nvmlDeviceGetHandleByIndex=Mock(return_value=handle),
            nvmlDeviceGetName=Mock(return_value=b"GPU Test"),
            nvmlDeviceGetUUID=Mock(
                return_value=b"GPU-00000000-0000-0000-0000-000000000123"
            ),
            nvmlDeviceGetMemoryInfo=Mock(
                return_value=SimpleNamespace(used=1024**3, total=24 * 1024**3)
            ),
            nvmlDeviceGetUtilizationRates=Mock(return_value=SimpleNamespace(gpu=42)),
            nvmlDeviceGetTemperature=Mock(return_value=55),
            nvmlDeviceGetProcessUtilization=Mock(
                return_value=[SimpleNamespace(pid=123, timeStamp=10, smUtil=37)]
            ),
            nvmlDeviceGetComputeRunningProcesses=Mock(
                return_value=[SimpleNamespace(pid=123, usedGpuMemory=2 * 1024**3)]
            ),
            nvmlDeviceGetGraphicsRunningProcesses=Mock(return_value=[]),
        )

        with patch.dict(sys.modules, {"pynvml": fake_nvml}), patch(
            "bk.gpu._host_identity",
            return_value=gpu._HostIdentity(1001, "alice", "python train.py", "start"),
        ):
            sampler = gpu._NvmlSampler()
            try:
                devices = sampler.snapshots(1)
                repeated = sampler.snapshots(1)
            finally:
                sampler.close()

        self.assertEqual(len(devices), 1)
        self.assertTrue(devices[0].process_telemetry_available)
        self.assertTrue(devices[0].process_utilization_available)
        self.assertEqual(
            devices[0].device_uuid,
            "GPU-00000000-0000-0000-0000-000000000123",
        )
        self.assertEqual(devices[0].processes[0].sm_utilization_percent, 37)
        self.assertEqual(devices[0].processes[0].gpu_memory_mb, 2048)
        self.assertEqual(repeated[0].device_uuid, devices[0].device_uuid)
        fake_nvml.nvmlDeviceGetUUID.assert_called_once_with(handle)

    def test_nvml_snapshot_marks_failed_process_queries_as_unavailable(self):
        fake_nvml = SimpleNamespace(
            NVML_TEMPERATURE_GPU=0,
            nvmlInit=Mock(),
            nvmlShutdown=Mock(),
            nvmlDeviceGetCount=Mock(return_value=1),
            nvmlDeviceGetHandleByIndex=Mock(return_value=object()),
            nvmlDeviceGetName=Mock(return_value="GPU Test"),
            nvmlDeviceGetMemoryInfo=Mock(
                return_value=SimpleNamespace(used=0, total=24 * 1024**3)
            ),
            nvmlDeviceGetUtilizationRates=Mock(return_value=SimpleNamespace(gpu=0)),
            nvmlDeviceGetTemperature=Mock(return_value=40),
            nvmlDeviceGetProcessUtilization=Mock(side_effect=RuntimeError("unsupported")),
            nvmlDeviceGetComputeRunningProcesses=Mock(side_effect=PermissionError("denied")),
            nvmlDeviceGetGraphicsRunningProcesses=Mock(return_value=[]),
        )

        with patch.dict(sys.modules, {"pynvml": fake_nvml}):
            sampler = gpu._NvmlSampler()
            try:
                device = sampler.snapshots(1)[0]
            finally:
                sampler.close()

        self.assertFalse(device.process_telemetry_available)
        self.assertFalse(device.process_utilization_available)
        self.assertEqual(device.processes, ())

    def test_nvml_uuid_cache_retries_failure_then_reuses_success(self):
        handle = object()
        query = Mock(
            side_effect=[
                RuntimeError("temporary failure"),
                "GPU-00000000-0000-0000-0000-000000000123",
            ]
        )
        sampler = object.__new__(gpu._NvmlSampler)
        sampler.nvml = SimpleNamespace(nvmlDeviceGetUUID=query)
        sampler.device_uuids = [""]

        self.assertEqual(sampler._device_uuid(0, handle), "")
        self.assertEqual(
            sampler._device_uuid(0, handle),
            "GPU-00000000-0000-0000-0000-000000000123",
        )
        self.assertEqual(
            sampler._device_uuid(0, handle),
            "GPU-00000000-0000-0000-0000-000000000123",
        )
        self.assertEqual(query.call_count, 2)

    def test_nvidia_smi_csv_handles_quoted_names_and_unsupported_metrics(self):
        output = (
            '0, GPU-00000000-0000-0000-0000-000000000123, "NVIDIA, Special", '
            "1024, 24576, N/A, [Not Supported]\n"
        )

        with patch("bk.gpu.subprocess.check_output", return_value=output):
            devices = gpu._nvidia_smi_snapshot()

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "NVIDIA, Special")
        self.assertEqual(devices[0].memory_used_mb, 1024)
        self.assertIsNone(devices[0].utilization_percent)
        self.assertIsNone(devices[0].temperature_c)
        self.assertTrue(has_stable_device_identifier(devices[0]))

    def test_nvidia_smi_rejects_non_contiguous_indices(self):
        output = "1, GPU-00000000-0000-0000-0000-000000000123, GPU, 0, 24576, 0, 30\n"

        with patch("bk.gpu.subprocess.check_output", return_value=output):
            with self.assertRaisesRegex(ValueError, "contiguous"):
                gpu._nvidia_smi_snapshot()

    def test_nvidia_smi_missing_uuid_keeps_degraded_device_metrics(self):
        output = "0, N/A, GPU, 1024, 24576, 10, 40\n"

        with patch("bk.gpu.subprocess.check_output", return_value=output):
            devices = gpu._nvidia_smi_snapshot()

        self.assertEqual(devices[0].memory_total_mb, 24576)
        self.assertFalse(has_stable_device_identifier(devices[0]))

    def test_nvidia_smi_rejects_an_empty_success_response(self):
        with patch("bk.gpu.subprocess.check_output", return_value=""):
            with self.assertRaisesRegex(ValueError, "no GPU rows"):
                gpu._nvidia_smi_snapshot()

    def test_process_identity_keeps_uid_when_command_is_unreadable(self):
        proc_stat = SimpleNamespace(st_ino=123, st_ctime_ns=456, st_uid=1001)

        with patch("bk.gpu.Path.stat", return_value=proc_stat), patch(
            "bk.gpu.pwd.getpwuid", side_effect=KeyError
        ), patch("bk.gpu._read_process_command", return_value=""):
            identity = gpu._host_identity(4321)

        self.assertEqual(identity.uid, 1001)
        self.assertEqual(identity.username, "1001")
        self.assertEqual(identity.command, "")
        self.assertEqual(identity.start_id, "123:456")

    def test_process_command_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_dir = Path(tmp)
            (proc_dir / "cmdline").write_bytes(b"x" * (gpu.MAX_PROCESS_COMMAND_BYTES + 100))

            command = gpu._read_process_command(proc_dir)

        self.assertEqual(len(command.encode("utf-8")), gpu.MAX_PROCESS_COMMAND_BYTES)

    def test_cuda_visible_tokens_use_stable_ids_only_when_every_gpu_has_one(self):
        devices = [
            GpuSnapshot(0, "gpu0", device_uuid="GPU-aaaaaaaa-0000-0000-0000-000000000000"),
            GpuSnapshot(1, "gpu1", device_uuid="GPU-bbbbbbbb-0000-0000-0000-000000000000"),
        ]

        self.assertEqual(
            cuda_visible_device_tokens([1, 0], devices),
            (
                "GPU-bbbbbbbb-0000-0000-0000-000000000000",
                "GPU-aaaaaaaa-0000-0000-0000-000000000000",
            ),
        )

        devices[0] = GpuSnapshot(0, "gpu0", device_uuid="unsafe,device")
        self.assertEqual(cuda_visible_device_tokens([1, 0], devices), ("1", "0"))


if __name__ == "__main__":
    unittest.main()
