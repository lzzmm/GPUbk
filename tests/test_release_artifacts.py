import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "verify_index_artifacts.py"
SPEC = importlib.util.spec_from_file_location("verify_index_artifacts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class ReleaseArtifactVerifierTests(unittest.TestCase):
    def test_reads_standard_sha256sum_output(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "SHA256SUMS"
            path.write_text(
                f"{'a' * 64}  gpubk-1.0-py3-none-any.whl\n"
                f"{'b' * 64} *gpubk-1.0.tar.gz\n",
                encoding="ascii",
            )

            self.assertEqual(
                VERIFIER.read_checksums(path),
                {
                    "gpubk-1.0-py3-none-any.whl": "a" * 64,
                    "gpubk-1.0.tar.gz": "b" * 64,
                },
            )

    def test_rejects_paths_duplicate_names_and_invalid_digests(self):
        cases = (
            f"{'a' * 64}  dist/gpubk.whl\n",
            f"{'a' * 64}  dist\\gpubk.whl\n",
            f"{'a' * 64}  gpubk.whl\n{'b' * 64}  gpubk.whl\n",
            "not-a-digest  gpubk.whl\n",
            "",
        )
        for content in cases:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as raw_directory:
                    path = Path(raw_directory) / "SHA256SUMS"
                    path.write_text(content, encoding="ascii")
                    with self.assertRaises(ValueError):
                        VERIFIER.read_checksums(path)

    def test_extracts_and_compares_index_digests(self):
        payload = {
            "urls": [
                {"filename": "gpubk.whl", "digests": {"sha256": "a" * 64}},
                {"filename": "gpubk.tar.gz", "digests": {"sha256": "b" * 64}},
            ]
        }
        observed = VERIFIER.release_checksums(payload)

        self.assertEqual(observed, {"gpubk.whl": "a" * 64, "gpubk.tar.gz": "b" * 64})
        VERIFIER.compare_checksums(observed, observed)

    def test_reports_missing_unexpected_and_mismatched_artifacts(self):
        expected = {"wheel.whl": "a" * 64, "source.tar.gz": "b" * 64}
        observed = {"wheel.whl": "c" * 64, "other.zip": "d" * 64}

        with self.assertRaisesRegex(
            ValueError,
            "missing=source.tar.gz; unexpected=other.zip; digest-mismatch=wheel.whl",
        ):
            VERIFIER.compare_checksums(expected, observed)


if __name__ == "__main__":
    unittest.main()
