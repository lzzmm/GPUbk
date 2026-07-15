#!/usr/bin/env python3
"""Fetch, run, and retrieve a GPUBK GPU-host acceptance test."""

from __future__ import annotations

import argparse
import ast
import email.parser
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
LOCAL_RUNNER = Path(__file__).resolve()
REMOTE_RUNNER = ROOT / "tools" / "acceptance_remote.py"
MANIFEST_SCHEMA = "gpubk.acceptance-bundle.v1"
REPORT_MEMBER_ROOT = "gpubk-acceptance/"
SAFE_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
SAFE_SSH_TARGET = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+")
SAFE_GIT_REVISION = re.compile(r"[0-9a-f]{40}")
DEFAULT_REPOSITORY = "https://github.com/lzzmm/GPUbk.git"


class AcceptanceError(RuntimeError):
    """An expected setup, transport, or verification failure."""


class DownloadUnavailable(AcceptanceError):
    """The selected package index could not supply the requested wheelhouse."""


@dataclass(frozen=True)
class SshSettings:
    target: str
    port: int | None
    identity: Path | None
    options: tuple[str, ...]

    def transport_options(self) -> tuple[str, ...]:
        configured = {
            re.split(r"[ =]", option.strip(), maxsplit=1)[0].lower()
            for option in self.options
        }
        defaults = (
            "ConnectTimeout=20",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=3",
        )
        return (
            tuple(
                option
                for option in defaults
                if option.partition("=")[0].lower() not in configured
            )
            + self.options
        )

    def ssh_argv(self, *, tty: bool = False) -> list[str]:
        argv = ["ssh"]
        if tty:
            argv.append("-tt")
        if self.port is not None:
            argv.extend(("-p", str(self.port)))
        if self.identity is not None:
            argv.extend(("-i", str(self.identity)))
        for option in self.transport_options():
            argv.extend(("-o", option))
        argv.append(self.target)
        return argv

    def scp_argv(self) -> list[str]:
        argv = ["scp", "-q"]
        if self.port is not None:
            argv.extend(("-P", str(self.port)))
        if self.identity is not None:
            argv.extend(("-i", str(self.identity)))
        for option in self.transport_options():
            argv.extend(("-o", option))
        return argv


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def source_version(root: Path = ROOT) -> str:
    init_path = root / "src" / "bk" / "__init__.py"
    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise AcceptanceError(f"could not read __version__ from {init_path}")


def validate_target(value: str) -> str:
    if SAFE_SSH_TARGET.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "SSH target must be USER@HOST or a configured host alias"
        )
    return value


def validate_option(value: str) -> str:
    if not value or any(character in "\r\n\x00" for character in value):
        raise argparse.ArgumentTypeError(
            "SSH options cannot be empty or contain control characters"
        )
    return value


def validate_executable(value: str) -> str:
    if not value or any(character in "\r\n\x00" for character in value):
        raise argparse.ArgumentTypeError(
            "remote command cannot be empty or contain control characters"
        )
    return value


def require_local_commands() -> None:
    missing = [name for name in ("ssh", "scp") if shutil.which(name) is None]
    if missing:
        raise AcceptanceError("missing local command(s): " + ", ".join(missing))


def run_checked(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    visible: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv),
            text=True,
            stdout=None if visible else subprocess.PIPE,
            stderr=None if visible else subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = f" after {timeout:g}s" if timeout is not None else ""
        raise AcceptanceError(
            f"command timed out{duration}: {shlex.join(argv)}; "
            "check local PyPI connectivity and retry"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise AcceptanceError(
            f"command failed ({result.returncode}): {shlex.join(argv)}\n{detail}"
        )
    return result


def wheel_metadata(path: Path) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
        raise AcceptanceError(f"wheelhouse entry is not a regular wheel: {path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(names) != 1:
                raise AcceptanceError(
                    f"wheel has {len(names)} METADATA files: {path.name}"
                )
            raw = archive.read(names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise AcceptanceError(f"cannot inspect wheel {path.name}: {exc}") from exc
    message = email.parser.Parser().parsestr(raw)
    name = message.get("Name")
    version = message.get("Version")
    if not name or not version:
        raise AcceptanceError(f"wheel metadata lacks Name or Version: {path.name}")
    return name, version


def pypi_digest(name: str, version: str, filename: str) -> str:
    encoded_name = urllib.parse.quote(name, safe="")
    encoded_version = urllib.parse.quote(version, safe="")
    url = f"https://pypi.org/pypi/{encoded_name}/{encoded_version}/json"
    request = urllib.request.Request(
        url, headers={"User-Agent": "gpubk-remote-acceptance/1"}
    )
    try:
        # The URL is assembled from escaped path segments on the fixed public PyPI HTTPS origin.
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
            payload = json.load(response)
    except OSError as exc:
        raise DownloadUnavailable(
            f"could not verify {filename} against PyPI: {exc}"
        ) from exc
    except ValueError as exc:
        raise AcceptanceError(f"PyPI returned invalid metadata for {filename}: {exc}") from exc
    for item in payload.get("urls", []):
        if item.get("filename") == filename:
            digest = item.get("digests", {}).get("sha256")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    raise AcceptanceError(f"PyPI does not list downloaded file {filename}")


def verify_wheelhouse(
    wheelhouse: Path, version: str, *, verify_index: bool = True
) -> list[Path]:
    files = sorted(wheelhouse.iterdir())
    if not files:
        raise AcceptanceError("wheelhouse is empty")
    package_matches = 0
    for path in files:
        name, package_version = wheel_metadata(path)
        if canonical_name(name) == "gpubk":
            package_matches += 1
            if package_version != version:
                raise AcceptanceError(
                    f"downloaded GPUBK version {package_version}, expected {version}"
                )
        if verify_index:
            expected = pypi_digest(name, package_version, path.name)
            actual = sha256_file(path)
            if actual != expected:
                raise AcceptanceError(f"PyPI SHA-256 mismatch for {path.name}")
    if package_matches != 1:
        raise AcceptanceError(f"expected one GPUBK wheel, found {package_matches}")
    return files


def prepare_wheelhouse(
    destination: Path,
    version: str,
    supplied: Path | None,
    *,
    verify_index: bool,
    python_executable: str = sys.executable,
) -> list[Path]:
    destination.mkdir(mode=0o700)
    if supplied is None:
        print(
            f"Downloading gpubk[gpu]=={version} and wheels from public PyPI...",
            flush=True,
        )
        try:
            run_checked(
                [
                    python_executable,
                    "-m",
                    "pip",
                    "download",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--index-url",
                    "https://pypi.org/simple/",
                    "--only-binary=:all:",
                    "--timeout",
                    "20",
                    "--retries",
                    "2",
                    "--progress-bar",
                    "off",
                    "--dest",
                    str(destination),
                    f"gpubk[gpu]=={version}",
                ],
                timeout=180,
                visible=True,
            )
        except AcceptanceError as exc:
            raise DownloadUnavailable(str(exc)) from exc
    else:
        supplied = supplied.expanduser()
        if supplied.is_symlink():
            raise AcceptanceError("--wheelhouse must name a real directory")
        supplied = supplied.resolve(strict=True)
        if not supplied.is_dir():
            raise AcceptanceError("--wheelhouse must name a real directory")
        for source in supplied.iterdir():
            if source.is_symlink() or not source.is_file():
                raise AcceptanceError(f"unsafe wheelhouse entry: {source.name}")
            shutil.copyfile(source, destination / source.name)
    return verify_wheelhouse(destination, version, verify_index=verify_index)


def build_manifest(
    run_id: str,
    version: str,
    runner: Path,
    wheels: Sequence[Path],
    *,
    source: str = "https://pypi.org/project/gpubk/",
) -> dict[str, Any]:
    paths = [runner, *wheels]
    files: dict[str, dict[str, Any]] = {}
    for path in paths:
        relative = (
            "acceptance_remote.py" if path == runner else f"wheelhouse/{path.name}"
        )
        files[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return {
        "schema_version": MANIFEST_SCHEMA,
        "run_id": run_id,
        "version": version,
        "created_at": utc_now(),
        "source": source,
        "files": files,
    }


def build_bundle(
    work: Path, manifest: dict[str, Any], runner: Path, wheels: Sequence[Path]
) -> Path:
    manifest_path = work / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o600)
    bundle_path = work / "gpubk-acceptance-bundle.tar.gz"
    with tarfile.open(bundle_path, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        bundle.add(runner, arcname="acceptance_remote.py", recursive=False)
        bundle.add(manifest_path, arcname="manifest.json", recursive=False)
        for wheel in wheels:
            bundle.add(wheel, arcname=f"wheelhouse/{wheel.name}", recursive=False)
    bundle_path.chmod(0o600)
    return bundle_path


def remote_stage(run_id: str) -> str:
    if SAFE_RUN_ID.fullmatch(run_id) is None:
        raise AcceptanceError("generated run ID is unsafe")
    return f".cache/gpubk/acceptance/{run_id}"


def remote_setup_command(run_id: str) -> str:
    stage_name = shlex.quote(run_id)
    return (
        "set -eu; umask 077; "
        'root="$HOME/.cache/gpubk/acceptance"; '
        'mkdir -p "$root"; chmod 700 "$root"; '
        f'stage="$root"/{stage_name}; '
        'test ! -e "$stage"; mkdir "$stage"; chmod 700 "$stage"'
    )


def source_revision() -> str:
    status = run_checked(
        ["git", "status", "--porcelain", "--untracked-files=no"], timeout=15
    ).stdout.strip()
    if status:
        raise AcceptanceError(
            "tracked files are modified; commit them before using --source"
        )
    revision = run_checked(["git", "rev-parse", "HEAD"], timeout=15).stdout.strip()
    if SAFE_GIT_REVISION.fullmatch(revision) is None:
        raise AcceptanceError("current Git revision is invalid")
    return revision


def remote_source_command(
    relative_stage: str, *, repository: str, revision: str
) -> str:
    if SAFE_GIT_REVISION.fullmatch(revision) is None:
        raise AcceptanceError("source revision is unsafe")
    return (
        'set -eu; stage="$HOME/'
        + relative_stage
        + '"; source="$stage/source"; '
        + 'command -v git >/dev/null; git init -q "$source"; '
        + 'git -C "$source" remote add origin '
        + shlex.quote(repository)
        + "; git -C \"$source\" fetch -q --depth=1 origin "
        + shlex.quote(revision)
        + '; git -C "$source" checkout -q --detach FETCH_HEAD; '
        + 'test "$(git -C "$source" rev-parse HEAD)" = '
        + shlex.quote(revision)
    )


EXTRACT_CODE = """\
import pathlib, shutil, sys, tarfile
archive = pathlib.Path(sys.argv[1])
stage = pathlib.Path(sys.argv[2])
allowed = {"acceptance_remote.py", "manifest.json"}
with tarfile.open(archive, "r:gz") as bundle:
    members = bundle.getmembers()
    names = [member.name for member in members]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate archive member")
    for member in members:
        name = member.name
        wheel = name.startswith("wheelhouse/") and "/" not in name.removeprefix("wheelhouse/")
        if (name not in allowed and not wheel) or not member.isfile():
            raise SystemExit(f"unsafe archive member: {name}")
        target = stage / name
        target.parent.mkdir(mode=0o700, exist_ok=True)
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"unreadable archive member: {name}")
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o700 if name == "acceptance_remote.py" else 0o600)
archive.unlink()
"""


CLEANUP_CODE = """\
import pathlib, shutil, sys
stage = pathlib.Path(sys.argv[1])
root = pathlib.Path.home() / ".cache" / "gpubk" / "acceptance"
if stage.is_symlink() or stage.parent.resolve() != root.resolve():
    raise SystemExit("refusing unsafe cleanup path")
if stage.exists():
    shutil.rmtree(stage)
"""


def run_ssh(
    settings: SshSettings,
    command: str,
    *,
    tty: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    argv = [*settings.ssh_argv(tty=tty), command]
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )


def require_ssh_success(
    result: subprocess.CompletedProcess[str], operation: str
) -> None:
    if result.returncode == 0:
        return
    detail = ((result.stderr or "") + (result.stdout or "")).strip()
    raise AcceptanceError(
        f"{operation} failed ({result.returncode})" + (f": {detail}" if detail else "")
    )


def upload_bundle(settings: SshSettings, bundle: Path, relative_stage: str) -> None:
    destination = f"{settings.target}:{relative_stage}/bundle.tar.gz"
    result = subprocess.run(
        [*settings.scp_argv(), str(bundle), destination], check=False
    )
    if result.returncode != 0:
        raise AcceptanceError(f"bundle upload failed ({result.returncode})")


def upload_bootstrap(
    settings: SshSettings,
    runner: Path,
    downloader: Path,
    relative_stage: str,
) -> None:
    destination = f"{settings.target}:{relative_stage}/"
    result = subprocess.run(
        [*settings.scp_argv(), str(runner), str(downloader), destination], check=False
    )
    if result.returncode != 0:
        raise AcceptanceError(f"remote bootstrap upload failed ({result.returncode})")


def extraction_command(relative_stage: str, remote_python: str) -> str:
    return (
        'set -eu; stage="$HOME/'
        + relative_stage
        + '"; exec '
        + shlex.quote(remote_python)
        + " -c "
        + shlex.quote(EXTRACT_CODE)
        + ' "$stage/bundle.tar.gz" "$stage"'
    )


def runner_command(
    relative_stage: str,
    *,
    run_id: str,
    version: str,
    remote_python: str,
    system_bk: str,
    sudo: bool,
    include_journal: bool,
    source_revision: str | None = None,
    live_gpu: bool = False,
    live_seconds: int = 65,
    live_python: str = "python3",
    download_wheelhouse: bool = False,
    launcher_python: str = "python3",
) -> str:
    options = [
        "--run-id",
        run_id,
        "--version",
        version,
        "--remote-python",
        remote_python,
        "--system-bk",
        system_bk,
    ]
    if sudo:
        options.append("--sudo")
    if include_journal:
        options.append("--include-journal")
    if source_revision is not None:
        options.extend(("--build-source", "--source-revision", source_revision))
    if live_gpu:
        options.extend(
            (
                "--live-gpu",
                "--live-seconds",
                str(live_seconds),
                "--live-python",
                live_python,
            )
        )
    if download_wheelhouse:
        options.append("--download-wheelhouse")
    quoted_options = " ".join(shlex.quote(value) for value in options)
    return (
        'set -eu; stage="$HOME/'
        + relative_stage
        + '"; exec '
        + shlex.quote(launcher_python)
        + (
            ' "$stage/source/tools/acceptance_remote.py" --stage "$stage" '
            if source_revision is not None
            else ' "$stage/acceptance_remote.py" --stage "$stage" '
        )
        + quoted_options
    )


def cleanup_command(relative_stage: str, remote_python: str) -> str:
    return (
        'stage="$HOME/'
        + relative_stage
        + '"; exec '
        + shlex.quote(remote_python)
        + " -c "
        + shlex.quote(CLEANUP_CODE)
        + ' "$stage"'
    )


def download_report(
    settings: SshSettings,
    relative_stage: str,
    output: Path,
) -> tuple[Path, dict[str, Any]]:
    prepare_report_output(output)
    archive_part = output / "report.tar.gz.part"
    digest_part = output / "report.tar.gz.sha256.part"
    for remote_name, local_path in (
        ("report.tar.gz", archive_part),
        ("report.tar.gz.sha256", digest_part),
    ):
        source = f"{settings.target}:{relative_stage}/{remote_name}"
        result = subprocess.run(
            [*settings.scp_argv(), source, str(local_path)], check=False
        )
        if result.returncode != 0:
            raise AcceptanceError(
                f"report download failed for {remote_name} ({result.returncode})"
            )
    expected = digest_part.read_text(encoding="ascii").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise AcceptanceError("remote report SHA-256 file is invalid")
    actual = sha256_file(archive_part)
    if actual != expected:
        raise AcceptanceError("downloaded report SHA-256 does not match")
    archive = output / "report.tar.gz"
    digest_path = output / "report.tar.gz.sha256"
    os.replace(archive_part, archive)
    os.replace(digest_part, digest_path)
    payload = extract_report(archive, output)
    return archive, payload


def prepare_report_output(output: Path) -> None:
    root = output.parent
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise AcceptanceError(f"report root is not a real directory: {root}")
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
    output.mkdir(mode=0o700)


def extract_report(archive: Path, output: Path) -> dict[str, Any]:
    report_dir = output / "gpubk-acceptance"
    report_dir.mkdir(mode=0o700)
    allowed = {
        "gpubk-acceptance/README.txt",
        "gpubk-acceptance/acceptance.json",
        "gpubk-acceptance/bundle-manifest.json",
    }
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise AcceptanceError("report archive contains duplicate members")
        for member in members:
            if not member.isfile() or member.name not in allowed:
                raise AcceptanceError(f"unsafe report archive member: {member.name}")
            name = member.name.removeprefix(REPORT_MEMBER_ROOT)
            if not name:
                raise AcceptanceError("report archive contains an empty filename")
            source = bundle.extractfile(member)
            if source is None:
                raise AcceptanceError(f"cannot read report member: {member.name}")
            destination = report_dir / name
            with source, destination.open("xb") as handle:
                shutil.copyfileobj(source, handle)
            destination.chmod(0o600)
    acceptance = report_dir / "acceptance.json"
    if not acceptance.is_file():
        raise AcceptanceError("report archive has no acceptance.json")
    try:
        payload = json.loads(acceptance.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"acceptance.json is invalid: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "gpubk.acceptance.v1"
    ):
        raise AcceptanceError("acceptance report schema is invalid")
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Fetch the current committed GPUBK revision from GitHub on a GPU host, "
            "test it in an isolated directory, inspect the deployed services, and "
            "retrieve a verified report. Production stays read-only unless "
            "--live-gpu or --full is explicitly selected."
        )
    )
    result.add_argument(
        "target", type=validate_target, help="SSH target, such as user@gpu-host"
    )
    result.add_argument(
        "--version",
        default=source_version(),
        help="expected package version (default: version declared by this checkout)",
    )
    result.add_argument("--output-dir", type=Path, default=ROOT / "acceptance-reports")
    result.add_argument("--remote-python", type=validate_executable, default="python3")
    result.add_argument("--system-bk", type=validate_executable, default="bk")
    result.add_argument("--port", type=int)
    result.add_argument("--identity", type=Path)
    result.add_argument(
        "-o", "--ssh-option", type=validate_option, action="append", default=[]
    )
    result.add_argument(
        "--sudo", action="store_true", help="prompt for remote sudo read-only checks"
    )
    result.add_argument(
        "--full",
        action="store_true",
        help=(
            "run sudo inspection, GPUBK-only journal checks, and one bounded "
            "workload on an idle booked GPU"
        ),
    )
    result.add_argument(
        "--include-journal",
        action="store_true",
        help="include the last 80 lines from GPUBK service units (requires --sudo)",
    )
    result.add_argument(
        "--live-gpu",
        action="store_true",
        help=(
            "book one idle production GPU, run a bounded CUDA workload, verify "
            "usage attribution, and remove the booking"
        ),
    )
    result.add_argument(
        "--live-seconds",
        type=int,
        default=65,
        help="live workload duration in seconds, 20-180 (default: 65)",
    )
    result.add_argument(
        "--live-python",
        type=validate_executable,
        default="auto",
        help=(
            "remote CUDA Python containing PyTorch; auto checks common user "
            "environments before booking (default: auto)"
        ),
    )
    candidate = result.add_mutually_exclusive_group()
    candidate.add_argument(
        "--source",
        dest="source",
        action="store_true",
        help="fetch and test the current committed checkout from GitHub (default)",
    )
    candidate.add_argument(
        "--release",
        dest="source",
        action="store_false",
        help="test the exact public PyPI release selected by --version",
    )
    result.set_defaults(source=None)
    result.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help="HTTPS Git repository used by --source",
    )
    result.add_argument("--wheelhouse", type=Path, help="use a prepared wheelhouse")
    result.add_argument(
        "--skip-index-digest-check",
        action="store_true",
        help="trust a supplied wheelhouse without comparing each wheel to public PyPI",
    )
    result.add_argument(
        "--keep-remote", action="store_true", help="retain private remote stage"
    )
    result.add_argument(
        "--dry-run", action="store_true", help="show the run without network access"
    )
    return result


def print_summary(payload: dict[str, Any], output: Path) -> None:
    counts = payload.get("counts", {})
    print("", flush=True)
    print(
        f"Automated result: {str(payload.get('result', 'unknown')).upper()}", flush=True
    )
    print(
        "Checks: "
        f"{counts.get('pass', 0)} pass, {counts.get('warn', 0)} warning, "
        f"{counts.get('fail', 0)} fail, {counts.get('skip', 0)} skipped",
        flush=True,
    )
    print(f"Local report: {output}", flush=True)
    checks = payload.get("checks")
    if isinstance(checks, list):
        notable = [
            item
            for item in checks
            if isinstance(item, dict) and item.get("status") in {"fail", "warn"}
        ]
        if notable:
            print("Review these checks:", flush=True)
            for item in notable:
                print(
                    f"  {str(item.get('status')).upper():4} "
                    f"{item.get('id')}: {item.get('summary')}",
                    flush=True,
                )
    manual_checks = payload.get("manual_checks")
    if not isinstance(manual_checks, list):
        print("Manual check status is unavailable in this report.")
        return
    pending = [
        str(item.get("id"))
        for item in manual_checks
        if isinstance(item, dict) and item.get("status") != "passed"
    ]
    if pending:
        print("Manual checks still required: " + ", ".join(pending) + ".")
    else:
        print("No manual checks remain in this report.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.source is None:
        args.source = args.wheelhouse is None
    if args.full:
        args.sudo = True
        args.include_journal = True
        args.live_gpu = True
    if args.include_journal and not args.sudo:
        parser().error("--include-journal requires --sudo")
    if args.skip_index_digest_check and args.wheelhouse is None:
        parser().error("--skip-index-digest-check requires --wheelhouse")
    if args.source and args.wheelhouse is not None:
        parser().error("Git source mode cannot be combined with --wheelhouse")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser().error("--port must be between 1 and 65535")
    if not 20 <= args.live_seconds <= 180:
        parser().error("--live-seconds must be between 20 and 180")
    if args.identity is not None:
        args.identity = args.identity.expanduser().resolve(strict=True)
        if not args.identity.is_file():
            parser().error("--identity must be a regular file")
    version = args.version.strip()
    if not version or any(character.isspace() for character in version):
        parser().error("--version must be one exact package version")

    revision = source_revision() if args.source else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(
        6
    )
    relative_stage = remote_stage(run_id)
    settings = SshSettings(
        target=args.target,
        port=args.port,
        identity=args.identity,
        options=tuple(args.ssh_option),
    )
    output = (
        args.output_dir.expanduser().resolve()
        / f"{args.target.replace('@', '_')}-{run_id}"
    )
    print(f"version: {version}")
    print(f"target: {args.target}")
    print(f"remote stage: ~/{relative_stage}")
    print(
        f"candidate source: {args.repository}@{revision} (fetched on GPU host)"
        if args.source
        else "release source: public PyPI, with automatic remote-host fallback"
    )
    print(f"local report: {output}")
    print("candidate install: isolated remote cache; deployed GPUBK is not upgraded")
    if args.dry_run:
        if args.live_gpu:
            print(
                "production changes: one short booking plus append-only audit and "
                "usage records; the active booking is removed"
            )
        else:
            print("production changes: none")
        return 0

    require_local_commands()
    if not REMOTE_RUNNER.is_file() or REMOTE_RUNNER.is_symlink():
        raise AcceptanceError(f"remote runner is missing or unsafe: {REMOTE_RUNNER}")

    stage_created = False
    report_downloaded = False
    remote_result: subprocess.CompletedProcess[str] | None = None
    error: Exception | None = None
    with tempfile.TemporaryDirectory(prefix="gpubk-acceptance-") as raw_work:
        work = Path(raw_work)
        wheelhouse = work / "wheelhouse"
        try:
            remote_download = False
            bundle: Path | None = None
            if not args.source:
                try:
                    wheels = prepare_wheelhouse(
                        wheelhouse,
                        version,
                        args.wheelhouse,
                        verify_index=not args.skip_index_digest_check,
                    )
                except DownloadUnavailable as exc:
                    if args.wheelhouse is not None:
                        raise
                    remote_download = True
                    print(
                        f"Local PyPI unavailable ({exc}); using {args.target} instead.",
                        flush=True,
                    )
                else:
                    manifest = build_manifest(run_id, version, REMOTE_RUNNER, wheels)
                    bundle = build_bundle(work, manifest, REMOTE_RUNNER, wheels)
                    print(f"Bundle SHA256: {sha256_file(bundle)}", flush=True)

            setup = run_ssh(settings, remote_setup_command(run_id), capture=True)
            require_ssh_success(setup, "remote stage creation")
            stage_created = True
            if args.source:
                print(
                    f"Fetching exact candidate {revision[:12]} on {args.target}...",
                    flush=True,
                )
                source_fetch = run_ssh(
                    settings,
                    remote_source_command(
                        relative_stage,
                        repository=args.repository,
                        revision=revision,
                    ),
                    capture=True,
                )
                require_ssh_success(source_fetch, "remote source fetch")
            elif remote_download:
                print(
                    f"Uploading runner; {args.target} will download verified wheels...",
                    flush=True,
                )
                upload_bootstrap(
                    settings, REMOTE_RUNNER, LOCAL_RUNNER, relative_stage
                )
            else:
                if bundle is None:
                    raise AcceptanceError("local bundle was not prepared")
                print(f"Uploading private bundle to {args.target}...", flush=True)
                upload_bundle(settings, bundle, relative_stage)
                extraction = run_ssh(
                    settings,
                    extraction_command(relative_stage, "python3"),
                    capture=True,
                )
                require_ssh_success(extraction, "remote bundle extraction")
            print(
                "Running isolated candidate and deployed-service checks...", flush=True
            )
            remote_result = run_ssh(
                settings,
                runner_command(
                    relative_stage,
                    run_id=run_id,
                    version=version,
                    remote_python=args.remote_python,
                    system_bk=args.system_bk,
                    sudo=args.sudo,
                    include_journal=args.include_journal,
                    source_revision=revision,
                    live_gpu=args.live_gpu,
                    live_seconds=args.live_seconds,
                    live_python=args.live_python,
                    download_wheelhouse=remote_download,
                    launcher_python="python3",
                ),
                tty=args.sudo,
            )
            archive, payload = download_report(settings, relative_stage, output)
            del archive
            report_downloaded = True
            print_summary(payload, output)
        except (AcceptanceError, OSError, subprocess.TimeoutExpired) as exc:
            error = exc
        finally:
            if stage_created and not args.keep_remote:
                cleanup = run_ssh(
                    settings,
                    cleanup_command(relative_stage, args.remote_python),
                    capture=True,
                )
                if cleanup.returncode != 0 and args.remote_python != "python3":
                    cleanup = run_ssh(
                        settings,
                        cleanup_command(relative_stage, "python3"),
                        capture=True,
                    )
                if cleanup.returncode != 0 and error is None:
                    error = AcceptanceError("remote temporary-directory cleanup failed")
                elif cleanup.returncode != 0:
                    print(
                        f"warning: remote cleanup failed; inspect {args.target}:~/{relative_stage}",
                        file=sys.stderr,
                    )
            elif stage_created:
                print(
                    f"Remote stage retained: {args.target}:~/{relative_stage}",
                    flush=True,
                )

    if error is not None:
        raise error
    if not report_downloaded or remote_result is None:
        raise AcceptanceError("acceptance run ended without a verified report")
    return remote_result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AcceptanceError, OSError, subprocess.SubprocessError) as exc:
        print(f"gpubk acceptance: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
