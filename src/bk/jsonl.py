from __future__ import annotations

import errno
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Tuple

from .fileio import (
    ensure_directory,
    fsync_directory,
    open_existing_regular,
    open_or_create_regular,
)


WRITE_CHUNK_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class JsonlFormatError(OSError):
    pass


@dataclass(frozen=True)
class JsonlAppendResult:
    count: int
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class JsonlTailResult:
    records: Tuple[dict, ...]
    invalid_records: int = 0
    oversized_records: int = 0
    final_newline_missing: bool = False
    scan_truncated: bool = False


def append_json_objects(
    path: Path,
    records: Sequence[dict],
    *,
    file_mode: int,
    dir_mode: int,
    max_line_bytes: int,
    max_file_bytes: Optional[int],
    record_name: str,
    compact: bool = True,
    expected_gid: Optional[int] = None,
) -> JsonlAppendResult:
    if not records:
        return JsonlAppendResult(0)
    if max_line_bytes < 2 or (max_file_bytes is not None and max_file_bytes < 1):
        raise ValueError("invalid JSONL safety limit")

    serialized = []
    total_bytes = 0
    for record in records:
        line = encode_json_object_line(
            record,
            max_line_bytes=max_line_bytes,
            record_name=record_name,
            compact=compact,
        )
        serialized.append(line)
        total_bytes += len(line)

    ensure_directory(
        path.parent,
        dir_mode,
        require_mode=True,
        expected_gid=expected_gid,
    )
    fd = open_or_create_regular(
        path,
        os.O_RDWR | os.O_APPEND,
        file_mode,
        expected_gid=expected_gid,
    )
    warnings = []
    try:
        original_size = _repair_tail(fd, path, max_line_bytes, record_name, warnings)
        if max_file_bytes is not None and original_size + total_bytes > max_file_bytes:
            raise JsonlFormatError(f"{record_name} file exceeds its safety limit: {path}")
        try:
            _write_batch(fd, serialized)
            os.fsync(fd)
            fsync_directory(path.parent)
        except BaseException as append_error:
            try:
                os.ftruncate(fd, original_size)
                os.fsync(fd)
            except OSError as rollback_error:
                raise OSError(
                    errno.EIO,
                    f"{record_name} append failed and rollback failed for {path}: "
                    f"{append_error}; {rollback_error}",
                ) from append_error
            raise
    finally:
        os.close(fd)
    return JsonlAppendResult(len(records), tuple(warnings))


def encode_json_object_line(
    record: dict,
    *,
    max_line_bytes: int,
    record_name: str,
    compact: bool = True,
) -> bytes:
    if not isinstance(record, dict):
        raise ValueError(f"{record_name} record must be a JSON object")
    try:
        if compact:
            text = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        else:
            text = json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{record_name} record is not valid JSON: {exc}") from exc
    line = (text + "\n").encode("utf-8")
    if len(line) > max_line_bytes:
        raise ValueError(f"{record_name} record exceeds the line safety limit")
    return line


def read_json_objects_tail(
    path: Path,
    *,
    limit: int,
    max_line_bytes: int,
    predicate: Optional[Callable[[dict], bool]] = None,
    transform: Optional[Callable[[dict], Optional[dict]]] = None,
    max_scan_bytes: Optional[int] = None,
) -> JsonlTailResult:
    """Return recent matching JSON objects in file order with bounded memory."""
    if (
        limit < 1
        or max_line_bytes < 2
        or (max_scan_bytes is not None and max_scan_bytes < 1)
    ):
        raise ValueError("invalid JSONL read limit")

    fd = open_existing_regular(path)
    records = []
    invalid_records = 0
    oversized_records = 0
    try:
        size = os.fstat(fd).st_size
        lower_bound = max(0, size - max_scan_bytes) if max_scan_bytes is not None else 0
        final_newline_missing = bool(size and os.pread(fd, 1, size - 1) != b"\n")
        for raw_line in _iter_lines_reverse(fd, size, lower_bound, max_line_bytes):
            if raw_line is None:
                oversized_records += 1
                continue
            if not raw_line:
                invalid_records += 1
                continue
            try:
                value = json.loads(raw_line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                invalid_records += 1
                continue
            if not isinstance(value, dict):
                invalid_records += 1
                continue
            if predicate is not None and not predicate(value):
                continue
            if transform is not None:
                value = transform(value)
                if value is None:
                    continue
                if not isinstance(value, dict):
                    raise TypeError("JSONL tail transform must return an object or None")
            records.append(value)
            if len(records) >= limit:
                break
    finally:
        os.close(fd)

    records.reverse()
    return JsonlTailResult(
        tuple(records),
        invalid_records=invalid_records,
        oversized_records=oversized_records,
        final_newline_missing=final_newline_missing,
        scan_truncated=bool(lower_bound and len(records) < limit),
    )


def _repair_tail(
    fd: int,
    path: Path,
    max_line_bytes: int,
    record_name: str,
    warnings: list[str],
) -> int:
    size = os.fstat(fd).st_size
    if size == 0 or os.pread(fd, 1, size - 1) == b"\n":
        return size

    read_size = min(size, max_line_bytes + 1)
    tail_start = size - read_size
    tail = os.pread(fd, read_size, tail_start)
    separator = tail.rfind(b"\n")
    if separator >= 0:
        record_start = tail_start + separator + 1
        fragment = tail[separator + 1 :]
    elif tail_start == 0:
        record_start = 0
        fragment = tail
    else:
        raise JsonlFormatError(f"unterminated {record_name} record exceeds the line limit: {path}")

    if len(fragment) + 1 > max_line_bytes:
        raise JsonlFormatError(f"unterminated {record_name} record exceeds the line limit: {path}")
    try:
        value = json.loads(fragment)
    except (ValueError, UnicodeDecodeError, RecursionError):
        value = None
    if isinstance(value, dict):
        _write_all(fd, b"\n")
        os.fsync(fd)
        warnings.append(f"restored a missing final newline in {path}")
        return size + 1

    os.ftruncate(fd, record_start)
    os.fsync(fd)
    warnings.append(f"discarded an incomplete trailing {record_name} record in {path}")
    return record_start


def _write_batch(fd: int, lines: Sequence[bytes]) -> None:
    chunk = bytearray()
    for line in lines:
        if chunk and len(chunk) + len(line) > WRITE_CHUNK_BYTES:
            _write_all(fd, chunk)
            chunk.clear()
        chunk.extend(line)
    if chunk:
        _write_all(fd, chunk)


def _write_all(fd: int, payload: bytes | bytearray) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "short write while appending JSONL data")
        remaining = remaining[written:]


def _iter_lines_reverse(
    fd: int,
    size: int,
    lower_bound: int,
    max_line_bytes: int,
) -> Iterator[Optional[bytes]]:
    position = size
    pending = b""
    dropping_oversized = False
    at_file_end = True

    while position > lower_bound:
        read_size = min(position - lower_bound, READ_CHUNK_BYTES)
        position -= read_size
        chunk = os.pread(fd, read_size, position)
        if len(chunk) != read_size:
            raise OSError(errno.EIO, "short read while scanning JSONL data")

        if dropping_oversized:
            separator = chunk.rfind(b"\n")
            if separator < 0:
                continue
            yield None
            at_file_end = False
            dropping_oversized = False
            pending = b""
            chunk = chunk[:separator]

        pending = chunk + pending
        while True:
            separator = pending.rfind(b"\n")
            if separator < 0:
                break
            line = pending[separator + 1 :]
            pending = pending[:separator]
            if at_file_end and not line:
                at_file_end = False
                continue
            at_file_end = False
            if len(line) + 1 > max_line_bytes:
                yield None
            else:
                yield line

        if len(pending) + 1 > max_line_bytes:
            dropping_oversized = True
            pending = b""
            at_file_end = False

    if lower_bound:
        return
    if dropping_oversized:
        yield None
    elif pending:
        if len(pending) + 1 > max_line_bytes:
            yield None
        else:
            yield pending
