#!/usr/bin/env python3
"""Verify that an index release exactly matches locally built distributions."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


INDEX_URLS = {
    "pypi": "https://pypi.org/pypi/{project}/{version}/json",
    "testpypi": "https://test.pypi.org/pypi/{project}/{version}/json",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


def is_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and "/" not in value
        and "\\" not in value
    )


def read_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        if not raw_line.strip():
            continue
        fields = raw_line.split(maxsplit=1)
        if len(fields) != 2:
            raise ValueError(f"{path}:{line_number}: malformed checksum line")
        digest, filename = fields
        filename = filename.lstrip("*")
        if not SHA256.fullmatch(digest):
            raise ValueError(f"{path}:{line_number}: invalid SHA-256 digest")
        if not is_basename(filename):
            raise ValueError(f"{path}:{line_number}: filename must be a basename")
        if filename in checksums:
            raise ValueError(f"{path}:{line_number}: duplicate filename {filename}")
        checksums[filename] = digest
    if not checksums:
        raise ValueError(f"{path}: checksum file is empty")
    return checksums


def release_checksums(payload: dict[str, Any]) -> dict[str, str]:
    urls = payload.get("urls")
    if not isinstance(urls, list):
        raise ValueError("index response does not contain a urls list")

    checksums: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise ValueError("index response contains an invalid file entry")
        filename = item.get("filename")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not is_basename(filename):
            raise ValueError("index response contains an invalid filename")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError(f"index response has no valid SHA-256 for {filename}")
        if filename in checksums:
            raise ValueError(f"index response repeats {filename}")
        checksums[filename] = digest
    return checksums


def compare_checksums(expected: dict[str, str], observed: dict[str, str]) -> None:
    missing = sorted(expected.keys() - observed.keys())
    unexpected = sorted(observed.keys() - expected.keys())
    mismatched = sorted(
        filename
        for filename in expected.keys() & observed.keys()
        if expected[filename] != observed[filename]
    )
    problems = []
    if missing:
        problems.append("missing=" + ",".join(missing))
    if unexpected:
        problems.append("unexpected=" + ",".join(unexpected))
    if mismatched:
        problems.append("digest-mismatch=" + ",".join(mismatched))
    if problems:
        raise ValueError("index artifacts differ: " + "; ".join(problems))


def fetch_release(index: str, project: str, version: str, *, timeout: float) -> dict[str, Any]:
    template = INDEX_URLS[index]
    url = template.format(
        project=urllib.parse.quote(project, safe=""),
        version=urllib.parse.quote(version, safe=""),
    )
    request = urllib.request.Request(url, headers={"User-Agent": "gpubk-release-verifier"})
    # The CLI accepts only the two static HTTPS templates in INDEX_URLS.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("index response is not a JSON object")
    return payload


def verify(
    *,
    index: str,
    project: str,
    version: str,
    checksums_path: Path,
    attempts: int,
    delay: float,
    timeout: float,
) -> int:
    expected = read_checksums(checksums_path)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            observed = release_checksums(fetch_release(index, project, version, timeout=timeout))
            compare_checksums(expected, observed)
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay)
                continue
            break
        print(f"verified {len(expected)} artifact digest(s) for {project} {version} on {index}")
        return 0
    raise RuntimeError(
        f"could not verify {project} {version} on {index} after {attempts} attempt(s): "
        f"{last_error}"
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--index", choices=sorted(INDEX_URLS), required=True)
    result.add_argument("--project", required=True)
    result.add_argument("--version", required=True)
    result.add_argument("--checksums", type=Path, required=True)
    result.add_argument("--attempts", type=positive_int, default=12)
    result.add_argument("--delay", type=nonnegative_float, default=10.0)
    result.add_argument("--timeout", type=positive_float, default=20.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return verify(
            index=args.index,
            project=args.project,
            version=args.version,
            checksums_path=args.checksums,
            attempts=args.attempts,
            delay=args.delay,
            timeout=args.timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser().error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
