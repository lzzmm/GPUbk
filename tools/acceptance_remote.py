#!/usr/bin/env python3
"""Run privacy-conscious GPUBK acceptance checks on a remote Linux host."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


SCHEMA_VERSION = "gpubk.acceptance.v1"
MANIFEST_SCHEMA = "gpubk.acceptance-bundle.v1"
MAX_CAPTURE_CHARS = 64 * 1024
SYSTEM_SERVICES = ("gpubk-broker.service", "gpubk-monitor.service")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bounded_text(value: str) -> str:
    if len(value) <= MAX_CAPTURE_CHARS:
        return value
    half = MAX_CAPTURE_CHARS // 2
    omitted = len(value) - (half * 2)
    return value[:half] + f"\n... {omitted} characters omitted ...\n" + value[-half:]


@dataclass(frozen=True)
class CommandOutcome:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


def execute(
    argv: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 90.0,
) -> CommandOutcome:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return CommandOutcome(
            argv=tuple(str(part) for part in argv),
            returncode=completed.returncode,
            stdout=bounded_text(completed.stdout),
            stderr=bounded_text(completed.stderr),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else exc.stdout or ""
        )
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else exc.stderr or ""
        )
        return CommandOutcome(
            argv=tuple(str(part) for part in argv),
            returncode=124,
            stdout=bounded_text(stdout),
            stderr=bounded_text(stderr),
            duration_ms=round((time.monotonic() - started) * 1000),
            timed_out=True,
        )


class AcceptanceReport:
    def __init__(self, *, run_id: str, version: str) -> None:
        self.run_id = run_id
        self.version = version
        self.started_at = utc_now()
        self.checks: list[dict[str, Any]] = []
        self.bundle_manifest: dict[str, Any] | None = None

    def add(
        self,
        check_id: str,
        *,
        status: str,
        critical: bool,
        summary: str,
        outcome: CommandOutcome | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"pass", "warn", "fail", "skip"}:
            raise ValueError(f"invalid check status: {status}")
        item: dict[str, Any] = {
            "id": check_id,
            "status": status,
            "critical": critical,
            "summary": summary,
        }
        if outcome is not None:
            item["command"] = list(outcome.argv)
            item["returncode"] = outcome.returncode
            item["duration_ms"] = outcome.duration_ms
            item["timed_out"] = outcome.timed_out
            item["stdout"] = outcome.stdout
            item["stderr"] = outcome.stderr
        if details:
            item["details"] = details
        self.checks.append(item)
        print(f"[{status.upper():4}] {check_id}: {summary}", flush=True)

    def command(
        self,
        check_id: str,
        argv: Sequence[str],
        *,
        critical: bool = True,
        expected_codes: Iterable[int] = (0,),
        validator: Callable[[CommandOutcome], bool] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 90.0,
        success: str = "command completed",
        failure: str = "command failed",
    ) -> CommandOutcome:
        outcome = execute(argv, env=env, timeout=timeout)
        passed = outcome.returncode in set(expected_codes)
        if passed and validator is not None:
            try:
                passed = bool(validator(outcome))
            except (TypeError, ValueError):
                passed = False
        self.add(
            check_id,
            status="pass" if passed else ("fail" if critical else "warn"),
            critical=critical,
            summary=success if passed else failure,
            outcome=outcome,
        )
        return outcome

    @property
    def result(self) -> str:
        if any(item["critical"] and item["status"] == "fail" for item in self.checks):
            return "fail"
        if any(item["status"] in {"warn", "fail"} for item in self.checks):
            return "warn"
        return "pass"

    def payload(self) -> dict[str, Any]:
        journal_included = any(
            item["id"] == "system.journal"
            and item["status"] == "pass"
            and bool(item.get("stdout"))
            for item in self.checks
        )
        counts = {
            status: sum(item["status"] == status for item in self.checks)
            for status in ("pass", "warn", "fail", "skip")
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "remote_acceptance",
            "run_id": self.run_id,
            "expected_version": self.version,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "result": self.result,
            "counts": counts,
            "host": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "uid": os.getuid(),
            },
            "privacy": {
                "raw_ledger_included": False,
                "process_commands_included": False,
                "other_user_names_included": journal_included,
                "journal_included": journal_included,
                "journal_may_contain_user_content": journal_included,
            },
            "manual_checks": [
                {
                    "id": "tui",
                    "status": "pending",
                    "instruction": "Open `bk t` and inspect layout, colors, navigation, add, and edit.",
                },
                {
                    "id": "second-user-authz",
                    "status": "pending",
                    "instruction": "Use a second real UID to confirm view access and denied cross-UID edits.",
                },
                {
                    "id": "live-gpu-workload",
                    "status": "pending",
                    "instruction": "Run an approved tiny booked workload on an idle GPU and verify attribution.",
                },
                {
                    "id": "boot-persistence",
                    "status": "pending",
                    "instruction": "At the next maintenance reboot, verify both GPUBK services return active.",
                },
            ],
            "bundle": self.bundle_manifest,
            "checks": self.checks,
        }


def parse_json_output(outcome: CommandOutcome) -> dict[str, Any] | None:
    try:
        payload = json.loads(outcome.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def add_semantic_check(
    report: AcceptanceReport,
    check_id: str,
    passed: bool,
    summary: str,
    *,
    critical: bool = True,
    details: dict[str, Any] | None = None,
) -> None:
    report.add(
        check_id,
        status="pass" if passed else ("fail" if critical else "warn"),
        critical=critical,
        summary=summary,
        details=details,
    )


def validate_stage(stage: Path) -> Path:
    stage = stage.expanduser()
    if not stage.is_absolute():
        raise ValueError("stage must be an absolute path")
    metadata = stage.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("stage is not a real directory")
    stage = stage.resolve(strict=True)
    expected_root = (Path.home() / ".cache" / "gpubk" / "acceptance").resolve()
    if stage.parent != expected_root:
        raise ValueError(f"stage must be one directory below {expected_root}")
    if metadata.st_uid != os.getuid():
        raise ValueError("stage is not owned by the current UID")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("stage is accessible by group or other users")
    return stage


def load_and_verify_bundle(
    stage: Path, version: str, run_id: str | None = None
) -> dict[str, Any]:
    manifest_path = stage / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("bundle manifest is missing or unsafe")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != MANIFEST_SCHEMA
    ):
        raise ValueError("bundle manifest schema is invalid")
    if payload.get("version") != version:
        raise ValueError("bundle version does not match the requested version")
    if run_id is not None and payload.get("run_id") != run_id:
        raise ValueError("bundle run ID does not match the requested run")
    expected = payload.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("bundle manifest has no files")

    wheelhouse = stage / "wheelhouse"
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("wheelhouse is missing or unsafe")
    actual_paths = list(wheelhouse.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in actual_paths):
        raise ValueError("wheelhouse contains a non-regular file")
    runner_path = stage / "acceptance_remote.py"
    if runner_path.is_symlink() or not runner_path.is_file():
        raise ValueError("remote acceptance runner is missing or unsafe")
    actual_names = {"acceptance_remote.py"}
    actual_names.update(f"wheelhouse/{path.name}" for path in actual_paths)
    if actual_names != set(expected):
        raise ValueError("wheelhouse file set differs from the bundle manifest")
    for relative, metadata in expected.items():
        if not isinstance(relative, str) or relative not in actual_names:
            raise ValueError("bundle manifest contains an unsafe path")
        if relative == "acceptance_remote.py":
            path = runner_path
        else:
            name = relative.removeprefix("wheelhouse/")
            if name in {"", ".", ".."} or "/" in name or "\\" in name:
                raise ValueError("bundle manifest contains an unsafe filename")
            path = wheelhouse / name
        if not isinstance(metadata, dict):
            raise ValueError("bundle manifest file metadata is invalid")
        if path.stat().st_size != metadata.get("size"):
            raise ValueError(f"bundle size mismatch for {relative}")
        if sha256_file(path) != metadata.get("sha256"):
            raise ValueError(f"bundle digest mismatch for {relative}")
    return payload


def download_verified_wheelhouse(
    report: AcceptanceReport,
    stage: Path,
    *,
    version: str,
    run_id: str,
    remote_python: str,
) -> None:
    """Build the normal verified bundle manifest using PyPI from this host."""
    downloader_path = stage / "remote_acceptance.py"
    if downloader_path.is_symlink() or not downloader_path.is_file():
        raise ValueError("remote download helper is missing or unsafe")
    spec = importlib.util.spec_from_file_location(
        "gpubk_remote_download_helper", downloader_path
    )
    if spec is None or spec.loader is None:
        raise ValueError("remote download helper could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        wheelhouse = stage / "wheelhouse"
        wheels = module.prepare_wheelhouse(
            wheelhouse,
            version,
            None,
            verify_index=True,
            python_executable=remote_python,
        )
        manifest = module.build_manifest(
            run_id, version, stage / "acceptance_remote.py", wheels
        )
        manifest["source"] = "public-pypi-on-gpu-host"
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        report.add(
            "bundle.remote-download",
            status="pass",
            critical=True,
            summary="GPU host downloaded and verified the exact PyPI wheelhouse",
        )
    finally:
        sys.modules.pop(spec.name, None)
        downloader_path.unlink(missing_ok=True)


def find_command(value: str) -> str | None:
    if "/" in value:
        path = Path(value).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(value)


def isolated_environment(
    stage: Path, site: Path, *, gpu_count: int | None = None
) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("BK_")}
    env["PYTHONPATH"] = str(site)
    env["PYTHONNOUSERSITE"] = "1"
    env["BK_DATA_DIR"] = str(stage / "isolated-data")
    env["BK_JOB_LOG_DIR"] = str(stage / "isolated-jobs")
    env["BK_MAX_SHARED_USERS"] = "4"
    env["BK_SLOT_MINUTES"] = "5"
    env["XDG_CONFIG_HOME"] = str(stage / "xdg-config")
    env["XDG_DATA_HOME"] = str(stage / "xdg-data")
    env["XDG_STATE_HOME"] = str(stage / "xdg-state")
    if gpu_count is not None:
        env["BK_GPU_COUNT"] = str(gpu_count)
    else:
        env.pop("BK_GPU_COUNT", None)
    return env


def run_isolated_checks(
    report: AcceptanceReport,
    *,
    stage: Path,
    remote_python: str,
    version: str,
) -> None:
    wheelhouse = stage / "wheelhouse"
    site = stage / "site"
    site.mkdir(mode=0o700)
    install = report.command(
        "candidate.install",
        [
            remote_python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--target",
            str(site),
            f"gpubk[gpu]=={version}",
        ],
        timeout=180,
        success="candidate and GPU dependency installed into the private stage",
        failure="offline candidate installation failed",
    )
    if install.returncode != 0:
        report.add(
            "candidate.runtime",
            status="skip",
            critical=True,
            summary="candidate checks skipped because isolated installation failed",
        )
        return
    staged_bk = [remote_python, "-m", "bk"]
    base_env = isolated_environment(stage, site)
    report.command(
        "candidate.version",
        [*staged_bk, "--version"],
        env=base_env,
        validator=lambda result: result.stdout.strip() == f"bk {version}",
        success=f"candidate reports bk {version}",
        failure="candidate version does not match the downloaded release",
    )
    report.command(
        "candidate.help",
        [*staged_bk, "--help"],
        env=base_env,
        validator=lambda result: (
            "GPUBK" in result.stdout and "bk doctor" in result.stdout
        ),
        success="candidate CLI help rendered",
        failure="candidate CLI help is incomplete",
    )

    gpu_query = report.command(
        "gpu.inventory",
        [
            "nvidia-smi",
            "--query-gpu=index,name,driver_version,memory.total,memory.used,utilization.gpu,compute_mode",
            "--format=csv,noheader,nounits",
        ],
        validator=lambda result: bool(result.stdout.strip()),
        success="real GPU inventory is readable without exposing UUIDs or process commands",
        failure="nvidia-smi GPU inventory is unavailable",
    )
    gpu_count = (
        len([line for line in gpu_query.stdout.splitlines() if line.strip()])
        if gpu_query.returncode == 0
        else 0
    )
    add_semantic_check(
        report,
        "gpu.count",
        gpu_count > 0,
        f"detected {gpu_count} GPU(s)" if gpu_count else "no GPUs were detected",
        details={"gpu_count": gpu_count},
    )

    hardware_data = stage / "hardware-probe-data"
    hardware_data.mkdir(mode=0o700)
    hardware_env = isolated_environment(stage, site)
    hardware_env["BK_DATA_DIR"] = str(hardware_data)
    report.command(
        "candidate.hardware-probe",
        [*staged_bk, "doctor", "--probe", "--json", "--strict"],
        env=hardware_env,
        timeout=120,
        success="candidate passed strict atomic, locking, NVML, and identity probes",
        failure="candidate strict hardware probe failed",
    )

    if gpu_count <= 0:
        report.add(
            "candidate.scheduler",
            status="skip",
            critical=True,
            summary="scheduler smoke test skipped because no GPU topology was detected",
        )
        return

    scheduler_env = isolated_environment(stage, site, gpu_count=gpu_count)
    scheduler_data = Path(scheduler_env["BK_DATA_DIR"])
    scheduler_data.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    start = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    start_text = start.isoformat().replace("+00:00", "Z")

    first = report.command(
        "scheduler.shared-first",
        [*staged_bk, "1", "10m", "--start", start_text, "--json"],
        env=scheduler_env,
        success="first shared reservation was created in the isolated ledger",
        failure="first isolated shared reservation failed",
    )
    first_payload = parse_json_output(first)
    first_reservation = (
        first_payload.get("reservation") if isinstance(first_payload, dict) else None
    )
    gpu = None
    if isinstance(first_reservation, dict):
        gpus = first_reservation.get("gpus")
        if isinstance(gpus, list) and len(gpus) == 1 and isinstance(gpus[0], int):
            gpu = gpus[0]

    created_ids: list[str] = []
    if isinstance(first_reservation, dict) and isinstance(
        first_reservation.get("id"), str
    ):
        created_ids.append(first_reservation["id"])
    if gpu is None:
        add_semantic_check(
            report, "scheduler.selection", False, "first booking returned no usable GPU"
        )
        return

    second = report.command(
        "scheduler.shared-overlap",
        [*staged_bk, "1", "5m", "--gpu", str(gpu), "--start", start_text, "--json"],
        env=scheduler_env,
        success="a second shared record overlapped within integer slot capacity",
        failure="valid shared overlap was rejected",
    )
    second_payload = parse_json_output(second)
    second_reservation = (
        second_payload.get("reservation") if isinstance(second_payload, dict) else None
    )
    if isinstance(second_reservation, dict) and isinstance(
        second_reservation.get("id"), str
    ):
        created_ids.append(second_reservation["id"])

    conflict = report.command(
        "scheduler.explicit-exclusive-conflict",
        [
            *staged_bk,
            "x",
            "1",
            "10m",
            "--gpu",
            str(gpu),
            "--start",
            start_text,
            "--json",
        ],
        env=scheduler_env,
        expected_codes=range(1, 256),
        validator=lambda result: (
            "conflict" in (result.stdout + result.stderr).lower()
            or "available" in (result.stdout + result.stderr).lower()
        ),
        success="explicit exclusive overlap was rejected without moving the requested time",
        failure="explicit exclusive conflict was not rejected clearly",
    )
    del conflict

    queued = report.command(
        "scheduler.implicit-exclusive-queue",
        [*staged_bk, "x", "1", "10m", "--gpu", str(gpu), "--json"],
        env=scheduler_env,
        success="implicit exclusive request found a queueable future slot",
        failure="implicit exclusive request could not queue",
    )
    queued_payload = parse_json_output(queued)
    queued_reservation = (
        queued_payload.get("reservation") if isinstance(queued_payload, dict) else None
    )
    if isinstance(queued_reservation, dict) and isinstance(
        queued_reservation.get("id"), str
    ):
        created_ids.append(queued_reservation["id"])

    semantics_ok = (
        isinstance(first_reservation, dict)
        and isinstance(second_reservation, dict)
        and isinstance(queued_reservation, dict)
        and first_reservation.get("id") != second_reservation.get("id")
        and first_reservation.get("start_at") == second_reservation.get("start_at")
        and first_reservation.get("mode") == "shared"
        and second_reservation.get("mode") == "shared"
        and queued_reservation.get("mode") == "exclusive"
        and first_payload.get("status") == "created"
        and second_payload.get("status") == "created"
        and queued_payload.get("status") == "queued"
        and str(queued_reservation.get("start_at", ""))
        >= str(first_reservation.get("end_at", ""))
    )
    add_semantic_check(
        report,
        "scheduler.semantics",
        semantics_ok,
        "shared overlap and exclusive queue semantics match policy"
        if semantics_ok
        else "isolated reservation payloads violate expected scheduling semantics",
    )

    report.command(
        "scheduler.ledger-health",
        [*staged_bk, "doctor", "--json", "--strict"],
        env=scheduler_env,
        success="isolated ledger remained healthy after scheduling tests",
        failure="isolated ledger failed strict validation",
    )
    for index, reservation_id in enumerate(created_ids, 1):
        report.command(
            f"scheduler.cleanup-{index}",
            [*staged_bk, "d", reservation_id],
            env=scheduler_env,
            critical=False,
            success="isolated reservation removed",
            failure="isolated reservation cleanup failed",
        )


def system_bk_command(value: str) -> str | None:
    return find_command(value)


def deployment_paths(effective: dict[str, Any]) -> list[str]:
    paths: list[Path] = []
    for key, include_parent in (
        ("config_file", True),
        ("data_dir", False),
        ("broker_socket", True),
    ):
        raw = effective.get(key)
        if raw is None and key == "broker_socket":
            continue
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"effective configuration has no usable {key}")
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError(f"effective {key} is not absolute")
        if include_parent:
            paths.append(path.parent)
        paths.append(path)
    return list(dict.fromkeys(str(path) for path in paths))


def run_system_checks(
    report: AcceptanceReport,
    *,
    system_bk: str,
    version: str,
    sudo_ready: bool,
    include_journal: bool,
) -> None:
    bk_path = system_bk_command(system_bk)
    if bk_path is None:
        report.add(
            "system.command",
            status="fail",
            critical=True,
            summary=f"deployed bk command is not executable: {system_bk}",
        )
        return
    report.add(
        "system.command",
        status="pass",
        critical=True,
        summary="deployed bk command is executable",
        details={"path": bk_path},
    )
    report.command(
        "system.version",
        [bk_path, "--version"],
        validator=lambda result: result.stdout.strip() == f"bk {version}",
        success=f"deployed command matches candidate version {version}",
        failure=f"deployed command is not candidate version {version}",
    )
    config_result = report.command(
        "system.config",
        [bk_path, "config", "--json"],
        success="deployed configuration is readable",
        failure="deployed configuration could not be read",
    )
    config_payload = parse_json_output(config_result)
    effective = (
        config_payload.get("effective") if isinstance(config_payload, dict) else None
    )
    add_semantic_check(
        report,
        "system.config-schema",
        isinstance(effective, dict),
        "deployed configuration has the expected structured schema"
        if isinstance(effective, dict)
        else "deployed configuration output is not structured JSON",
    )

    report.command(
        "system.user-probe",
        [bk_path, "doctor", "--probe", "--json", "--strict"],
        timeout=120,
        success="login user passed strict deployment and broker checks",
        failure="login user strict deployment probe failed",
    )
    report.command(
        "system.monitor-health",
        [bk_path, "doctor", "--require-monitor", "--json", "--strict"],
        timeout=120,
        success="deployed monitor heartbeat and attribution state are healthy",
        failure="deployed monitor health check failed",
    )
    report.command(
        "system.worker-health",
        [bk_path, "doctor", "--require-worker", "--json", "--strict"],
        critical=False,
        timeout=120,
        success="this user's optional scheduled-command worker is healthy",
        failure="this user's optional worker is absent or unhealthy",
    )

    monitor_uid = effective.get("monitor_uid") if isinstance(effective, dict) else None
    if isinstance(monitor_uid, int) and monitor_uid != os.getuid():
        if sudo_ready:
            report.command(
                "system.service-account-probe",
                [
                    "sudo",
                    "-n",
                    "-H",
                    "-u",
                    f"#{monitor_uid}",
                    bk_path,
                    "doctor",
                    "--probe",
                    "--require-monitor",
                    "--json",
                    "--strict",
                ],
                timeout=120,
                success="configured monitor UID passed strict writable probes",
                failure="configured monitor UID strict probe failed",
            )
        else:
            report.add(
                "system.service-account-probe",
                status="warn",
                critical=False,
                summary="monitor UID differs from login UID; rerun with --sudo to test it",
                details={"monitor_uid": monitor_uid, "login_uid": os.getuid()},
            )
    elif isinstance(monitor_uid, int):
        report.add(
            "system.service-account-probe",
            status="pass",
            critical=True,
            summary="login UID is the configured monitor UID and was probed above",
            details={"monitor_uid": monitor_uid},
        )

    for service in SYSTEM_SERVICES:
        report.command(
            f"systemd.{service}.active",
            ["systemctl", "is-active", service],
            validator=lambda result: result.stdout.strip() == "active",
            success=f"{service} is active",
            failure=f"{service} is not active",
        )
        report.command(
            f"systemd.{service}.enabled",
            ["systemctl", "is-enabled", service],
            validator=lambda result: result.stdout.strip() == "enabled",
            success=f"{service} is enabled for boot",
            failure=f"{service} is not enabled for boot",
        )
        report.command(
            f"systemd.{service}.properties",
            [
                "systemctl",
                "show",
                service,
                "--property=ActiveState,SubState,MainPID,User,Group,ExecMainStatus,NRestarts,MemoryCurrent,CPUUsageNSec",
            ],
            critical=False,
            success=f"{service} runtime properties captured",
            failure=f"{service} runtime properties unavailable",
        )

    unit_paths = [Path("/etc/systemd/system") / service for service in SYSTEM_SERVICES]
    if all(path.is_file() and not path.is_symlink() for path in unit_paths):
        report.command(
            "systemd.unit-verify",
            ["systemd-analyze", "verify", *(str(path) for path in unit_paths)],
            critical=False,
            success="tracked systemd units passed systemd-analyze verification",
            failure="systemd-analyze reported unit errors or host-level warnings",
        )
    else:
        report.add(
            "systemd.unit-verify",
            status="warn",
            critical=False,
            summary="one or more expected /etc/systemd/system unit files are missing",
        )

    if sudo_ready:
        try:
            if not isinstance(effective, dict):
                raise ValueError("effective configuration is unavailable")
            trusted_paths = deployment_paths(effective)
        except ValueError as exc:
            report.add(
                "system.paths",
                status="fail",
                critical=True,
                summary=f"trusted deployment paths are invalid: {exc}",
            )
        else:
            report.command(
                "system.paths",
                [
                    "sudo",
                    "-n",
                    "stat",
                    "-c",
                    "%n mode=%a uid=%u gid=%g type=%F",
                    "--",
                    *trusted_paths,
                ],
                success="configured trusted path ownership captured",
                failure="configured trusted paths could not be inspected",
            )
    if include_journal:
        if sudo_ready:
            report.command(
                "system.journal",
                [
                    "sudo",
                    "-n",
                    "journalctl",
                    "--no-pager",
                    "--output=short-iso",
                    "--lines=80",
                    "-u",
                    SYSTEM_SERVICES[0],
                    "-u",
                    SYSTEM_SERVICES[1],
                ],
                critical=False,
                success="recent GPUBK-only service journal captured",
                failure="GPUBK service journal could not be read",
            )
        else:
            report.add(
                "system.journal",
                status="warn",
                critical=False,
                summary="--include-journal requires --sudo",
            )


def acquire_sudo(report: AcceptanceReport, requested: bool) -> bool:
    if not requested:
        report.add(
            "sudo",
            status="skip",
            critical=False,
            summary="privileged inspection was not requested",
        )
        return False
    print("Remote sudo authentication may prompt now.", flush=True)
    started = time.monotonic()
    completed = subprocess.run(["sudo", "-v"], check=False)
    outcome = CommandOutcome(
        argv=("sudo", "-v"),
        returncode=completed.returncode,
        stdout="",
        stderr="",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    report.add(
        "sudo",
        status="pass" if completed.returncode == 0 else "fail",
        critical=requested,
        summary="sudo credential cached for read-only privileged checks"
        if completed.returncode == 0
        else "sudo authentication failed",
        outcome=outcome,
    )
    return completed.returncode == 0


def report_readme(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    lines = [
        "GPUBK remote acceptance report",
        "",
        f"Result: {payload['result'].upper()}",
        f"Expected version: {payload['expected_version']}",
        f"Host: {payload['host']['hostname']}",
        (
            "Checks: "
            f"{counts['pass']} pass, {counts['warn']} warning, "
            f"{counts['fail']} fail, {counts['skip']} skipped"
        ),
        "",
        "This archive intentionally excludes raw ledgers, other users' names,",
        "GPU process command lines, and non-GPUBK journals.",
        "",
        "Automated checks do not replace the four manual checks listed in acceptance.json.",
    ]
    return "\n".join(lines) + "\n"


def write_report(stage: Path, report: AcceptanceReport) -> tuple[Path, str]:
    report_dir = stage / "report"
    if report_dir.exists():
        raise ValueError("report directory already exists")
    report_dir.mkdir(mode=0o700)
    payload = report.payload()
    acceptance_path = report_dir / "acceptance.json"
    acceptance_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    acceptance_path.chmod(0o600)
    readme_path = report_dir / "README.txt"
    readme_path.write_text(report_readme(payload), encoding="utf-8")
    readme_path.chmod(0o600)
    if report.bundle_manifest is not None:
        manifest_path = report_dir / "bundle-manifest.json"
        manifest_path.write_text(
            json.dumps(report.bundle_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)

    archive = stage / "report.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for path in sorted(report_dir.iterdir()):
            bundle.add(path, arcname=f"gpubk-acceptance/{path.name}", recursive=False)
    archive.chmod(0o600)
    digest = sha256_file(archive)
    digest_path = stage / "report.tar.gz.sha256"
    digest_path.write_text(digest + "\n", encoding="ascii")
    digest_path.chmod(0o600)
    return archive, digest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--stage", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--version", required=True)
    result.add_argument("--remote-python", default="python3")
    result.add_argument("--system-bk", default="bk")
    result.add_argument("--sudo", action="store_true")
    result.add_argument("--include-journal", action="store_true")
    result.add_argument("--download-wheelhouse", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = AcceptanceReport(run_id=args.run_id, version=args.version)
    exit_code = 2
    try:
        stage = validate_stage(args.stage)
        remote_python = find_command(args.remote_python)
        if remote_python is None:
            raise ValueError(f"remote Python is not executable: {args.remote_python}")
        if args.download_wheelhouse:
            download_verified_wheelhouse(
                report,
                stage,
                version=args.version,
                run_id=args.run_id,
                remote_python=remote_python,
            )
        report.bundle_manifest = load_and_verify_bundle(
            stage, args.version, args.run_id
        )
        report.add(
            "bundle",
            status="pass",
            critical=True,
            summary=(
                "wheelhouse matches the verified SHA-256 manifest"
                if args.download_wheelhouse
                else "uploaded wheelhouse matches the local SHA-256 manifest"
            ),
        )
        sudo_ready = acquire_sudo(report, args.sudo)
        run_isolated_checks(
            report,
            stage=stage,
            remote_python=remote_python,
            version=args.version,
        )
        run_system_checks(
            report,
            system_bk=args.system_bk,
            version=args.version,
            sudo_ready=sudo_ready,
            include_journal=args.include_journal,
        )
        exit_code = 2 if report.result == "fail" else 0
    except Exception as exc:  # noqa: BLE001 - report unexpected remote setup failures
        report.add(
            "runner",
            status="fail",
            critical=True,
            summary=f"acceptance runner failed safely: {type(exc).__name__}: {exc}",
        )
        exit_code = 3
    try:
        stage = validate_stage(args.stage)
        archive, digest = write_report(stage, report)
        print(f"Report: {archive}", flush=True)
        print(f"SHA256: {digest}", flush=True)
        print(f"Automated result: {report.result.upper()}", flush=True)
    except Exception as exc:  # noqa: BLE001 - final fallback must be visible to the operator
        print(
            f"could not write acceptance report: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
