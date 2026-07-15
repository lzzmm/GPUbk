from __future__ import annotations

import gzip
import hashlib
import json
import fcntl
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .config import Config
from .fileio import fsync_directory, open_existing_regular
from .models import BookingError
from .node_identity import stable_node_identity
from .timeparse import parse_duration_seconds, parse_iso, to_iso, utc_now
from .usage_api import (
    MAX_QUERY_RECORDS,
    UsageQueryService,
    summarize_public_rollups,
)
from .usage_schema import USAGE_API_VERSION, parse_resolution, resolution_label


HISTORY_SCHEMA_VERSION = "gpubk.cluster-history.v1"
HISTORY_DIRECTORY_MODE = 0o755
GENERATION_DIRECTORY_MODE = 0o555
HISTORY_FILE_MODE = 0o444
MAX_HISTORY_NODES = 128
MAX_HISTORY_GENERATIONS = 4096
MAX_HISTORY_FILES = 8192
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
_NODE_ID = re.compile(r"^[0-9a-f]{20}$")
_GENERATION_PATTERN = r"\d{8}T\d{6}Z-\d{8}T\d{6}Z-(?:5m|10m|1h|1d)"
_GENERATION = re.compile(rf"^{_GENERATION_PATTERN}$")
_TEMP_GENERATION = re.compile(
    rf"^\.tmp-{_GENERATION_PATTERN}-[0-9a-f]{{32}}$"
)
_PAYLOAD_NAME = re.compile(r"^day-\d{5}-(?:samples|users)\.json\.gz$")
_PRIVATE_KEYS = frozenset(
    {
        "argv",
        "command",
        "cwd",
        "environment",
        "job_spec",
        "raw_command",
        "secret",
        "stderr",
        "stdout",
    }
)


@dataclass(frozen=True)
class ArchivedUsage:
    node_id: str
    generation: str
    start_at: datetime
    end_at: datetime
    payload: dict


@dataclass(frozen=True)
class HistoryGeneration:
    node_id: str
    name: str
    path: Path
    start_at: datetime
    end_at: datetime
    resolution: str
    files: tuple[dict, ...]
    manifest: dict


def resolve_history_window(
    root: Path,
    node_id: str,
    *,
    since: str = "30d",
    start: Optional[str] = None,
    until: Optional[str] = None,
    now: Optional[datetime] = None,
    incremental: bool = False,
) -> tuple[datetime, datetime]:
    current = (now or utc_now()).astimezone(timezone.utc)
    default_end = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = _parse_history_time(until, default_end) if until else default_end
    if start:
        begin = _parse_history_time(start, end)
    else:
        begin = end - timedelta(seconds=parse_duration_seconds(since))
        if incremental and root.is_absolute() and os.path.lexists(root):
            latest = latest_history_end(root, node_id)
            if latest is not None:
                begin = latest
    _require_day_boundary(begin, "history start")
    _require_day_boundary(end, "history end")
    if end <= begin:
        if incremental and begin >= end:
            return end, end
        raise BookingError("no complete UTC-day history remains to export")
    return begin, end


def export_cluster_history(
    root: Path,
    config: Config,
    *,
    start: datetime,
    end: datetime,
    resolution: str = "10m",
    api: Optional[UsageQueryService] = None,
) -> dict:
    root = _validate_history_root(root, writable=True)
    begin = start.astimezone(timezone.utc)
    finish = end.astimezone(timezone.utc)
    _require_day_boundary(begin, "history start")
    _require_day_boundary(finish, "history end")
    if finish <= begin:
        raise BookingError("history end must be after start")
    seconds = parse_resolution(resolution)
    label = resolution_label(seconds)
    if label not in {"5m", "10m", "1h", "1d"}:
        raise BookingError("cluster history resolution must be 5m, 10m, 1h, or 1d")
    days = int((finish - begin).total_seconds() // 86400)
    if days < 1 or days * 2 > MAX_HISTORY_FILES:
        raise BookingError(
            f"one history generation must contain 1-{MAX_HISTORY_FILES // 2} UTC days"
        )

    node = stable_node_identity()
    node_id = str(node.get("id", ""))
    if not _NODE_ID.fullmatch(node_id):
        raise BookingError("local stable node identity is invalid")
    owner_uid = (
        config.monitor_uid
        if os.geteuid() == 0 and config.monitor_uid is not None
        else os.geteuid()
    )
    namespace = _prepare_namespace(root, node_id, owner_uid=owner_uid)
    generation_name = _generation_name(begin, finish, label)
    destination = namespace / generation_name
    with _namespace_lock(
        namespace,
        config.lock_timeout_seconds,
        owner_uid=owner_uid,
    ):
        _cleanup_stale_temporary_generations(namespace, owner_uid=owner_uid)
        existing = _generation_manifests(namespace, node_id)
        for generation in existing:
            if generation.name == generation_name:
                verified = verify_cluster_history(destination, expected_node_ids={node_id})
                return {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "status": "exists",
                    "root": str(root),
                    "node_id": node_id,
                    "generation": generation_name,
                    "start_at": to_iso(begin),
                    "end_at": to_iso(finish),
                    "resolution": label,
                    "files": verified["files"],
                    "bytes": verified["bytes"],
                }
            if begin < generation.end_at and generation.start_at < finish:
                raise BookingError(
                    "history generation overlaps existing immutable data: "
                    f"{generation.name} ({to_iso(generation.start_at)} -> "
                    f"{to_iso(generation.end_at)})"
                )

        query = api or UsageQueryService(config)
        temporary = namespace / f".tmp-{generation_name}-{uuid.uuid4().hex}"
        os.mkdir(temporary, 0o700)
        files: list[dict] = []
        try:
            index = 0
            for batch_start, batch_end, batch in _query_sample_batches(
                query,
                begin,
                finish,
                label,
                node_id,
            ):
                cursor = batch_start
                records = list(batch.get("records", []))
                record_index = 0
                while cursor < batch_end:
                    chunk_end = min(cursor + timedelta(days=1), batch_end)
                    daily_records = []
                    while record_index < len(records):
                        record = records[record_index]
                        try:
                            record_start = parse_iso(str(record["window_start"]))
                            record_end = parse_iso(str(record["window_end"]))
                        except (KeyError, TypeError, ValueError) as exc:
                            raise BookingError(
                                "public usage sample has an invalid time window"
                            ) from exc
                        if record_start >= chunk_end:
                            break
                        if record_start < cursor or record_end > chunk_end:
                            raise BookingError(
                                "public usage sample crosses an archive day boundary"
                            )
                        daily_records.append(record)
                        record_index += 1

                    samples = _slice_samples(batch, cursor, chunk_end, daily_records)
                    users = _users_from_samples(samples)
                    for payload, kind, collection in (
                        (users, "usage-users", "users"),
                        (samples, "usage-samples", "records"),
                    ):
                        _validate_public_payload(
                            payload,
                            node_id=node_id,
                            kind=kind,
                            start=cursor,
                            end=chunk_end,
                        )
                        suffix = "users" if kind == "usage-users" else "samples"
                        name = f"day-{index:05d}-{suffix}.json.gz"
                        encoded = _encode_payload(payload)
                        _write_immutable_file(temporary / name, encoded)
                        files.append(
                            {
                                "name": name,
                                "kind": kind,
                                "schema_version": USAGE_API_VERSION,
                                "start_at": to_iso(cursor),
                                "end_at": to_iso(chunk_end),
                                "records": len(payload.get(collection, [])),
                                "bytes": len(encoded),
                                "sha256": hashlib.sha256(encoded).hexdigest(),
                            }
                        )
                    cursor = chunk_end
                    index += 1
                if record_index != len(records):
                    raise BookingError("public usage sample falls outside its query batch")

            manifest = {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "kind": "cluster-history-generation",
                "generation": generation_name,
                "created_at": to_iso(utc_now()),
                "node": node,
                "range": {
                    "start_at": to_iso(begin),
                    "end_at": to_iso(finish),
                    "resolution": label,
                    "resolution_seconds": seconds,
                },
                "payload_schema": USAGE_API_VERSION,
                "files": files,
            }
            manifest_bytes = _json_bytes(manifest)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise BookingError("cluster history manifest exceeds 2 MiB")
            _write_immutable_file(temporary / "manifest.json", manifest_bytes)
            fsync_directory(temporary)
            os.chmod(temporary, GENERATION_DIRECTORY_MODE)
            fsync_directory(temporary)
            os.rename(temporary, destination)
            fsync_directory(namespace)
        except Exception:
            _remove_temporary_generation(temporary)
            raise

    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "status": "exported",
        "root": str(root),
        "node_id": node_id,
        "generation": generation_name,
        "start_at": to_iso(begin),
        "end_at": to_iso(finish),
        "resolution": label,
        "files": len(files),
        "bytes": sum(int(item["bytes"]) for item in files),
    }


def verify_cluster_history(
    path: Path,
    *,
    expected_node_ids: Optional[set[str]] = None,
) -> dict:
    root = _validate_history_root(path, writable=False)
    generations = _discover_generations(root, expected_node_ids=expected_node_ids)
    total_files = 0
    total_bytes = 0
    nodes: set[str] = set()
    for generation in generations:
        nodes.add(generation.node_id)
        for entry in generation.files:
            payload = _read_payload(generation.path, entry)
            _validate_public_payload(
                payload,
                node_id=generation.node_id,
                kind=str(entry["kind"]),
                start=parse_iso(str(entry["start_at"])),
                end=parse_iso(str(entry["end_at"])),
            )
            total_files += 1
            total_bytes += int(entry["bytes"])
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "status": "verified",
        "root": str(root),
        "nodes": sorted(nodes),
        "generations": len(generations),
        "files": total_files,
        "bytes": total_bytes,
    }


def load_archived_user_usage(
    root: Path,
    *,
    start: datetime,
    end: datetime,
    node_ids: Sequence[str],
) -> tuple[list[ArchivedUsage], dict]:
    root = _validate_history_root(root, writable=False)
    allowed = set(node_ids)
    generations = _discover_generations(root, expected_node_ids=allowed)
    _validate_non_overlapping(generations)
    begin = start.astimezone(timezone.utc)
    finish = end.astimezone(timezone.utc)
    payloads: list[ArchivedUsage] = []
    generation_names: set[tuple[str, str]] = set()
    for generation in generations:
        for entry in generation.files:
            if entry["kind"] != "usage-users":
                continue
            entry_start = parse_iso(str(entry["start_at"]))
            entry_end = parse_iso(str(entry["end_at"]))
            if entry_start < begin or entry_end > finish:
                continue
            payload = _read_payload(generation.path, entry)
            _validate_public_payload(
                payload,
                node_id=generation.node_id,
                kind="usage-users",
                start=entry_start,
                end=entry_end,
            )
            payloads.append(
                ArchivedUsage(
                    generation.node_id,
                    generation.name,
                    entry_start,
                    entry_end,
                    payload,
                )
            )
            generation_names.add((generation.node_id, generation.name))
    payloads.sort(key=lambda item: (item.start_at, item.node_id, item.generation))
    return payloads, {
        "root": str(root),
        "generations": len(generation_names),
        "chunks": len(payloads),
        "start_at": to_iso(begin),
        "end_at": to_iso(finish),
    }


def latest_history_end(root: Path, node_id: str) -> Optional[datetime]:
    root = _validate_history_root(root, writable=False)
    namespace = root / node_id
    if not os.path.lexists(namespace):
        return None
    generations = _generation_manifests(namespace, node_id)
    return max((item.end_at for item in generations), default=None)


def _discover_generations(
    root: Path,
    *,
    expected_node_ids: Optional[set[str]],
) -> list[HistoryGeneration]:
    if root.name == "manifest.json" or _GENERATION.fullmatch(root.name):
        generation_path = root.parent if root.name == "manifest.json" else root
        node_id = generation_path.parent.name
        if expected_node_ids is not None and node_id not in expected_node_ids:
            raise BookingError(f"history generation belongs to unexpected node {node_id}")
        return [_read_manifest(generation_path, node_id)]

    entries = _directory_entries(root)
    node_paths = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if not _NODE_ID.fullmatch(entry.name):
            raise BookingError(f"unexpected entry in cluster history root: {entry.name}")
        if expected_node_ids is not None and entry.name not in expected_node_ids:
            continue
        node_paths.append(Path(entry.path))
    if len(node_paths) > MAX_HISTORY_NODES:
        raise BookingError(f"cluster history contains more than {MAX_HISTORY_NODES} nodes")
    generations = []
    for namespace in sorted(node_paths):
        _validate_namespace(namespace)
        generations.extend(_generation_manifests(namespace, namespace.name))
    if len(generations) > MAX_HISTORY_GENERATIONS:
        raise BookingError(
            f"cluster history contains more than {MAX_HISTORY_GENERATIONS} generations"
        )
    return generations


def _generation_manifests(namespace: Path, node_id: str) -> list[HistoryGeneration]:
    if not _NODE_ID.fullmatch(node_id):
        raise BookingError(f"invalid history node namespace: {node_id}")
    _validate_namespace(namespace)
    generations = []
    for entry in _directory_entries(namespace):
        if entry.name == ".export.lock" or _TEMP_GENERATION.fullmatch(entry.name):
            continue
        if not _GENERATION.fullmatch(entry.name):
            raise BookingError(f"unexpected entry in history namespace {node_id}: {entry.name}")
        generations.append(_read_manifest(Path(entry.path), node_id))
    if len(generations) > MAX_HISTORY_GENERATIONS:
        raise BookingError(
            f"history namespace {node_id} exceeds {MAX_HISTORY_GENERATIONS} generations"
        )
    return sorted(generations, key=lambda item: (item.start_at, item.end_at, item.name))


def _read_manifest(path: Path, node_id: str) -> HistoryGeneration:
    _validate_directory(path, GENERATION_DIRECTORY_MODE, "history generation")
    fd = open_existing_regular(path / "manifest.json", expected_mode=HISTORY_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        if metadata.st_size > MAX_MANIFEST_BYTES:
            raise BookingError(f"history manifest exceeds 2 MiB: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BookingError(f"invalid cluster history manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise BookingError(f"invalid cluster history manifest: {path}")
    if manifest.get("schema_version") != HISTORY_SCHEMA_VERSION:
        raise BookingError(f"unsupported cluster history schema: {path}")
    if manifest.get("kind") != "cluster-history-generation":
        raise BookingError(f"invalid cluster history kind: {path}")
    if manifest.get("generation") != path.name or not _GENERATION.fullmatch(path.name):
        raise BookingError(f"history generation name mismatch: {path}")
    node = manifest.get("node")
    if not isinstance(node, dict) or node.get("id") != node_id:
        raise BookingError(f"history node identity mismatch: {path}")
    range_value = manifest.get("range")
    if not isinstance(range_value, dict):
        raise BookingError(f"history range is invalid: {path}")
    try:
        start = parse_iso(str(range_value["start_at"]))
        end = parse_iso(str(range_value["end_at"]))
        resolution = str(range_value["resolution"])
        seconds = int(range_value["resolution_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BookingError(f"history range is invalid: {path}") from exc
    try:
        parsed_resolution = parse_resolution(resolution)
    except ValueError as exc:
        raise BookingError(f"history range is invalid: {path}") from exc
    if end <= start or resolution_label(parsed_resolution) != resolution:
        raise BookingError(f"history range is invalid: {path}")
    _require_day_boundary(start, "history generation start")
    _require_day_boundary(end, "history generation end")
    if parsed_resolution != seconds:
        raise BookingError(f"history resolution is inconsistent: {path}")
    if manifest.get("payload_schema") != USAGE_API_VERSION:
        raise BookingError(f"history payload schema is invalid: {path}")
    if _generation_name(start, end, resolution) != path.name:
        raise BookingError(f"history range does not match generation name: {path}")
    files_value = manifest.get("files")
    if not isinstance(files_value, list) or not 1 <= len(files_value) <= MAX_HISTORY_FILES:
        raise BookingError(f"history manifest file list is invalid: {path}")
    files = tuple(_validate_file_entry(item, path) for item in files_value)
    names = [str(item["name"]) for item in files]
    if len(set(names)) != len(names):
        raise BookingError(f"history manifest repeats a payload file: {path}")
    for entry in files:
        entry_start = parse_iso(str(entry["start_at"]))
        entry_end = parse_iso(str(entry["end_at"]))
        if entry_start < start or entry_end > end:
            raise BookingError(f"history payload falls outside its generation: {path}")
    _validate_chunk_coverage(files, start, end, path)
    expected_entries = {"manifest.json", *names}
    actual_entries = {entry.name for entry in _directory_entries(path)}
    if actual_entries != expected_entries:
        raise BookingError(f"history generation contains unlisted files: {path}")
    return HistoryGeneration(node_id, path.name, path, start, end, resolution, files, manifest)


def _validate_file_entry(value: object, generation: Path) -> dict:
    if not isinstance(value, dict):
        raise BookingError(f"history payload metadata is invalid: {generation}")
    name = value.get("name")
    kind = value.get("kind")
    digest = value.get("sha256")
    size = value.get("bytes")
    records = value.get("records")
    if not isinstance(name, str) or not _PAYLOAD_NAME.fullmatch(name):
        raise BookingError(f"history payload name is invalid: {generation}")
    if kind not in {"usage-users", "usage-samples"}:
        raise BookingError(f"history payload kind is invalid: {generation / name}")
    expected_suffix = "users" if kind == "usage-users" else "samples"
    if not name.endswith(f"-{expected_suffix}.json.gz"):
        raise BookingError(f"history payload name/kind mismatch: {generation / name}")
    if value.get("schema_version") != USAGE_API_VERSION:
        raise BookingError(f"history payload schema is invalid: {generation / name}")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise BookingError(f"history payload checksum is invalid: {generation / name}")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_COMPRESSED_BYTES:
        raise BookingError(f"history payload size is invalid: {generation / name}")
    if isinstance(records, bool) or not isinstance(records, int) or records < 0:
        raise BookingError(f"history payload record count is invalid: {generation / name}")
    try:
        start = parse_iso(str(value["start_at"]))
        end = parse_iso(str(value["end_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BookingError(f"history payload range is invalid: {generation / name}") from exc
    if end <= start:
        raise BookingError(f"history payload range is invalid: {generation / name}")
    return dict(value)


def _validate_chunk_coverage(
    files: Sequence[dict],
    start: datetime,
    end: datetime,
    generation: Path,
) -> None:
    ranges: dict[str, list[tuple[datetime, datetime]]] = {
        "usage-users": [],
        "usage-samples": [],
    }
    for entry in files:
        ranges[str(entry["kind"])].append(
            (
                parse_iso(str(entry["start_at"])),
                parse_iso(str(entry["end_at"])),
            )
        )
    users = sorted(ranges["usage-users"])
    samples = sorted(ranges["usage-samples"])
    if users != samples or not users:
        raise BookingError(f"history generation has incomplete users/samples pairs: {generation}")
    cursor = start
    for chunk_start, chunk_end in users:
        if chunk_start != cursor or chunk_end - chunk_start != timedelta(days=1):
            raise BookingError(f"history generation has a gap or non-daily chunk: {generation}")
        cursor = chunk_end
    if cursor != end:
        raise BookingError(f"history generation does not cover its declared range: {generation}")


def _read_payload(generation: Path, entry: dict) -> dict:
    path = generation / str(entry["name"])
    fd = open_existing_regular(path, expected_mode=HISTORY_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        expected_size = int(entry["bytes"])
        if metadata.st_size != expected_size or metadata.st_size > MAX_COMPRESSED_BYTES:
            raise BookingError(f"history payload size mismatch: {path}")
        digest = hashlib.sha256()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise BookingError(f"history payload ended early: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if digest.hexdigest() != entry["sha256"]:
            raise BookingError(f"history payload checksum mismatch: {path}")
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as raw, gzip.GzipFile(fileobj=raw) as compressed:
            decoded = compressed.read(MAX_PAYLOAD_BYTES + 1)
        if len(decoded) > MAX_PAYLOAD_BYTES:
            raise BookingError(f"history payload expands beyond 128 MiB: {path}")
    except (OSError, EOFError) as exc:
        raise BookingError(f"cannot read compressed history payload: {path}: {exc}") from exc
    finally:
        os.close(fd)
    try:
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BookingError(f"history payload is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise BookingError(f"history payload must be an object: {path}")
    collection = "users" if entry["kind"] == "usage-users" else "records"
    records = payload.get(collection)
    if not isinstance(records, list) or len(records) != entry["records"]:
        raise BookingError(f"history payload record count mismatch: {path}")
    return payload


def _validate_public_payload(
    payload: object,
    *,
    node_id: str,
    kind: str,
    start: datetime,
    end: datetime,
) -> None:
    if not isinstance(payload, dict):
        raise BookingError("public usage query did not return an object")
    if payload.get("schema_version") != USAGE_API_VERSION or payload.get("kind") != kind:
        raise BookingError(f"public usage query returned an incompatible {kind} payload")
    if payload.get("truncated") is not False:
        raise BookingError("refusing incomplete public usage payload in cluster history")
    node = payload.get("node")
    if not isinstance(node, dict) or node.get("id") != node_id:
        raise BookingError("public usage payload has the wrong stable node identity")
    query = payload.get("query")
    if not isinstance(query, dict):
        raise BookingError("public usage payload has no query metadata")
    try:
        query_start = parse_iso(str(query["start_at"]))
        query_end = parse_iso(str(query["end_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BookingError("public usage payload query range is invalid") from exc
    if query_start != start.astimezone(timezone.utc) or query_end != end.astimezone(timezone.utc):
        raise BookingError("public usage payload query range does not match the archive chunk")
    private_key = _find_private_key(payload)
    if private_key is not None:
        raise BookingError(f"refusing private field in cluster history payload: {private_key}")


def _query_sample_batches(
    api: UsageQueryService,
    start: datetime,
    end: datetime,
    resolution: str,
    node_id: str,
) -> Iterator[tuple[datetime, datetime, dict]]:
    pending = []
    cursor = start
    while cursor < end:
        batch_end = min(cursor + timedelta(days=30), end)
        pending.append((cursor, batch_end))
        cursor = batch_end
    while pending:
        batch_start, batch_end = pending.pop(0)
        payload = api.samples(
            start=batch_start,
            end=batch_end,
            resolution=resolution,
            limit=MAX_QUERY_RECORDS,
            include_workloads=True,
        )
        if payload.get("truncated"):
            days = int((batch_end - batch_start).total_seconds() // 86400)
            if days <= 1:
                raise BookingError(
                    f"public usage query exceeded {MAX_QUERY_RECORDS} records for "
                    f"{batch_start.date()}; use a coarser resolution"
                )
            split = batch_start + timedelta(days=max(1, days // 2))
            pending[0:0] = [(batch_start, split), (split, batch_end)]
            continue
        _validate_public_payload(
            payload,
            node_id=node_id,
            kind="usage-samples",
            start=batch_start,
            end=batch_end,
        )
        yield batch_start, batch_end, payload


def _slice_samples(
    payload: dict,
    start: datetime,
    end: datetime,
    records: Sequence[dict],
) -> dict:
    query = dict(payload.get("query", {}))
    query.update(
        {
            "start_at": to_iso(start),
            "end_at": to_iso(end),
            "limit": MAX_QUERY_RECORDS,
        }
    )
    return {
        **payload,
        "query": query,
        "records": list(records),
        "truncated": False,
    }


def _users_from_samples(payload: dict) -> dict:
    return {
        "schema_version": USAGE_API_VERSION,
        "kind": "usage-users",
        "generated_at": payload.get("generated_at"),
        "node": payload.get("node"),
        "collector": payload.get("collector"),
        "query": dict(payload.get("query", {})),
        "users": summarize_public_rollups(payload.get("records", [])),
        "truncated": False,
        "warnings": list(payload.get("warnings", [])),
        "notes": [
            "Derived from the matching archived public usage samples.",
            "Whole-device utilization is not divided between shared users.",
            "Missing history is never interpreted as zero utilization.",
        ],
    }


def _find_private_key(value: object) -> Optional[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _PRIVATE_KEYS:
                return str(key)
            found = _find_private_key(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_private_key(nested)
            if found is not None:
                return found
    return None


def _validate_non_overlapping(generations: Sequence[HistoryGeneration]) -> None:
    by_node: dict[str, list[HistoryGeneration]] = {}
    for generation in generations:
        by_node.setdefault(generation.node_id, []).append(generation)
    for node_id, items in by_node.items():
        ordered = sorted(items, key=lambda item: (item.start_at, item.end_at))
        for left, right in zip(ordered, ordered[1:]):
            if right.start_at < left.end_at:
                raise BookingError(
                    f"overlapping immutable history generations for node {node_id}: "
                    f"{left.name} and {right.name}"
                )


def _prepare_namespace(root: Path, node_id: str, *, owner_uid: int) -> Path:
    namespace = root / node_id
    if not os.path.lexists(namespace):
        try:
            os.mkdir(namespace, HISTORY_DIRECTORY_MODE)
            if os.geteuid() == 0 and owner_uid != 0:
                os.chown(namespace, owner_uid, -1)
            os.chmod(namespace, HISTORY_DIRECTORY_MODE)
            fsync_directory(root)
        except FileExistsError:
            pass
    _validate_namespace(namespace, writable=True, owner_uid=owner_uid)
    return namespace


def _validate_history_root(path: Path, *, writable: bool) -> Path:
    if not path.is_absolute():
        raise BookingError("cluster history root must be an absolute path")
    if not os.path.lexists(path):
        raise BookingError(
            f"cluster history root does not exist: {path}; create a dedicated directory first"
        )
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise BookingError(f"cluster history root must not be a symlink: {path}")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise BookingError(f"cannot resolve cluster history root: {path}: {exc}") from exc
    _validate_real_directory_chain(path)
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise BookingError(
            "writable cluster history root must use the sticky bit, such as mode 1777"
        )
    return path


def _validate_namespace(
    path: Path,
    *,
    writable: bool = False,
    owner_uid: Optional[int] = None,
) -> None:
    _validate_directory(path, HISTORY_DIRECTORY_MODE, "history node namespace")
    metadata = path.lstat()
    if metadata.st_mode & 0o022:
        raise BookingError(f"history node namespace must not be group/other writable: {path}")
    expected_owner = os.geteuid() if owner_uid is None else owner_uid
    if writable and metadata.st_uid != expected_owner:
        raise BookingError(
            f"history node namespace is owned by UID {metadata.st_uid}, not writer UID "
            f"{expected_owner}: {path}"
        )


def _validate_directory(path: Path, expected_mode: int, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BookingError(f"cannot inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"{label} must be a real directory: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != expected_mode:
        raise BookingError(
            f"{label} mode is {mode:04o}; expected {expected_mode:04o}: {path}"
        )


def _validate_real_directory_chain(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise BookingError(f"cannot inspect cluster history path: {current}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BookingError(f"cluster history path contains a non-directory or symlink: {current}")


def _directory_entries(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as iterator:
            return list(iterator)
    except OSError as exc:
        raise BookingError(f"cannot list cluster history directory: {path}: {exc}") from exc


def _write_immutable_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for name in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while publishing cluster history")
            view = view[written:]
        os.fchmod(fd, HISTORY_FILE_MODE)
        os.fsync(fd)
    finally:
        os.close(fd)


def _encode_payload(payload: dict) -> bytes:
    raw = _json_bytes(payload)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise BookingError("one public usage payload exceeds 128 MiB")
    encoded = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(encoded) > MAX_COMPRESSED_BYTES:
        raise BookingError("one compressed public usage payload exceeds 64 MiB")
    return encoded


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _generation_name(start: datetime, end: datetime, resolution: str) -> str:
    return (
        f"{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{resolution}"
    )


def _parse_history_time(value: str, reference: datetime) -> datetime:
    raw = value.strip()
    if raw == "now":
        return reference
    if raw.startswith("-"):
        return reference - timedelta(seconds=parse_duration_seconds(raw[1:]))
    try:
        return parse_iso(raw)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise BookingError("history time must be YYYY-MM-DD, ISO 8601, or a negative duration") from exc


def _require_day_boundary(value: datetime, label: str) -> None:
    normalized = value.astimezone(timezone.utc)
    if any((normalized.hour, normalized.minute, normalized.second, normalized.microsecond)):
        raise BookingError(f"{label} must be a complete UTC-day boundary (00:00Z)")


def _cleanup_stale_temporary_generations(
    namespace: Path,
    *,
    owner_uid: int,
) -> None:
    allowed_owner_uids = {os.geteuid()}
    if os.geteuid() == 0:
        allowed_owner_uids.add(owner_uid)
    removed = False
    for entry in _directory_entries(namespace):
        if not entry.name.startswith(".tmp-"):
            continue
        if not _TEMP_GENERATION.fullmatch(entry.name):
            raise BookingError(
                f"unexpected temporary entry in history namespace: {entry.name}"
            )
        path = Path(entry.path)
        if not _remove_temporary_generation(
            path,
            allowed_owner_uids=allowed_owner_uids,
        ):
            raise BookingError(
                f"cannot safely remove stale history export directory: {path}"
            )
        removed = True
    if removed:
        fsync_directory(namespace)


def _remove_temporary_generation(
    path: Path,
    *,
    allowed_owner_uids: Optional[set[int]] = None,
) -> bool:
    if not os.path.lexists(path):
        return True
    try:
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return False
        if allowed_owner_uids is not None and metadata.st_uid not in allowed_owner_uids:
            return False
        entries = _directory_entries(path)
        for entry in entries:
            item = Path(entry.path)
            item_metadata = item.lstat()
            if not stat.S_ISREG(item_metadata.st_mode) or item_metadata.st_nlink != 1:
                return False
            if (
                allowed_owner_uids is not None
                and item_metadata.st_uid not in allowed_owner_uids
            ):
                return False
        os.chmod(path, 0o700)
        for entry in entries:
            Path(entry.path).unlink()
        path.rmdir()
        return True
    except (OSError, BookingError):
        return False


@contextmanager
def _namespace_lock(
    namespace: Path,
    timeout_seconds: float,
    *,
    owner_uid: int,
) -> Iterator[None]:
    path = namespace / ".export.lock"
    flags = os.O_RDWR | os.O_CREAT
    for name in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if os.geteuid() == 0 and metadata.st_uid == 0 and owner_uid != 0:
            os.fchown(fd, owner_uid, -1)
            metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != owner_uid
        ):
            raise BookingError(f"unsafe cluster history export lock: {path}")
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise BookingError(
                        f"timed out waiting for cluster history export lock: {path}"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


__all__ = [
    "HISTORY_SCHEMA_VERSION",
    "ArchivedUsage",
    "export_cluster_history",
    "latest_history_end",
    "load_archived_user_usage",
    "resolve_history_window",
    "verify_cluster_history",
]
