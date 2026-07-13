from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .advisor import GpuAdvice
from .config import Config
from .models import Actor
from .scheduler import list_active
from .sharing import reservation_share_units
from .storage import LedgerStore
from .timeparse import to_iso


ALLOCATOR_SCHEMA_VERSION = "bk.allocator.v1"
MAX_ALLOCATOR_OUTPUT_BYTES = 64 * 1024
MAX_ALLOCATOR_STDERR_BYTES = 8 * 1024
ALLOCATOR_IO_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class AllocatorDecision:
    order: List[int]
    scores: Dict[int, float]
    source: str
    reason: str = ""
    warning: str = ""


def apply_external_allocator(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    advice: GpuAdvice,
    *,
    count: int,
    duration_seconds: int,
    start_at: datetime,
    mode: str,
    expected_memory_mb: Optional[int],
    share_units: int = 1,
    excluded_gpus: Optional[Sequence[int]] = None,
) -> AllocatorDecision:
    if not config.allocator_command:
        return AllocatorDecision(list(advice.order), dict(advice.scores), "builtin")
    payload = _allocator_payload(
        config,
        store,
        actor,
        advice,
        count=count,
        duration_seconds=duration_seconds,
        start_at=start_at,
        mode=mode,
        expected_memory_mb=expected_memory_mb,
        share_units=share_units,
        excluded_gpus=excluded_gpus,
    )
    try:
        returncode, stdout, stderr = _run_allocator_process(
            list(config.allocator_command),
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            config.allocator_timeout_seconds,
        )
        if returncode != 0:
            detail = stderr.strip().splitlines()[-1][:200] if stderr.strip() else "no stderr"
            raise ValueError(f"allocator exited {returncode}: {detail}")
        response = json.loads(stdout)
        order, reason = _validate_allocator_response(response, config.gpu_count)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        return AllocatorDecision(
            list(advice.order),
            dict(advice.scores),
            "builtin-fallback",
            warning=f"external allocator ignored: {exc}",
        )

    adjusted_scores = dict(advice.scores)
    for rank, gpu in enumerate(order):
        adjusted_scores[gpu] = round(adjusted_scores[gpu] + rank * config.allocator_weight, 3)
    return AllocatorDecision(order, adjusted_scores, "external", reason=reason)


def _run_allocator_process(argv: List[str], payload: str, timeout_seconds: float) -> tuple[int, str, str]:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        close_fds=True,
    )
    streams = (process.stdin, process.stdout, process.stderr)
    if any(stream is None for stream in streams):
        _kill_allocator_process_group(process)
        raise OSError("allocator subprocess pipes are unavailable")
    selector = selectors.DefaultSelector()
    payload_bytes = payload.encode("utf-8")
    payload_offset = 0
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            for key, _events in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                if key.data == "stdin":
                    try:
                        written = os.write(
                            stream.fileno(),
                            payload_bytes[payload_offset : payload_offset + ALLOCATOR_IO_CHUNK_BYTES],
                        )
                    except BrokenPipeError:
                        _close_selector_stream(selector, stream)
                        continue
                    payload_offset += written
                    if payload_offset >= len(payload_bytes):
                        _close_selector_stream(selector, stream)
                    continue

                try:
                    chunk = os.read(stream.fileno(), ALLOCATOR_IO_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    _close_selector_stream(selector, stream)
                elif key.data == "stdout":
                    keep = MAX_ALLOCATOR_OUTPUT_BYTES + 1 - len(stdout)
                    if keep > 0:
                        stdout.extend(chunk[:keep])
                    if len(stdout) > MAX_ALLOCATOR_OUTPUT_BYTES:
                        raise ValueError("allocator output exceeded 64 KiB")
                else:
                    _append_bounded_tail(stderr, chunk, MAX_ALLOCATOR_STDERR_BYTES)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        returncode = process.wait(timeout=remaining)
        _kill_allocator_process_group(process)
        return returncode, stdout.decode("utf-8"), stderr.decode("utf-8", errors="replace")
    except BaseException:
        _kill_allocator_process_group(process)
        raise
    finally:
        for stream in streams:
            _close_selector_stream(selector, stream)
        selector.close()


def _append_bounded_tail(buffer: bytearray, chunk: bytes, limit: int) -> None:
    if len(chunk) >= limit:
        buffer[:] = chunk[-limit:]
        return
    overflow = len(buffer) + len(chunk) - limit
    if overflow > 0:
        del buffer[:overflow]
    buffer.extend(chunk)


def _close_selector_stream(selector: selectors.BaseSelector, stream) -> None:
    try:
        selector.unregister(stream)
    except (KeyError, ValueError):
        pass
    try:
        stream.close()
    except OSError:
        pass


def _kill_allocator_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _allocator_payload(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    advice: GpuAdvice,
    *,
    count: int,
    duration_seconds: int,
    start_at: datetime,
    mode: str,
    expected_memory_mb: Optional[int],
    share_units: int,
    excluded_gpus: Optional[Sequence[int]] = None,
) -> dict:
    reservations = list_active(store.load(), start_at)
    return {
        "schema_version": ALLOCATOR_SCHEMA_VERSION,
        "kind": "allocation_request",
        "generated_at": to_iso(advice.generated_at),
        "actor": {"uid": actor.uid},
        "request": {
            "count": count,
            "duration_seconds": duration_seconds,
            "start_at": to_iso(start_at),
            "mode": mode,
            "expected_memory_mb_per_gpu": expected_memory_mb,
            "share_units_per_gpu": share_units if mode == "shared" else None,
            "excluded_gpus": list(excluded_gpus or ()),
        },
        "policy": {
            "gpu_count": config.gpu_count,
            "enabled_gpus": list(config.enabled_gpus),
            "disabled_gpus": list(config.disabled_gpus),
            "gpu_priority": {
                str(gpu): priority for gpu, priority in config.gpu_priority
            },
            "granularity_minutes": config.slot_minutes,
            "max_shared_reservations_per_gpu": config.max_shared_users,
            "shared_capacity_units_per_gpu": config.max_shared_users,
            "shared_memory_reserve_mb": config.shared_memory_reserve_mb,
            "local_score_is_authoritative_for_safety": True,
            "allocator_weight": config.allocator_weight,
        },
        "builtin_advice": advice.as_dict(),
        "reservations": [
            {
                "uid": item.get("uid"),
                "gpus": list(item.get("gpus", [])),
                "mode": item.get("mode"),
                "start_at": item.get("start_at"),
                "end_at": item.get("end_at"),
                "expected_memory_mb_per_gpu": item.get("expected_memory_mb"),
                "share_units_per_gpu": (
                    reservation_share_units(item, config.max_shared_users)
                    if item.get("mode") == "shared"
                    else None
                ),
            }
            for item in reservations
        ],
        "response_contract": {
            "schema_version": ALLOCATOR_SCHEMA_VERSION,
            "gpu_order": "permutation of every configured GPU index",
            "eligibility": "disabled and request-excluded GPUs are never selectable",
            "reason": "optional privacy-safe text, max 500 chars",
        },
    }


def _validate_allocator_response(response: object, gpu_count: int) -> tuple[List[int], str]:
    if not isinstance(response, dict):
        raise ValueError("allocator response must be a JSON object")
    if response.get("schema_version") != ALLOCATOR_SCHEMA_VERSION:
        raise ValueError("allocator schema version mismatch")
    raw_order = response.get("gpu_order")
    if not isinstance(raw_order, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in raw_order):
        raise ValueError("gpu_order must be an integer array")
    if raw_order != list(dict.fromkeys(raw_order)) or sorted(raw_order) != list(range(gpu_count)):
        raise ValueError("gpu_order must be a permutation of configured GPUs")
    reason = str(response.get("reason", "")).strip()
    if len(reason) > 500 or any(ord(char) < 32 and char not in "\t" for char in reason):
        raise ValueError("allocator reason is invalid")
    return list(raw_order), reason
