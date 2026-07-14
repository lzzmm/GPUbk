# Release Checklist

## One-time trusted-publisher setup

GPUBK publishes without a stored PyPI API token. Before the first release:

1. In GitHub, create environments named `testpypi` and `pypi`. Require the `lzzmm` reviewer for `pypi`, restrict it to the `main` branch and `v*.*.*` tags, and keep `main` protected by the complete CI check set. The public repository currently enforces these rules for administrators too.
2. In TestPyPI's trusted-publisher settings, add owner `lzzmm`, repository `gpubk`, workflow `release.yml`, and environment `testpypi`.
3. In PyPI's pending trusted-publisher form, enter project `gpubk`, owner `lzzmm`, repository `gpubk`, workflow `release.yml`, and environment `pypi`.
4. Protect `main` with pull requests, strict GitHub Actions checks, linear history, resolved conversations, and force-push/deletion prevention. Protect version tags from update and deletion, and enable private vulnerability reporting.
5. Enable GitHub release immutability before the first GitHub Release. Immutable releases lock the tag and uploaded assets after publication, so all assets must be attached while the release is still a draft.
6. Create repository Actions variables `TESTPYPI_RELEASE_ENABLED` and `PYPI_RELEASE_ENABLED`, initially set to `false`. Set each to `true` only after its trusted publisher and environment protection are verified.

The publish jobs receive `id-token: write` only inside their protected environments. Both jobs also fail closed behind their corresponding release-enabled variable. Do not add `PYPI_API_TOKEN`, `TWINE_PASSWORD`, or a long-lived upload token to repository secrets.

The workflow treats a disabled release gate as an error, not as a successful build-only run.
If a manual release exits immediately with a `*_RELEASE_ENABLED must be true` message,
update the named repository variable after re-confirming GitHub access, then start a new run.

## Release candidates

Use a unique PEP 440 prerelease such as `0.2.0rc1` in `src/bk/__init__.py` and
keep the target heading, for example `## 0.2.0 - Unreleased`, in
`CHANGELOG.md`. Push the candidate branch, wait for CI, then manually dispatch
the `Release` workflow from `main` with `promote_run_id` left empty. This mode
accepts prereleases only and stops after TestPyPI digest and installation
verification.

Test the exact TestPyPI wheel in an isolated environment and data directory.
Increment the prerelease number for every retry; neither package index permits
replacing an uploaded file.

Publishing a tested candidate to public PyPI is an explicit promotion, not a
rebuild:

1. Confirm the TestPyPI `Release` run completed successfully and copy its
   numeric run ID from the Actions URL.
2. Set `PYPI_RELEASE_ENABLED` to `true`.
3. Dispatch `Release` from `main` again and enter that ID as `promote_run_id`.
4. Review and approve the protected `pypi` deployment.
5. Wait for the PyPI digest comparison and installation test, then return
   `PYPI_RELEASE_ENABLED` to `false`.

The promotion job accepts only a successful `release.yml` run from this
repository's `main` branch with a successful `verify-testpypi` job. It downloads
that run's immutable artifact, validates its SHA-256 file set and wheel metadata,
and compares it with TestPyPI before requesting an OIDC upload. Candidate
promotion does not create a GitHub tag or GitHub Release; those remain reserved
for final versions and their immutable release assets.

## Every release

1. Confirm the owner-approved Apache-2.0 `LICENSE` is included in both wheel and sdist metadata.
2. Confirm the `lzzmm` author/maintainer metadata and `https://github.com/lzzmm/gpubk` URLs in `pyproject.toml`.
3. Replace the candidate version in `src/bk/__init__.py` with the final version and replace `Unreleased` in the matching `CHANGELOG.md` heading with the release date. Package metadata reads the version from `bk.__version__`.
4. Run core tests:

   ```bash
   python -m pip install --upgrade pip
   python -m compileall -q src tests benchmarks tools
   ruff check src tests benchmarks tools
   PYTHONPATH=src python benchmarks/scheduler_queue.py
   PYTHONPATH=src python benchmarks/usage_store.py
   coverage run -m unittest discover -s tests -p 'test_*.py'
   coverage combine
   coverage report
   ```

5. Install optional dependencies in a clean environment and run the MCP protocol test:

   ```bash
   python -m pip install '.[mcp,gpu]' build twine pyyaml
   python -m unittest tests.test_mcp_server tests.test_mcp_integration
   python /path/to/skill-creator/scripts/quick_validate.py src/bk/data/codex-skill/gpubk
   ```

6. Build and inspect artifacts:

   ```bash
   rm -rf build dist
   python -m build
   python -m twine check dist/*
   check-wheel-contents dist/*.whl
   validate-pyproject pyproject.toml
   ```

7. Install the wheel into a fresh environment. Verify `bk --version`, core zero-dependency
   installation, `bk skill install`, and `bk-mcp` with the MCP extra. In an isolated no-GPU
   simulation, create a scheduled-command reservation from the wheel, run `bk worker --once`,
   and verify its terminal state, injected GPU environment, private log, and spec cleanup. Also
   install the most recent public GPUBK release in a separate environment, create a real ledger,
   upgrade it with the new wheel, confirm read-only checks do not rewrite the old files, and then
   create a reservation using the new scheduling fields.
8. As the configured `monitor_uid`, run `bk doctor --probe --strict` and bounded read-only
   NVML/context/recommendation checks on a real multi-GPU host with an isolated `BK_DATA_DIR`.
   The `process-identity` probe must demonstrate numeric ownership visibility for a process from
   another UID; create no GPU workload merely for this check. Confirm every GPU reports
   `capabilities.stable_device_identifier=true`; after one bounded `bk monitor --once` sample,
   confirm `collector.stable_device_identifier_gap=[]` and
   `collector.process_identity_gap=[]`. Do not start workloads or services during release
   validation.

   Prefer the local SSH orchestrator. It downloads and verifies the exact PyPI wheelhouse,
   leaves production state untouched, and retrieves a digest-verified report even when an
   acceptance check fails:

   ```bash
   python3 tools/remote_acceptance.py USER@GPU-HOST \
     --remote-python /opt/gpubk/bin/python \
     --system-bk /usr/local/bin/bk \
     --sudo
   ```

   Archive the resulting `acceptance-reports/` directory with the release evidence. Complete
   its four listed manual checks before promoting a release candidate to a final version. For
   the approved live-workload check, activate a CUDA PyTorch environment as an ordinary user and
   run `bk usage demo`; retain its summary, one-minute samples, and process events with the report.

   For a cluster-capable release, also test the exact candidate wheel on at least two distinct
   SSH hosts. This second runner uses simulated GPUs and private temporary ledgers, so it may run
   before the approved live workload and without `sudo`:

   ```bash
   python3 tools/cluster_acceptance.py USER@GPU-HOST-A USER@GPU-HOST-B \
     --wheel dist/gpubk-VERSION-py3-none-any.whl
   ```

   Require a PASS report, distinct stable node IDs, and successful remote cleanup. This proves
   package installation, SSH federation, routing, replay, and cancellation; it does not replace
   the real NVML, second-user authorization, workload, service restart, or reboot checks.
9. Commit and push the release metadata through a pull request, wait for `CI` to pass, and merge it to `main`. Create the annotated tag from that exact `main` commit:

   ```bash
   VERSION=$(PYTHONPATH=src python -c 'from bk import __version__; print(__version__)')
   git tag -a "v$VERSION" -m "GPUBK $VERSION"
   git push origin "v$VERSION"
   ```

   Do not merge another change before the tag workflow starts. Production
   release tags must be annotated and point exactly at the current
   `origin/main` tip; an older commit that merely exists in main history is
   rejected.

10. The `Release` workflow rebuilds and tests the tag, records the wheel and sdist SHA-256
    digests, uploads those artifacts to TestPyPI, verifies both indexed files against the recorded
    digests, and installs the wheel back from TestPyPI. Only then does the protected `pypi`
    environment request approval to promote the exact same wheel and sdist.
11. Approve `pypi` and wait for its automatic digest comparison and installation smoke test.
    Download the exact workflow artifact, confirm its recorded hashes, create a draft GitHub Release
    for the same tag, attach the wheel and sdist, then publish the draft. Never rebuild an artifact
    locally for promotion. Release immutability locks the published tag and assets and creates a
    release attestation.

The workflow rejects a tag that is lightweight, points anywhere other than the
current `main` tip, is a prerelease, does not match the package, or still has an
`Unreleased` changelog heading. It also rejects an already-existing TestPyPI or
PyPI version before requesting an upload. A normal final publication therefore
starts from a new final-version tag. Manual dispatch with an empty
`promote_run_id` remains TestPyPI-only; a non-empty ID invokes the separately
reviewed, artifact-preserving prerelease promotion path described above.
