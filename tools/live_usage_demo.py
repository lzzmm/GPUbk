#!/usr/bin/env python3
"""Create one short GPU booking and show the resulting usage statistics."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Mapping, Sequence


WORKLOAD_SECONDS = 65
WORKLOAD = r"""
import json
import os
import sys
import time

import torch

seconds = int(sys.argv[1])
torch.cuda.set_device(0)
x = torch.randn((4096, 4096), device="cuda")
y = torch.empty_like(x)
torch.cuda.synchronize()
print(json.dumps({
    "event": "start",
    "device": torch.cuda.get_device_name(0),
    "gpu": os.environ.get("BK_RESERVED_GPUS"),
    "seconds": seconds,
}), flush=True)
deadline = time.monotonic() + seconds
iterations = 0
while time.monotonic() < deadline:
    cycle = time.monotonic()
    burst_end = min(deadline, cycle + 0.10)
    while time.monotonic() < burst_end:
        torch.mm(x, x, out=y)
        iterations += 1
    torch.cuda.synchronize()
    time.sleep(max(0.0, min(deadline, cycle + 1.0) - time.monotonic()))
print(json.dumps({"event": "stop", "iterations": iterations}), flush=True)
""".strip()


class DemoError(RuntimeError):
    pass


def run_json(argv: Sequence[str], timeout: float = 30) -> dict:
    result = subprocess.run(
        list(argv),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DemoError(f"{' '.join(argv)} failed: {detail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DemoError(f"{' '.join(argv)} did not return JSON") from exc
    if not isinstance(value, dict):
        raise DemoError(f"{' '.join(argv)} returned invalid JSON")
    return value


def reservation_minutes(slot_minutes: int, workload_seconds: int) -> int:
    required_slots = math.ceil((workload_seconds + 30) / (slot_minutes * 60)) + 1
    duration = required_slots * slot_minutes
    if duration > 30:
        raise DemoError("the configured booking interval would make this demo exceed 30 minutes")
    return duration


def selected_idle_gpu(payload: Mapping[str, object]) -> int:
    allocation = payload.get("allocation")
    rows = allocation.get("selected") if isinstance(allocation, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise DemoError("booking did not select exactly one GPU")
    row = rows[0]
    if row.get("live_status") != "idle":
        raise DemoError(f"selected GPU is not idle: {row.get('live_reason') or 'unknown'}")
    gpu = row.get("gpu")
    if isinstance(gpu, bool) or not isinstance(gpu, int) or gpu < 0:
        raise DemoError("booking returned an invalid GPU index")
    return gpu


def stable_gpu_uuid(gpu: int) -> str:
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        raise DemoError("nvidia-smi is not available")
    result = subprocess.run(
        [nvidia_smi, "-i", str(gpu), "--query-gpu=uuid", "--format=csv,noheader"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    value = result.stdout.strip()
    if result.returncode or not value.startswith("GPU-"):
        raise DemoError(f"cannot resolve GPU {gpu} UUID: {result.stderr.strip()}")
    return value.splitlines()[0]


def cancel(bk: str, reservation_id: str) -> None:
    result = subprocess.run(
        [bk, "del", reservation_id],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    if result.returncode:
        print(
            f"WARNING: cancel {reservation_id[:8]} manually with `bk del {reservation_id[:8]}`",
            file=sys.stderr,
        )
    else:
        print(f"Released reservation {reservation_id[:8]}.")


def show(title: str, argv: Sequence[str]) -> None:
    print(f"\n== {title} ==")
    subprocess.run(list(argv), check=False)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a safe one-GPU workload and print GPUBK usage reports."
    )
    parser.add_argument("--bk", default=shutil.which("bk"), help="bk executable")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="CUDA-enabled Python with torch installed",
    )
    parser.add_argument("--seconds", type=int, default=WORKLOAD_SECONDS)
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args(argv)
    if not args.bk:
        parser.error("bk was not found; pass --bk /path/to/bk")
    if not 20 <= args.seconds <= 180:
        parser.error("--seconds must be between 20 and 180")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise DemoError("run as an ordinary user, not with sudo")
    bk = str(Path(args.bk).expanduser())
    python = str(Path(args.python).expanduser())

    print("Checking monitor and CUDA...", flush=True)
    run_json([bk, "doctor", "--require-monitor", "--json", "--strict"])
    config = run_json([bk, "config", "--json"])
    effective = config.get("effective")
    if not isinstance(effective, dict):
        raise DemoError("bk config has no effective settings")
    slot = int(effective["slot_minutes"])
    interval = float(effective["monitor_interval_seconds"])
    duration = reservation_minutes(slot, args.seconds)
    torch = run_json(
        [
            python,
            "-c",
            "import json,torch; print(json.dumps({'cuda':torch.cuda.is_available(),"
            "'name':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}))",
        ]
    )
    if torch.get("cuda") is not True:
        raise DemoError("this Python has no CUDA PyTorch; pass --python from a CUDA environment")

    print(
        f"Will book one immediately idle GPU for {duration}m, run a light {args.seconds}s "
        f"workload on {torch.get('name')}, then cancel the booking."
    )
    if not args.yes and input("Continue? [y/N]: ").strip().lower() not in {"y", "yes"}:
        return 0

    reservation_id: str | None = None
    try:
        result = run_json(
            [
                bk,
                "exclusive",
                "1",
                f"{duration}m",
                "--start",
                "now",
                "--op-id",
                f"usage-demo-{os.getuid()}-{uuid.uuid4().hex}",
                "--json",
            ],
            timeout=45,
        )
        reservation = result.get("reservation")
        if result.get("status") != "created" or not isinstance(reservation, dict):
            raise DemoError(f"booking was not created: {result.get('status')}")
        reservation_id = str(reservation["id"])
        gpu = selected_idle_gpu(result)
        gpu_uuid = stable_gpu_uuid(gpu)
        print(f"Booked GPU {gpu} as {reservation_id[:8]}; starting workload.")

        environment = dict(os.environ)
        environment.update(
            {
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                "CUDA_VISIBLE_DEVICES": gpu_uuid,
                "BK_RESERVATION_ID": reservation_id,
                "BK_RESERVED_GPUS": str(gpu),
            }
        )
        workload = subprocess.run(
            [python, "-c", WORKLOAD, str(args.seconds)],
            check=False,
            env=environment,
        )
        if workload.returncode:
            raise DemoError(f"workload exited with status {workload.returncode}")
        time.sleep(max(4.0, interval * 2.0))
    finally:
        if reservation_id:
            cancel(bk, reservation_id)

    show("Current user", [bk, "usage", "me", "--since", "15m"])
    show(
        "One-minute samples",
        [bk, "usage", "samples", "--user", "me", "--since", "15m", "--resolution", "1m"],
    )
    show("Process events", [bk, "usage", "events", "--user", "me", "--since", "15m"])
    print("\nDone. The demo reservation was removed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except (DemoError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"usage demo: {exc}", file=sys.stderr)
        raise SystemExit(2)
