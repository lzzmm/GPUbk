"""Synthetic sparse telemetry write, compaction, and query benchmark."""

from __future__ import annotations

import argparse
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.config import Config
from bk.usage_api import UsageQueryService
from bk.usage_store import UsageAuditStore, UsageRetentionPolicy
from bk.workload import describe_workload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--gpus", type=int, default=8)
    parser.add_argument("--users-per-gpu", type=int, default=2)
    args = parser.parse_args()
    if args.days < 1 or args.gpus < 1 or args.users_per_gpu < 1:
        raise SystemExit("all benchmark dimensions must be positive")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        config = Config(data_dir=data_dir, gpu_count=args.gpus)
        store = UsageAuditStore(data_dir)
        workload_ids = {
            uid: store.register_workload(uid, describe_workload("torchrun train.py"))
            for uid in range(1000, 1000 + args.gpus * args.users_per_gpu)
        }
        end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=args.days)
        records = list(_records(start, args.days, args.gpus, args.users_per_gpu, workload_ids))

        before = time.perf_counter()
        store.append_rollups(records)
        write_seconds = time.perf_counter() - before
        before = time.perf_counter()
        store.maintain(UsageRetentionPolicy(minute_days=0), now=end)
        compact_seconds = time.perf_counter() - before
        before = time.perf_counter()
        payload = UsageQueryService(config, store).users(start=start, end=end, resolution="5m")
        query_seconds = time.perf_counter() - before
        total_bytes = sum(path.stat().st_size for path in (data_dir / "usage").rglob("*") if path.is_file())

        print(
            f"records={len(records)} bytes={total_bytes} "
            f"write={write_seconds:.3f}s compact={compact_seconds:.3f}s "
            f"query={query_seconds:.3f}s users={len(payload['users'])}"
        )


def _records(start, days, gpu_count, users_per_gpu, workload_ids):
    minutes = days * 24 * 60
    for minute in range(minutes):
        window_start = start + timedelta(minutes=minute)
        window_end = window_start + timedelta(minutes=1)
        for gpu in range(gpu_count):
            for lane in range(users_per_gpu):
                uid = 1000 + gpu * users_per_gpu + lane
                yield {
                    "window_start": window_start.isoformat().replace("+00:00", "Z"),
                    "window_end": window_end.isoformat().replace("+00:00", "Z"),
                    "partial": False,
                    "gpu": gpu,
                    "uid": uid,
                    "username": f"user{uid}",
                    "status": "ok",
                    "reservation_ids": [f"r-{uid}"],
                    "sample_count": 30,
                    "observed_seconds": 60,
                    "active_sample_count": 30,
                    "active_observed_seconds": 60,
                    "avg_process_count": 1,
                    "max_process_count": 1,
                    "sm_sample_count": 30,
                    "avg_sm_percent": 50,
                    "max_sm_percent": 80,
                    "avg_gpu_memory_mb": 8192,
                    "max_gpu_memory_mb": 12288,
                    "device_util_sample_count": 30,
                    "avg_device_util_percent": 70,
                    "max_device_util_percent": 95,
                    "workload_ids": [workload_ids[uid]],
                    "workload_observed_seconds": {str(workload_ids[uid]): 60},
                }


if __name__ == "__main__":
    main()
