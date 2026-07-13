from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path
from time import perf_counter

from bk.config import Config
from bk.granularity import ceil_to_slot
from bk.models import MODE_EXCLUSIVE
from bk.scheduler import find_earliest_slot
from bk.timeparse import to_iso, utc_now


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark a fully booked BK queue search")
    parser.add_argument("--gpus", type=int, default=8)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--slot-minutes", type=int, default=30)
    args = parser.parse_args()
    config = Config(
        data_dir=Path("."),
        gpu_count=args.gpus,
        queue_search_hours=args.days * 24,
        slot_minutes=args.slot_minutes,
    )
    start = ceil_to_slot(utc_now(), config.slot_minutes)
    slots = args.days * 24 * 60 // args.slot_minutes
    reservations = []
    for slot in range(slots):
        left = start + timedelta(minutes=args.slot_minutes * slot)
        right = left + timedelta(minutes=args.slot_minutes)
        for gpu in range(args.gpus):
            reservations.append(
                {
                    "id": f"{slot}-{gpu}",
                    "uid": 1000 + gpu,
                    "username": f"user{gpu}",
                    "gpus": [gpu],
                    "mode": MODE_EXCLUSIVE,
                    "start_at": to_iso(left),
                    "end_at": to_iso(right),
                    "status": "active",
                }
            )
    ledger = {"version": 1, "reservations": reservations}
    before = perf_counter()
    result = find_earliest_slot(
        ledger,
        config,
        args.gpus,
        start,
        timedelta(minutes=args.slot_minutes),
        MODE_EXCLUSIVE,
        1000,
        allow_queue=True,
    )
    elapsed = perf_counter() - before
    print(
        json.dumps(
            {
                "schema_version": "bk.benchmark.v1",
                "reservations": len(reservations),
                "gpus": args.gpus,
                "days": args.days,
                "elapsed_seconds": round(elapsed, 6),
                "result_start": to_iso(result[0]) if result else None,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
