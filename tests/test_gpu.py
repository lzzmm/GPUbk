import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bk.config import Config
from bk.gpu import snapshot


class GpuSnapshotTests(unittest.TestCase):
    def test_simulation_file_supplies_device_and_process_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
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

    def test_invalid_simulation_file_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu-sim.json"
            path.write_text("not-json", encoding="utf-8")
            config = Config(data_dir=Path(tmp), gpu_count=2)

            with patch.dict(os.environ, {"BK_GPU_SIM_FILE": str(path)}):
                devices = snapshot(config)

            self.assertEqual([device.index for device in devices], [0, 1])
            self.assertTrue(all(device.source == "none" for device in devices))


if __name__ == "__main__":
    unittest.main()
