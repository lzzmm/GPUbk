# Release Checklist

1. Choose and add the owner-approved open-source license. Do not publish without an explicit license decision.
2. Confirm author/maintainer metadata and source URLs in `pyproject.toml`.
3. Update `src/bk/__init__.py` and `CHANGELOG.md`; package metadata reads the version from `bk.__version__`.
4. Run core tests:

   ```bash
   python -m compileall -q src tests
   coverage run -m unittest discover -s tests -p 'test_*.py'
   coverage combine
   coverage report
   ```

5. Install optional dependencies in a clean environment and run the MCP protocol test:

   ```bash
   python -m pip install '.[mcp,gpu]' build twine pyyaml
   python -m unittest tests.test_mcp_server tests.test_mcp_integration
   python /path/to/skill-creator/scripts/quick_validate.py src/bk/data/codex-skill/bk-gpu-scheduler
   ```

6. Build and inspect artifacts:

   ```bash
   rm -rf build dist
   python -m build
   python -m twine check dist/*
   ```

7. Install the wheel into a fresh environment. Verify `bk --version`, core zero-dependency installation, `bk skill install`, and `bk-mcp` with the MCP extra.
8. Run bounded read-only NVML/context/recommendation checks on a real multi-GPU host with an isolated `BK_DATA_DIR`. Do not start workloads or services during release validation.
9. Tag the exact tested commit. Publish to TestPyPI before PyPI, then verify installation from the uploaded wheel.
