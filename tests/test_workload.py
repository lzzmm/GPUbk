import unittest

from bk.workload import describe_workload


class WorkloadDescriptorTests(unittest.TestCase):
    def test_python_script_is_safely_classified_without_arguments(self):
        descriptor = describe_workload(
            "/opt/venv/bin/python /secret/project/train.py --token top-secret --epochs 3"
        )

        self.assertEqual(descriptor.launcher, "python")
        self.assertEqual(descriptor.entrypoint_kind, "script")
        self.assertEqual(descriptor.kind, "training")
        self.assertEqual(descriptor.label, "train.py")
        self.assertNotIn("secret", descriptor.label)
        self.assertNotIn("token", descriptor.signature)

    def test_common_distributed_launchers_are_separate_dimensions(self):
        cases = {
            "torchrun --nproc-per-node 8 train.py": ("torchrun", "pytorch", "distributed"),
            "deepspeed finetune.py --deepspeed ds.json": ("deepspeed", "pytorch", "distributed"),
            "accelerate launch evaluate.py": ("accelerate", "unknown", "distributed"),
            "mpirun -n 8 ./solver": ("mpi", "unknown", "distributed"),
            "srun python simulation.py": ("slurm", "unknown", "distributed"),
        }

        for command, expected in cases.items():
            with self.subTest(command=command):
                descriptor = describe_workload(command)
                self.assertEqual(
                    (descriptor.launcher, descriptor.framework, descriptor.execution),
                    expected,
                )

    def test_services_notebooks_and_unknown_native_programs_do_not_fake_training(self):
        vllm = describe_workload("python -m vllm.entrypoints.openai.api_server --model private")
        notebook = describe_workload("python -m ipykernel_launcher -f /tmp/kernel.json")
        native = describe_workload("./custom_solver --case private-case")

        self.assertEqual((vllm.framework, vllm.kind), ("vllm", "inference-service"))
        self.assertEqual((notebook.launcher, notebook.kind), ("jupyter", "interactive"))
        self.assertEqual((native.launcher, native.kind), ("native", "unknown"))

    def test_managed_summary_is_high_confidence_but_still_sanitized(self):
        descriptor = describe_workload(
            "python train.py --secret value",
            "torchrun train.py (+8 args)",
        )

        self.assertEqual(descriptor.source, "managed")
        self.assertEqual(descriptor.launcher, "torchrun")
        self.assertEqual(descriptor.framework, "pytorch")
        self.assertGreaterEqual(descriptor.confidence, 90)
        self.assertEqual(descriptor.label, "torchrun train.py (+8 args)")
        self.assertNotIn("value", descriptor.label)


if __name__ == "__main__":
    unittest.main()
