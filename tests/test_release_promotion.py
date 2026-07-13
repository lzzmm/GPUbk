import hashlib
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "validate_release_promotion.py"
SPEC = importlib.util.spec_from_file_location("validate_release_promotion", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PROMOTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROMOTION)


class ReleasePromotionTests(unittest.TestCase):
    def valid_run(self):
        return {
            "repository": {"full_name": "lzzmm/gpubk"},
            "head_repository": {"full_name": "lzzmm/gpubk"},
            "path": ".github/workflows/release.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "success",
        }

    def valid_jobs(self):
        return {"jobs": [{"name": "verify-testpypi", "conclusion": "success"}]}

    def write_artifact(self, root: Path):
        dist = root / "dist"
        dist.mkdir()
        wheel = dist / "gpubk-0.2.0rc1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "gpubk-0.2.0rc1.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: gpubk\nVersion: 0.2.0rc1\n",
            )
        sdist = dist / "gpubk-0.2.0rc1.tar.gz"
        sdist.write_bytes(b"source distribution")
        (root / "SHA256SUMS").write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                for path in (wheel, sdist)
            ),
            encoding="ascii",
        )
        return wheel, sdist

    def test_accepts_one_successful_main_testpypi_run(self):
        self.assertEqual(
            PROMOTION.validate_source_run(
                self.valid_run(),
                self.valid_jobs(),
                repository="lzzmm/gpubk",
            ),
            "a" * 40,
        )

    def test_rejects_untrusted_source_runs(self):
        cases = {
            "repository": ("repository", {"full_name": "other/gpubk"}),
            "workflow": ("path", ".github/workflows/other.yml"),
            "event": ("event", "pull_request"),
            "branch": ("head_branch", "feature"),
            "result": ("conclusion", "failure"),
        }
        for name, (field, value) in cases.items():
            with self.subTest(name=name):
                run = self.valid_run()
                run[field] = value
                with self.assertRaisesRegex(ValueError, "source run failed validation"):
                    PROMOTION.validate_source_run(run, self.valid_jobs(), repository="lzzmm/gpubk")

        with self.assertRaisesRegex(ValueError, "one successful verify-testpypi"):
            PROMOTION.validate_source_run(
                self.valid_run(),
                {"jobs": [{"name": "verify-testpypi", "conclusion": "skipped"}]},
                repository="lzzmm/gpubk",
            )

    def test_validates_exact_artifact_files_hashes_and_wheel_metadata(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            self.write_artifact(root)

            self.assertEqual(PROMOTION.validate_artifact(root), "0.2.0rc1")

    def test_rejects_changed_or_extra_artifact_files(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            _, sdist = self.write_artifact(root)
            sdist.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                PROMOTION.validate_artifact(root)

        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            self.write_artifact(root)
            (root / "dist" / "unexpected.txt").write_text("extra", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "artifact file set differs"):
                PROMOTION.validate_artifact(root)


if __name__ == "__main__":
    unittest.main()
