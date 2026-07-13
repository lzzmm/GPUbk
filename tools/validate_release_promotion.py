#!/usr/bin/env python3
"""Validate a tested release artifact before promoting it to PyPI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any


GITHUB_API = "https://api.github.com"
EXPECTED_WORKFLOW = ".github/workflows/release.yml"
EXPECTED_VERIFY_JOB = "verify-testpypi"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
RUN_ID = re.compile(r"[1-9][0-9]*")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def github_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "gpubk-release-promoter",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("GitHub API response is not an object")
    return payload


def fetch_source_run(repository: str, run_id: str, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be an owner/name pair")
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("run ID must be a positive integer")
    owner, name = (urllib.parse.quote(part, safe="") for part in repository.split("/", 1))
    base = f"{GITHUB_API}/repos/{owner}/{name}/actions/runs/{run_id}"
    return github_json(base, token), github_json(f"{base}/jobs?filter=latest&per_page=100", token)


def _full_name(payload: dict[str, Any], key: str) -> object:
    value = payload.get(key)
    return value.get("full_name") if isinstance(value, dict) else None


def validate_source_run(
    run: dict[str, Any],
    jobs_payload: dict[str, Any],
    *,
    repository: str,
) -> str:
    checks = {
        "repository": _full_name(run, "repository") == repository,
        "head repository": _full_name(run, "head_repository") == repository,
        "workflow": run.get("path") == EXPECTED_WORKFLOW,
        "event": run.get("event") == "workflow_dispatch",
        "branch": run.get("head_branch") == "main",
        "run result": run.get("status") == "completed" and run.get("conclusion") == "success",
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise ValueError("source run failed validation: " + ", ".join(failed))

    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("source run jobs response does not contain a jobs list")
    verify_jobs = [job for job in jobs if isinstance(job, dict) and job.get("name") == EXPECTED_VERIFY_JOB]
    if len(verify_jobs) != 1 or verify_jobs[0].get("conclusion") != "success":
        raise ValueError(f"source run must have one successful {EXPECTED_VERIFY_JOB} job")

    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("source run has no valid head SHA")
    return head_sha


def read_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        fields = raw_line.split(maxsplit=1)
        if len(fields) != 2:
            raise ValueError(f"{path}:{line_number}: malformed checksum line")
        digest, filename = fields
        filename = filename.lstrip("*")
        if not SHA256.fullmatch(digest):
            raise ValueError(f"{path}:{line_number}: invalid SHA-256 digest")
        if (
            filename in {"", ".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
        ):
            raise ValueError(f"{path}:{line_number}: filename must be a basename")
        if filename in checksums:
            raise ValueError(f"{path}:{line_number}: duplicate filename {filename}")
        checksums[filename] = digest
    if not checksums:
        raise ValueError(f"{path}: checksum file is empty")
    return checksums


def _wheel_metadata(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as wheel:
        metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"{path}: expected exactly one wheel METADATA file")
        metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not isinstance(name, str) or re.sub(r"[-_.]+", "-", name).lower() != "gpubk":
        raise ValueError(f"{path}: wheel project name is not gpubk")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError(f"{path}: wheel version is invalid")
    return name, version


def validate_artifact(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{root}: artifact directory is missing or unsafe")
    dist = root / "dist"
    checksums_path = root / "SHA256SUMS"
    if dist.is_symlink() or not dist.is_dir() or checksums_path.is_symlink():
        raise ValueError(f"{root}: distribution files are missing or unsafe")

    checksums = read_checksums(checksums_path)
    entries = list(dist.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ValueError(f"{dist}: artifact contains a non-regular file")
    files = entries
    filenames = {path.name for path in files}
    if filenames != set(checksums):
        missing = sorted(set(checksums) - filenames)
        unexpected = sorted(filenames - set(checksums))
        raise ValueError(f"artifact file set differs: missing={missing}, unexpected={unexpected}")
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != checksums[path.name]:
            raise ValueError(f"{path}: SHA-256 mismatch")

    wheels = [path for path in files if path.suffix == ".whl"]
    sdists = [path for path in files if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(files) != 2:
        raise ValueError("artifact must contain exactly one wheel and one .tar.gz source distribution")
    _, version = _wheel_metadata(wheels[0])
    return version


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repository", required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--artifact-dir", type=Path, required=True)
    result.add_argument("--github-output", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        parser().error("GITHUB_TOKEN is required")
    try:
        run, jobs = fetch_source_run(args.repository, args.run_id, token)
        head_sha = validate_source_run(run, jobs, repository=args.repository)
        version = validate_artifact(args.artifact_dir)
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"version={version}\nsource_sha={head_sha}\n")
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser().error(str(exc))
    print(f"validated gpubk {version} from successful TestPyPI run {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
