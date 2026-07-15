#!/usr/bin/env python3
"""Run an isolated two-or-more-host GPUBK cluster acceptance test over SSH."""

from __future__ import annotations

import argparse
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
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SAFE_TARGET = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+")
SAFE_REMOTE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
SAFE_NODE_ID = re.compile(r"[0-9a-f]{20}")
REPORT_SCHEMA = "gpubk.cluster-acceptance.v1"
PROTECTED_SSH_OPTIONS = {
    "batchmode",
    "stricthostkeychecking",
    "numberofpasswordprompts",
    "clearallforwardings",
    "permitlocalcommand",
    "requesttty",
}


class ClusterAcceptanceError(RuntimeError):
    """An expected build, transport, protocol, or acceptance failure."""


@dataclass(frozen=True)
class SshTarget:
    value: str
    options: tuple[str, ...]

    def ssh_argv(self) -> list[str]:
        configured = {
            re.split(r"[ =]", option.strip(), maxsplit=1)[0].lower()
            for option in self.options
        }
        defaults = (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "NumberOfPasswordPrompts=0",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "RequestTTY=no",
            "ConnectTimeout=15",
            "ConnectionAttempts=1",
        )
        argv = ["ssh", "-T"]
        for option in defaults:
            key = option.partition("=")[0].lower()
            if key in PROTECTED_SSH_OPTIONS or key not in configured:
                argv.extend(("-o", option))
        for option in self.options:
            argv.extend(("-o", option))
        argv.extend(("--", self.value))
        return argv

    def scp_argv(self) -> list[str]:
        argv = [
            "scp",
            "-q",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "NumberOfPasswordPrompts=0",
        ]
        for option in self.options:
            argv.extend(("-o", option))
        return argv


@dataclass(frozen=True)
class RemoteNode:
    name: str
    target: SshTarget
    stage: str
    wrapper: str
    node_id: str
    version: str
    actor: dict[str, Any]


REMOTE_SETUP = """\
import pathlib, re, sys
run_id = sys.argv[1]
if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", run_id) is None:
    raise SystemExit("unsafe run ID")
home = pathlib.Path.home().resolve()
root = home / ".cache" / "gpubk" / "cluster-acceptance"
root.mkdir(mode=0o700, parents=True, exist_ok=True)
if root.is_symlink() or not root.resolve().is_relative_to(home):
    raise SystemExit("unsafe acceptance root")
root.chmod(0o700)
stage = root / run_id
stage.mkdir(mode=0o700)
print(stage.resolve())
"""


REMOTE_INSTALL = """\
import hashlib, json, pathlib, subprocess, sys
stage = pathlib.Path(sys.argv[1])
wheel = stage / sys.argv[2]
expected_digest = sys.argv[3]
digest = hashlib.sha256()
with wheel.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected_digest:
    raise SystemExit("candidate wheel digest mismatch")
venv = stage / "venv"
subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
python = venv / "bin" / "python"
subprocess.run(
    [str(python), "-m", "pip", "install", "--disable-pip-version-check",
     "--no-index", "--no-deps", str(wheel)],
    check=True,
    stdout=subprocess.DEVNULL,
)
simulation = stage / "gpu.json"
simulation.write_text(json.dumps({"gpus": [{
    "index": 0,
    "name": "cluster-acceptance",
    "uuid": "GPU-00000000-0000-0000-0000-000000000000",
    "memory_total_mb": 32768,
    "memory_used_mb": 0,
    "utilization_percent": 0,
    "temperature_c": 30,
    "processes": [],
}]}), encoding="utf-8")
simulation.chmod(0o600)
wrapper = stage / "bk-node"
wrapper.write_text(
    "#!" + str(python) + "\\n"
    "import os, sys\\n"
    "os.environ.pop('BK_CONFIG_FILE', None)\\n"
    "os.environ.update({"
    + repr("BK_DATA_DIR") + ": " + repr(str(stage / "data")) + ", "
    + repr("BK_JOB_LOG_DIR") + ": " + repr(str(stage / "jobs")) + ", "
    + repr("BK_GPU_COUNT") + ": '1', "
    + repr("BK_MAX_SHARED_USERS") + ": '2', "
    + repr("BK_GPU_SIM_FILE") + ": " + repr(str(simulation)) + ", "
    + repr("BK_CLUSTER_DISABLE") + ": '1'})\\n"
    "from bk.cli import main\\n"
    "raise SystemExit(main(sys.argv[1:]))\\n",
    encoding="utf-8",
)
wrapper.chmod(0o700)
print(json.dumps({"wrapper": str(wrapper)}))
"""


REMOTE_CLEANUP = """\
import pathlib, shutil, sys
stage = pathlib.Path(sys.argv[1])
root = pathlib.Path.home().resolve() / ".cache" / "gpubk" / "cluster-acceptance"
if stage.is_symlink() or stage.parent.resolve() != root.resolve():
    raise SystemExit("refusing unsafe cleanup path")
if stage.exists():
    shutil.rmtree(stage)
try:
    root.rmdir()
except OSError:
    pass
"""


def target_value(value: str) -> str:
    if SAFE_TARGET.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "SSH target must be USER@HOST or a configured host alias"
        )
    return value


def ssh_option(value: str) -> str:
    if not value or any(character in value for character in "\r\n\x00"):
        raise argparse.ArgumentTypeError(
            "SSH options cannot contain control characters"
        )
    key = re.split(r"[ =]", value.strip(), maxsplit=1)[0].lower()
    if key in PROTECTED_SSH_OPTIONS:
        raise argparse.ArgumentTypeError(
            f"SSH option {key} is fixed by the acceptance safety policy"
        )
    return value


def run_checked(
    argv: Sequence[str],
    *,
    timeout: float = 120,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClusterAcceptanceError(
            f"command timed out after {timeout:g}s: {shlex.join(argv)}"
        ) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ClusterAcceptanceError(
            f"command failed ({result.returncode}): {shlex.join(argv)}"
            + (f"\n{detail[-4000:]}" if detail else "")
        )
    return result


def run_remote(
    target: SshTarget,
    argv: Sequence[str],
    *,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return run_checked(
        [*target.ssh_argv(), shlex.join(argv)],
        timeout=timeout,
    )


def parse_object(raw: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClusterAcceptanceError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ClusterAcceptanceError(f"{label} returned a non-object JSON value")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wheel_metadata(path: Path) -> tuple[str, str]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", path.name) is None:
        raise ClusterAcceptanceError("candidate wheel has an unsafe filename")
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(names) != 1:
                raise ClusterAcceptanceError(
                    "candidate wheel must contain one METADATA file"
                )
            message = email.parser.Parser().parsestr(
                archive.read(names[0]).decode("utf-8")
            )
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ClusterAcceptanceError(f"cannot inspect candidate wheel: {exc}") from exc
    name, version = message.get("Name"), message.get("Version")
    if not isinstance(name, str) or re.sub(r"[-_.]+", "-", name).lower() != "gpubk":
        raise ClusterAcceptanceError("candidate wheel is not GPUBK")
    if not isinstance(version, str) or not version:
        raise ClusterAcceptanceError("candidate wheel has no version")
    return name, version


def candidate_wheel(work: Path, supplied: Path | None) -> Path:
    if supplied is not None:
        wheel = supplied.expanduser().resolve(strict=True)
        if wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl":
            raise ClusterAcceptanceError("--wheel must be one regular wheel file")
        wheel_metadata(wheel)
        return wheel
    output = work / "dist"
    builders = (ROOT / ".venv" / "bin" / "python", Path(sys.executable))
    builder = next(
        (
            path
            for path in builders
            if path.is_file()
            and subprocess.run(
                [str(path), "-c", "import build"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        ),
        None,
    )
    if builder is None:
        raise ClusterAcceptanceError(
            "Python build frontend is unavailable; run `.venv/bin/python -m pip install build` "
            "or pass --wheel"
        )
    run_checked(
        [str(builder), "-m", "build", "--wheel", "--outdir", str(output), str(ROOT)],
        timeout=180,
    )
    wheels = list(output.glob("gpubk-*.whl"))
    if len(wheels) != 1:
        raise ClusterAcceptanceError(
            "candidate build did not produce exactly one GPUBK wheel"
        )
    wheel_metadata(wheels[0])
    return wheels[0]


def prepare_local_client(work: Path, wheel: Path) -> Path:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    venv = work / "client"
    run_checked([sys.executable, "-m", "venv", str(venv)], environment=environment)
    python = venv / "bin" / "python"
    run_checked(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        environment=environment,
    )
    return venv / "bin" / "bk"


def setup_remote_node(
    target: SshTarget,
    name: str,
    run_id: str,
    wheel: Path,
    wheel_digest: str,
    remote_python: str,
) -> RemoteNode:
    setup = run_remote(target, [remote_python, "-c", REMOTE_SETUP, run_id])
    stage = setup.stdout.strip()
    if SAFE_REMOTE_PATH.fullmatch(stage) is None:
        raise ClusterAcceptanceError(f"{target.value} returned an unsafe stage path")
    try:
        destination = f"{target.value}:{stage}/{wheel.name}"
        run_checked([*target.scp_argv(), str(wheel), destination], timeout=180)
        installed = run_remote(
            target,
            [remote_python, "-c", REMOTE_INSTALL, stage, wheel.name, wheel_digest],
            timeout=180,
        )
        wrapper = str(
            parse_object(installed.stdout, f"{target.value} install").get("wrapper", "")
        )
        if SAFE_REMOTE_PATH.fullmatch(wrapper) is None or not wrapper.startswith(
            stage + "/"
        ):
            raise ClusterAcceptanceError(
                f"{target.value} returned an unsafe wrapper path"
            )
        context = parse_object(
            run_remote(target, [wrapper, "agent", "context", "--compact"]).stdout,
            f"{target.value} context",
        )
        node = context.get("node")
        software = context.get("software")
        actor = context.get("actor")
        node_id = node.get("id") if isinstance(node, dict) else None
        version = software.get("version") if isinstance(software, dict) else None
        if not isinstance(node_id, str) or SAFE_NODE_ID.fullmatch(node_id) is None:
            raise ClusterAcceptanceError(
                f"{target.value} returned an invalid stable node ID"
            )
        if not isinstance(version, str) or not version:
            raise ClusterAcceptanceError(
                f"{target.value} candidate did not report its version"
            )
        if (
            not isinstance(actor, dict)
            or isinstance(actor.get("uid"), bool)
            or not isinstance(actor.get("uid"), int)
        ):
            raise ClusterAcceptanceError(f"{target.value} returned an invalid actor")
        return RemoteNode(name, target, stage, wrapper, node_id, version, actor)
    except BaseException as exc:
        try:
            run_remote(
                target,
                [remote_python, "-c", REMOTE_CLEANUP, stage],
                timeout=60,
            )
        except (ClusterAcceptanceError, OSError) as cleanup_exc:
            raise ClusterAcceptanceError(
                f"{exc}; cleanup also failed for {target.value}: {cleanup_exc}"
            ) from exc
        raise


def cleanup_remote(node: RemoteNode, remote_python: str) -> str | None:
    try:
        run_remote(
            node.target,
            [remote_python, "-c", REMOTE_CLEANUP, node.stage],
            timeout=60,
        )
    except ClusterAcceptanceError as exc:
        return str(exc)
    return None


def write_catalog(path: Path, nodes: Sequence[RemoteNode]) -> None:
    document = {
        "schema_version": "gpubk.cluster.v1",
        "nodes": [
            {
                "name": node.name,
                "node_id": node.node_id,
                "transport": "ssh",
                "target": node.target.value,
                "executable": node.wrapper,
                "timeout_seconds": 15,
            }
            for node in nodes
        ],
    }
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


def run_client(bk: Path, catalog: Path, *arguments: str) -> dict[str, Any] | str:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "BK_CLUSTER_CONFIG": str(catalog),
            "NO_COLOR": "1",
        }
    )
    result = run_checked([str(bk), *arguments], environment=environment, timeout=90)
    return (
        parse_object(result.stdout, "cluster client")
        if "--json" in arguments
        else result.stdout
    )


def exercise_cluster(
    bk: Path, catalog: Path, nodes: Sequence[RemoteNode]
) -> dict[str, Any]:
    status = run_client(bk, catalog, "cluster", "status", "--json")
    if not isinstance(status, dict):
        raise ClusterAcceptanceError("cluster status was not structured")
    statuses = status.get("nodes")
    if not isinstance(statuses, list) or len(statuses) != len(nodes):
        raise ClusterAcceptanceError("cluster status omitted a configured node")
    if any(not item.get("available") for item in statuses if isinstance(item, dict)):
        raise ClusterAcceptanceError("at least one candidate node is unavailable")

    health = run_client(bk, catalog, "cluster", "check", "--json")
    if not isinstance(health, dict) or health.get("ready") is not True:
        raise ClusterAcceptanceError("cluster readiness check did not pass")

    recommendation = run_client(
        bk,
        catalog,
        "cluster",
        "recommend",
        "1",
        "10m",
        "--mode",
        "x",
        "--json",
    )
    operation_a = "cluster-acceptance-a-" + secrets.token_hex(8)
    operation_b = "cluster-acceptance-b-" + secrets.token_hex(8)
    booking_args = ("cluster", "x", "1", "10m")
    first = run_client(bk, catalog, *booking_args, "--op-id", operation_a, "--json")
    second = run_client(bk, catalog, *booking_args, "--op-id", operation_b, "--json")
    replay = run_client(bk, catalog, *booking_args, "--op-id", operation_a, "--json")
    if not all(
        isinstance(item, dict) for item in (recommendation, first, second, replay)
    ):
        raise ClusterAcceptanceError("cluster operation returned non-structured output")
    first_node = first["node"]["name"]
    second_node = second["node"]["name"]
    if first_node == second_node:
        raise ClusterAcceptanceError(
            "two exclusive bookings did not use independent nodes"
        )
    first_reservation = first["result"]["reservation"]
    replay_reservation = replay["result"]["reservation"]
    if (
        replay["node"]["name"] != first_node
        or replay_reservation["id"] != first_reservation["id"]
    ):
        raise ClusterAcceptanceError(
            "operation replay was not pinned to its original node"
        )

    reservations = [
        (first_node, first_reservation),
        (second_node, second["result"]["reservation"]),
    ]
    for node_name, reservation in reservations:
        run_client(
            bk,
            catalog,
            "cluster",
            "cancel",
            f"{node_name}/{reservation['short_id']}",
        )
    final_status = run_client(bk, catalog, "cluster", "status", "--json")
    active = sum(
        len(item.get("context", {}).get("reservations", []))
        for item in final_status.get("nodes", [])
        if isinstance(item, dict)
    )
    if active:
        raise ClusterAcceptanceError(
            "isolated reservations remained active after cleanup"
        )
    return {
        "status": status,
        "health": health,
        "recommendation": recommendation,
        "bookings": [first, second],
        "replay": replay,
        "final_status": final_status,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Install one local GPUBK wheel into private temporary directories on two or "
            "more SSH hosts, exercise cluster routing with simulated GPUs, and clean up."
        )
    )
    result.add_argument("targets", nargs="+", type=target_value, metavar="USER@HOST")
    result.add_argument(
        "--wheel", type=Path, help="test this wheel instead of building the checkout"
    )
    result.add_argument("--remote-python", default="python3")
    result.add_argument(
        "-o", "--ssh-option", type=ssh_option, action="append", default=[]
    )
    result.add_argument("--output-dir", type=Path, default=ROOT / "acceptance-reports")
    result.add_argument("--keep-remote", action="store_true")
    result.add_argument("--dry-run", action="store_true")
    return result


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ClusterAcceptanceError("acceptance report directory is unsafe")
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(output, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if len(args.targets) < 2:
        parser().error("at least two SSH targets are required")
    if len(set(args.targets)) != len(args.targets):
        parser().error("SSH targets must be unique")
    if any(character in args.remote_python for character in "\r\n\x00"):
        parser().error("--remote-python contains a control character")
    if shutil.which("ssh") is None or shutil.which("scp") is None:
        raise ClusterAcceptanceError("OpenSSH ssh and scp are required")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(
        6
    )
    if args.dry_run:
        print(f"targets: {', '.join(args.targets)}")
        print(
            "candidate: supplied wheel"
            if args.wheel
            else "candidate: wheel built from checkout"
        )
        print("remote state: private temporary directories only")
        print("GPU access: simulated file; production NVML and ledgers are not used")
        return 0

    settings = [SshTarget(value, tuple(args.ssh_option)) for value in args.targets]
    remote_nodes: list[RemoteNode] = []
    cleanup_warnings: list[str] = []
    result_payload: dict[str, Any] | None = None
    failure: Exception | None = None
    wheel_name: str | None = None
    wheel_digest: str | None = None
    candidate_version: str | None = None
    output = args.output_dir.expanduser().resolve() / f"cluster-{run_id}.json"
    with tempfile.TemporaryDirectory(prefix="gpubk-cluster-acceptance-") as raw_work:
        work = Path(raw_work)
        try:
            wheel = candidate_wheel(work, args.wheel)
            _wheel_name, candidate_version = wheel_metadata(wheel)
            wheel_name = wheel.name
            wheel_digest = sha256_file(wheel)
            print(f"Candidate wheel: {wheel.name}", flush=True)
            client = prepare_local_client(work, wheel)
            for index, target in enumerate(settings, start=1):
                print(
                    f"Preparing isolated node-{index} on {target.value}...", flush=True
                )
                remote_nodes.append(
                    setup_remote_node(
                        target,
                        f"node-{index}",
                        run_id,
                        wheel,
                        wheel_digest,
                        args.remote_python,
                    )
                )
            node_ids = [node.node_id for node in remote_nodes]
            if len(set(node_ids)) != len(node_ids):
                raise ClusterAcceptanceError(
                    "SSH targets resolve to the same stable GPUBK node; use distinct hosts"
                )
            versions = {node.version for node in remote_nodes}
            if versions != {candidate_version}:
                raise ClusterAcceptanceError(
                    "remote candidate version does not match the supplied wheel"
                )
            catalog = work / "cluster.json"
            write_catalog(catalog, remote_nodes)
            print(
                "Exercising status, health, recommendation, booking, replay, and cancel...",
                flush=True,
            )
            result_payload = exercise_cluster(client, catalog, remote_nodes)
        except Exception as exc:
            failure = exc
        finally:
            if not args.keep_remote:
                for node in remote_nodes:
                    warning = cleanup_remote(node, args.remote_python)
                    if warning:
                        cleanup_warnings.append(f"{node.target.value}: {warning}")
            elif remote_nodes:
                for node in remote_nodes:
                    print(f"Retained: {node.target.value}:{node.stage}")

        if failure is None and result_payload is None:
            failure = ClusterAcceptanceError("cluster exercise ended without a result")
        report = {
            "schema_version": REPORT_SCHEMA,
            "result": (
                "fail"
                if failure is not None
                else ("warning" if cleanup_warnings else "pass")
            ),
            "run_id": run_id,
            "wheel": wheel_name,
            "wheel_sha256": wheel_digest,
            "nodes": [
                {
                    "name": node.name,
                    "target": node.target.value,
                    "node_id": node.node_id,
                    "version": node.version,
                    "actor": node.actor,
                }
                for node in remote_nodes
            ],
            "checks": result_payload,
            "error": str(failure) if failure is not None else None,
            "cleanup_warnings": cleanup_warnings,
            "production_state_changed": False,
            "live_gpu_tested": False,
        }
        write_report(output, report)
    if failure is not None:
        print("Cluster acceptance: FAIL")
        print(f"Report: {output}")
        raise ClusterAcceptanceError(str(failure)) from failure
    print("Cluster acceptance: PASS")
    print(f"Nodes: {len(remote_nodes)} | candidate version: {remote_nodes[0].version}")
    print(f"Report: {output}")
    if cleanup_warnings:
        print("Warning: " + "; ".join(cleanup_warnings))
    print(
        "Still manual: approved live-GPU workload, second-user authorization, and reboot checks."
    )
    return 0 if not cleanup_warnings else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ClusterAcceptanceError, OSError, subprocess.SubprocessError) as exc:
        print(f"gpubk cluster acceptance: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
