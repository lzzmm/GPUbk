import hashlib
import re
import runpy
import subprocess
import sys
import unittest
from pathlib import Path

from bk import __version__
from bk.systemd import system_unit_text


ROOT = Path(__file__).resolve().parents[1]
ACTION_REF = re.compile(r"^\s*-\s+uses:\s+([^\s@]+)@([^\s#]+)")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
APACHE_2_NORMALIZED_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"


class ReleaseConfigurationTests(unittest.TestCase):
    def test_user_facing_brand_uses_consistent_capitalization(self):
        legacy = "GPU" + "bk"
        files = [
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "CHANGELOG.md",
            ROOT / "RELEASING.md",
            ROOT / "SECURITY.md",
            ROOT / "TELEMETRY.md",
            ROOT / "UPGRADING.md",
            ROOT / "setup.py",
        ]
        files.extend((ROOT / "src").rglob("*.py"))
        files.extend((ROOT / "src").rglob("*.md"))
        files.extend((ROOT / "src").rglob("*.service"))
        files.extend((ROOT / ".github").rglob("*.yml"))

        offenders = [
            str(path.relative_to(ROOT))
            for path in files
            if legacy in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_source_build_guard_rejects_an_ignored_build_isolation(self):
        setup_namespace = runpy.run_path(str(ROOT / "setup.py"))
        require_setuptools = setup_namespace["_require_setuptools"]

        require_setuptools("77.0.0")
        require_setuptools("83.0.0")
        with self.assertRaisesRegex(RuntimeError, "Upgrade pip.*published GPUBK wheel"):
            require_setuptools("59.6.0")
        with self.assertRaisesRegex(RuntimeError, "cannot verify"):
            require_setuptools("unknown")

    def test_public_distribution_is_gpubk_and_cli_stays_bk(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertRegex(pyproject, r'(?m)^name = "gpubk"$')
        self.assertRegex(pyproject, r'(?m)^bk = "bk\.entrypoint:main"$')

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

    def test_source_distribution_keeps_release_tests_self_contained(self):
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("recursive-include tests test_*.py", manifest)
        self.assertIn("recursive-include tools *.py", manifest)
        self.assertIn("recursive-include .github/workflows *.yml", manifest)

    def test_remote_gpu_acceptance_runner_is_packaged_and_documented(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        releasing = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
        command = "python3 tools/remote_acceptance.py USER@GPU-HOST"

        self.assertTrue((ROOT / "tools" / "remote_acceptance.py").is_file())
        self.assertTrue((ROOT / "tools" / "acceptance_remote.py").is_file())
        self.assertIn(command, english)
        self.assertIn(command, chinese)
        self.assertIn("tools/remote_acceptance.py", releasing)

    def test_cluster_acceptance_and_design_are_packaged_and_documented(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        releasing = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        command = "python3 tools/cluster_acceptance.py user@gpu-a user@gpu-b"

        self.assertTrue((ROOT / "tools" / "cluster_acceptance.py").is_file())
        self.assertTrue((ROOT / "CLUSTER.md").is_file())
        self.assertIn(command, english)
        self.assertIn(command, chinese)
        self.assertIn("tools/cluster_acceptance.py", releasing)
        self.assertIn("include CLUSTER.md", manifest)

    def test_version_entrypoint_does_not_import_the_full_cli(self):
        code = (
            "import sys\n"
            "from bk.entrypoint import main\n"
            "result = main(['--version'])\n"
            "raise SystemExit(0 if result == 0 and 'bk.cli' not in sys.modules else 1)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"bk {__version__}\n")

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

    def test_shared_server_setup_uses_service_owned_broker_storage(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

        for text in (english, chinese):
            self.assertIn("sudo bk admin init", text)
            self.assertIn("--access group --group gpuusers", text)
            self.assertIn("--service-user", text)
            self.assertIn("/opt/gpubk", text)
            self.assertIn("bk admin transfer NEWUSER --dry-run", text)
            self.assertIn("bk admin transfer --recover --yes", text)
            self.assertIn("bk admin services install --yes", text)
            self.assertIn("gpubk-broker.service", text)
            self.assertIn("gpubk-monitor.service", text)
            self.assertIn("bk admin uninstall --dry-run --purge-data", text)
            self.assertIn("0644", text)
            self.assertIn("0755", text)
            self.assertNotIn("0777", text)
        self.assertIn("account owns the ledger", security)
        self.assertIn("SO_PEERCRED", security)
        self.assertIn("bk admin transfer", security)
        self.assertIn("bk admin services", security)
        self.assertNotIn("defaults to open cooperative access", security)

    def test_telemetry_contract_is_packaged_and_linked(self):
        telemetry = (ROOT / "TELEMETRY.md").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("include TELEMETRY.md", manifest)
        self.assertIn("gpubk.usage.v1", telemetry)
        self.assertIn("TelemetrySink", telemetry)
        self.assertIn("TELEMETRY.md", readme)

    def test_release_docs_require_stable_scheduled_job_device_binding(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        releasing = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
        telemetry = (ROOT / "TELEMETRY.md").read_text(encoding="utf-8")
        protocol = (
            ROOT / "src" / "bk" / "data" / "codex-skill" / "gpubk"
            / "references" / "protocol.md"
        ).read_text(encoding="utf-8")

        self.assertIn("NVML indices equal CUDA ordinals", readme)
        self.assertIn("stable UUIDs", security)
        self.assertIn("capabilities.stable_device_identifier=true", releasing)
        self.assertIn("collector.stable_device_identifier_gap=[]", releasing)
        self.assertIn("legacy v1 heartbeat", telemetry)
        self.assertIn("capabilities.stable_device_identifier", protocol)

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
        self.assertIn("monitor_uid", guide)
        self.assertIn("pip install --upgrade 'gpubk[gpu]'", guide)
        self.assertIn("bk admin transfer NEWUSER --dry-run", guide)
        self.assertIn("bk admin services install --yes", guide)
        self.assertIn("Do not run a 0.1 worker", guide)

    def test_bundled_monitor_unit_defers_timing_to_trusted_config(self):
        unit = (ROOT / "src" / "bk" / "data" / "systemd" / "bk-monitor.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("ExecStart=@PYTHON_EXECUTABLE@ -m bk monitor\n", unit)
        self.assertNotIn("--interval", unit)
        self.assertNotIn("--rollup", unit)
        self.assertIn("RestartPreventExitStatus=75 77 78", unit)
        self.assertIn("StartLimitIntervalSec=60", unit)
        self.assertIn("StartLimitBurst=3", unit)

    def test_bundled_worker_unit_has_bounded_failure_recovery(self):
        unit = (ROOT / "src" / "bk" / "data" / "systemd" / "bk-worker.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("RestartPreventExitStatus=75 78", unit)
        self.assertIn("TimeoutStopSec=75", unit)
        self.assertIn("StartLimitIntervalSec=60", unit)
        self.assertIn("StartLimitBurst=3", unit)

    def test_bundled_system_units_are_non_root_and_boot_persistent(self):
        directory = ROOT / "src" / "bk" / "data" / "systemd" / "system"
        broker = (directory / "gpubk-broker.service").read_text(encoding="utf-8")
        monitor = (directory / "gpubk-monitor.service").read_text(encoding="utf-8")

        for unit in (broker, monitor):
            self.assertIn("User=@SERVICE_UID@", unit)
            self.assertIn("Group=@SERVICE_GID@", unit)
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("NoNewPrivileges=true", unit)
            self.assertIn("WantedBy=multi-user.target", unit)
            self.assertNotIn("User=root", unit)
        rendered_broker = system_unit_text(
            "broker",
            python_executable="/opt/gpubk/bin/python",
            config_file=Path("/var/lib/gpubk/config.json"),
            service_uid=1000,
            service_gid=1000,
            data_dir=Path("/var/lib/gpubk"),
            socket_directory=Path("/run/gpubk"),
        )
        self.assertIn("RuntimeDirectoryPreserve=yes", rendered_broker)
        self.assertIn("Wants=gpubk-broker.service", monitor)
        self.assertNotIn("ProtectClock=", monitor)
        self.assertNotIn("DevicePolicy=", monitor)
        self.assertNotIn("DeviceAllow=", monitor)
        self.assertIn("ProtectClock=true", broker)
        self.assertIn("ReadWritePaths=@DATA_DIRECTORY@\n", monitor)
        self.assertNotIn("@SOCKET_DIRECTORY@", monitor)

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

        self.assertIn("bandit -q -r src/bk tools --severity-level medium", workflow)
        self.assertIn("ruff check src tests benchmarks tools", workflow)
        self.assertIn("AdminRootLifecycleTests", workflow)
        self.assertIn("validate-pyproject pyproject.toml", workflow)
        self.assertIn("check-wheel-contents dist/*.whl", workflow)
        self.assertIn("gpu-extra:", workflow)
        self.assertIn("python -m pip install '.[gpu]'", workflow)
        self.assertIn("nvmlDeviceGetProcessUtilization", workflow)
        self.assertIn("Verify scheduled-command wheel flow", workflow)
        self.assertIn('"bk/tutorial.py"', workflow)
        self.assertIn("GPUBK tutorial 1/", workflow)
        self.assertIn("service uninstall worker --target-dir", workflow)
        self.assertIn('"BK_WORKER_LIVE_GUARD": "0"', workflow)
        self.assertIn('stored["job"]["status"] != "succeeded"', workflow)

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
        self.assertIn("ENABLED: ${{ vars.TESTPYPI_RELEASE_ENABLED }}", workflow)
        self.assertIn("TESTPYPI_RELEASE_ENABLED must be true for a release build", workflow)
        self.assertEqual(workflow.count("ENABLED: ${{ vars.PYPI_RELEASE_ENABLED }}"), 2)
        self.assertIn("PYPI_RELEASE_ENABLED must be true for a production release", workflow)
        self.assertIn("PYPI_RELEASE_ENABLED must be true for artifact promotion", workflow)
        self.assertNotIn("vars.TESTPYPI_RELEASE_ENABLED == 'true'", workflow)
        self.assertNotIn("vars.PYPI_RELEASE_ENABLED == 'true'", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn("github.ref_type == 'tag'", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("https://test.pypi.org/legacy/", workflow)
        self.assertIn("manual Release dispatch requires a prerelease version", workflow)
        self.assertIn("production releases require an annotated tag", workflow)
        self.assertIn("tagged commit must equal the current origin/main tip", workflow)
        self.assertIn('git cat-file -t "$GITHUB_REF"', workflow)
        self.assertIn('git rev-parse "${GITHUB_REF}^{commit}"', workflow)
        self.assertIn("Smoke-test upgrade from last public release", workflow)
        self.assertIn('LAST_PUBLIC_VERSION: "0.1.0"', workflow)
        self.assertIn('old[0]["share_units_per_gpu"] != 1', workflow)
        self.assertIn('new["share_units_per_gpu"] != 2', workflow)
        self.assertIn("Record distribution hashes", workflow)
        self.assertEqual(workflow.count("Verify uploaded artifact hashes"), 2)
        self.assertEqual(workflow.count("tools/verify_index_artifacts.py"), 5)
        self.assertIn("--index testpypi", workflow)
        self.assertIn("--index pypi", workflow)
        self.assertIn("promote_run_id:", workflow)
        self.assertIn("tools/validate_release_promotion.py", workflow)
        self.assertIn("run-id: ${{ inputs.promote_run_id }}", workflow)
        self.assertIn("packages-dir: promoted-artifact/dist/", workflow)
        self.assertIn("actions: read", workflow)
        self.assertNotIn("merge-base --is-ancestor", workflow)
        self.assertIn("already exists on TestPyPI", workflow)
        self.assertIn("already exists on PyPI", workflow)
        self.assertIn("pypa/gh-action-pypi-publish@", workflow)
        self.assertEqual(workflow.count("packages-dir: dist/"), 2)
        self.assertNotIn("name: python-package-distributions\n          path: dist/", workflow)
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
        self.assertIn("automatic digest comparison", guide)
        self.assertIn("confirm its recorded hashes", guide)
        self.assertIn("enter that ID as `promote_run_id`", guide)
        self.assertIn("`PYPI_RELEASE_ENABLED` to `false`", guide)
        self.assertIn("does not create a GitHub tag or GitHub Release", guide)
        self.assertNotIn("git tag -a v0.1.0", guide)


if __name__ == "__main__":
    unittest.main()
