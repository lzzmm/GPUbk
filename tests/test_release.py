import hashlib
import re
import runpy
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTION_REF = re.compile(r"^\s*-\s+uses:\s+([^\s@]+)@([^\s#]+)")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
APACHE_2_NORMALIZED_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"


class ReleaseConfigurationTests(unittest.TestCase):
    def test_source_build_guard_rejects_an_ignored_build_isolation(self):
        setup_namespace = runpy.run_path(str(ROOT / "setup.py"))
        require_setuptools = setup_namespace["_require_setuptools"]

        require_setuptools("77.0.0")
        require_setuptools("83.0.0")
        with self.assertRaisesRegex(RuntimeError, "Upgrade pip.*published GPUbk wheel"):
            require_setuptools("59.6.0")
        with self.assertRaisesRegex(RuntimeError, "cannot verify"):
            require_setuptools("unknown")

    def test_public_distribution_is_gpubk_and_cli_stays_bk(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertRegex(pyproject, r'(?m)^name = "gpubk"$')
        self.assertRegex(pyproject, r'(?m)^bk = "bk\.cli:main"$')

        public_files = [
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "RELEASING.md",
            ROOT / "src" / "bk" / "mcp_server.py",
            ROOT / ".github" / "workflows" / "ci.yml",
        ]
        old_distribution = "bk-" + "gpu-booker"
        old_skill = "bk-" + "gpu-scheduler"
        for path in public_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(old_distribution, text, str(path.relative_to(ROOT)))
            self.assertNotIn(old_skill, text, str(path.relative_to(ROOT)))

    def test_default_readme_is_english_with_a_packaged_chinese_guide(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn('readme = "README.md"', pyproject)
        self.assertIn(
            "**English** | [简体中文](https://github.com/lzzmm/gpubk/blob/main/README.zh-CN.md)",
            english,
        )
        self.assertIn("[English](README.md) | **简体中文**", chinese)
        self.assertIn("include README.zh-CN.md", manifest)
        self.assertIn("## Install", english)
        self.assertIn("## 安装", chinese)
        self.assertNotIn("The detailed guide below is currently in Chinese.", english)

    def test_telemetry_contract_is_packaged_and_linked(self):
        telemetry = (ROOT / "TELEMETRY.md").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("include TELEMETRY.md", manifest)
        self.assertIn("gpubk.usage.v1", telemetry)
        self.assertIn("TelemetrySink", telemetry)
        self.assertIn("TELEMETRY.md", readme)

    def test_upgrade_guide_is_packaged_and_linked(self):
        guide = (ROOT / "UPGRADING.md").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn("include UPGRADING.md", manifest)
        self.assertIn("UPGRADING.md", english)
        self.assertIn("UPGRADING.md", chinese)
        self.assertIn("0.1.x to 0.2.x", guide)
        self.assertIn("weighted `--share` capacity", guide)
        self.assertIn("Do not run a 0.1 worker", guide)

    def test_prerelease_targets_a_documented_final_version(self):
        init = (ROOT / "src" / "bk" / "__init__.py").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        match = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)

        self.assertIsNotNone(match)
        candidate = re.fullmatch(r"(\d+\.\d+\.\d+)rc[1-9]\d*", match.group(1))
        if candidate:
            self.assertIn(f"## {candidate.group(1)} - Unreleased", changelog)

    def test_external_github_actions_are_pinned_to_commit_shas(self):
        workflows = ROOT / ".github" / "workflows"
        if not workflows.is_dir():
            self.skipTest("GitHub workflows are not included in this source distribution")

        unpinned = []
        for path in sorted(workflows.glob("*.yml")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = ACTION_REF.match(line)
                if match and not match.group(1).startswith("./") and not COMMIT_SHA.fullmatch(match.group(2)):
                    unpinned.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0).strip()}")

        self.assertEqual(unpinned, [], "mutable GitHub Action refs:\n" + "\n".join(unpinned))

    def test_quality_coverage_uses_one_normalized_source_tree(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("python -m pip install -e '.[mcp]'", workflow)
        for tool in ("bandit", "coverage", "ruff", "pip-audit"):
            self.assertIn(tool, workflow)
        self.assertIn("python -m pip install --upgrade 'pip>=26.1.2'", workflow)
        self.assertRegex(
            pyproject,
            r'(?s)\[tool\.coverage\.paths\]\s+source = \[\s+"src/bk",\s+"\*/site-packages/bk",\s+\]',
        )

    def test_ci_runs_security_and_package_structure_checks(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("bandit -q -r src/bk --severity-level medium", workflow)
        self.assertIn("validate-pyproject pyproject.toml", workflow)
        self.assertIn("check-wheel-contents dist/*.whl", workflow)

    def test_release_uses_trusted_publishers_and_one_promoted_artifact(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn('tags:\n      - "v[0-9]*.[0-9]*.[0-9]*"', workflow)
        self.assertIn("python -m pip install --upgrade 'pip>=26.1.2'", workflow)
        self.assertIn('"$GITHUB_REF_NAME" != "v$version"', workflow)
        self.assertIn("CHANGELOG.md needs a dated heading for $version", workflow)
        self.assertEqual(workflow.count("python -m build"), 1)
        self.assertIn("name: python-package-distributions", workflow)
        self.assertIn("environment:\n      name: testpypi", workflow)
        self.assertIn("environment:\n      name: pypi", workflow)
        self.assertIn("needs: [build, preflight-testpypi]", workflow)
        self.assertIn("needs: [build, verify-testpypi, preflight-pypi]", workflow)
        self.assertIn("vars.TESTPYPI_RELEASE_ENABLED == 'true'", workflow)
        self.assertIn("vars.PYPI_RELEASE_ENABLED == 'true'", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn("github.ref_type == 'tag'", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("https://test.pypi.org/legacy/", workflow)
        self.assertIn("manual Release dispatch requires a prerelease version", workflow)
        self.assertIn("tagged commit must be contained in origin/main", workflow)
        self.assertIn("already exists on TestPyPI", workflow)
        self.assertIn("already exists on PyPI", workflow)
        self.assertIn("pypa/gh-action-pypi-publish@", workflow)
        self.assertNotIn("password:", workflow)
        self.assertNotIn("TWINE_PASSWORD", workflow)

    def test_public_release_metadata_is_complete(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('authors = [{ name = "lzzmm", email = "cortexcyh@gmail.com" }]', pyproject)
        self.assertIn('maintainers = [{ name = "lzzmm", email = "cortexcyh@gmail.com" }]', pyproject)
        self.assertRegex(pyproject, r'(?m)^license = "Apache-2\.0"$')
        self.assertRegex(pyproject, r'(?m)^license-files = \["LICENSE"\]$')
        self.assertIn('Repository = "https://github.com/lzzmm/gpubk"', pyproject)
        self.assertIn('Issues = "https://github.com/lzzmm/gpubk/issues"', pyproject)

        license_digest = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
        self.assertEqual(license_digest, APACHE_2_NORMALIZED_SHA256)

    def test_release_docs_use_a_draft_first_immutable_github_release(self):
        guide = (ROOT / "RELEASING.md").read_text(encoding="utf-8")

        self.assertIn("Enable GitHub release immutability", guide)
        self.assertIn("create a draft GitHub Release", guide)
        self.assertIn("attach the wheel and sdist, then publish the draft", guide)
        self.assertIn("verify its hashes against PyPI", guide)
        self.assertIn("prerelease-only TestPyPI path", guide)
        self.assertNotIn("git tag -a v0.1.0", guide)


if __name__ == "__main__":
    unittest.main()
