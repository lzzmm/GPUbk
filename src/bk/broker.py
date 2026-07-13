from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pwd
import signal
import socket
import stat
import struct
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from .advisor import GpuAdvice, build_gpu_advice
from .config import Config, load_config
from .models import Actor, BookingError, BookingRequest, BookingResult, EditRequest
from .storage import LedgerStore
from .timeparse import parse_iso, to_iso, utc_now


BROKER_SCHEMA_VERSION = "gpubk.broker.v1"
BROKER_MAX_FRAME_BYTES = 1024 * 1024
BROKER_MAX_CLIENTS = 32
BROKER_IO_TIMEOUT_SECONDS = 5.0
BROKER_TRANSACTION_RETRIES = 8
BROKER_MAX_JOB_CHANGES = 256
BROKER_JOB_PATCH_OPERATION = "ledger.commit-own-job-patch"
JOB_BINDING_FIELDS = frozenset({"spec_id", "digest", "summary", "submitted_at"})
JOB_MUTABLE_FIELDS = frozenset(
    {
        "cancel_requested_at",
        "claim_token",
        "claimed_at",
        "exit_code",
        "finished_at",
        "launch_guard_key",
        "launch_guard_state",
        "log_rotated",
        "log_warning",
        "message",
        "recovered_at",
        "recovery_state",
        "recovery_worker_id",
        "runner_host",
        "runner_pid",
        "started_at",
        "status",
        "waiting_since",
        "worker_id",
        "worker_lease_id",
    }
)


class BrokerLedgerStore(LedgerStore):
    """Read locally, but route every supported ledger mutation through the broker."""

    def __init__(self, config: Config):
        if config.broker_socket is None or config.broker_uid is None:
            raise ValueError("broker ledger store requires broker configuration")
        super().__init__(
            config.data_dir,
            config.lock_timeout_seconds,
            config.backup_keep,
            config.file_mode,
            config.dir_mode,
            config.storage_gid,
        )
        self._broker = BrokerClient(config)

    def load(self) -> dict:
        return super().load_read_only()

    def load_read_only(self) -> dict:
        return super().load_read_only()

    def broker_add_booking(self, request: BookingRequest) -> BookingResult:
        payload = _booking_request_payload(request)
        return _booking_result(self._broker.call("booking.add", payload))

    def broker_edit_booking(self, request: EditRequest) -> BookingResult:
        payload = _edit_request_payload(request)
        return _booking_result(self._broker.call("booking.edit", payload))

    def broker_cancel_booking(self, reservation_id: str, actor: Actor) -> dict:
        del actor
        result = self._broker.call(
            "booking.cancel",
            {"reservation_id": str(reservation_id)},
        )
        if not isinstance(result, dict):
            raise BookingError("broker returned an invalid cancellation result")
        return result

    def transaction(self, mutator):
        try:
            return self._patch_transaction(mutator)
        except BookingError as exc:
            if str(exc) != f"unsupported broker operation: {BROKER_JOB_PATCH_OPERATION}":
                raise
        return self._legacy_transaction(mutator)

    def _patch_transaction(self, mutator):
        for _attempt in range(BROKER_TRANSACTION_RETRIES):
            current = super().load_read_only()
            base_digest = _ledger_digest(current)
            ledger = copy.deepcopy(current)
            mutation = mutator(ledger)
            if not isinstance(mutation, tuple) or len(mutation) != 4:
                raise BookingError("ledger mutator returned an invalid transaction")
            new_ledger, result, logs, changed = mutation
            log_items = list(logs)
            if not changed and not log_items:
                return result
            changes = _job_transaction_changes(current, new_ledger)
            response = self._broker.call(
                BROKER_JOB_PATCH_OPERATION,
                {
                    "base_digest": base_digest,
                    "changes": changes,
                    "logs": log_items,
                    "changed": bool(changed),
                },
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("committed"), bool
            ):
                raise BookingError("broker returned an invalid transaction result")
            if response["committed"]:
                return result
        raise BookingError("ledger changed repeatedly; retry the operation")

    def _legacy_transaction(self, mutator):
        for _attempt in range(BROKER_TRANSACTION_RETRIES):
            snapshot = self._broker.call("ledger.snapshot", {})
            if not isinstance(snapshot, dict) or not isinstance(
                snapshot.get("ledger"), dict
            ):
                raise BookingError("broker returned an invalid ledger snapshot")
            base_digest = snapshot.get("digest")
            if not isinstance(base_digest, str):
                raise BookingError("broker returned an invalid ledger digest")
            ledger = copy.deepcopy(snapshot["ledger"])
            mutation = mutator(ledger)
            if not isinstance(mutation, tuple) or len(mutation) != 4:
                raise BookingError("ledger mutator returned an invalid transaction")
            new_ledger, result, logs, changed = mutation
            log_items = list(logs)
            if not changed and not log_items:
                return result
            response = self._broker.call(
                "ledger.commit-own-job",
                {
                    "base_digest": base_digest,
                    "ledger": new_ledger,
                    "logs": log_items,
                    "changed": bool(changed),
                },
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("committed"), bool
            ):
                raise BookingError("broker returned an invalid transaction result")
            if response["committed"]:
                return result
        raise BookingError("ledger changed repeatedly; retry the operation")

    def reset(self) -> dict:
        raise BookingError(
            "broker-backed data requires an administrator maintenance command"
        )


class BrokerClient:
    def __init__(self, config: Config):
        if config.broker_socket is None or config.broker_uid is None:
            raise ValueError("broker client requires broker_socket and broker_uid")
        self.socket_path = config.broker_socket
        self.broker_uid = config.broker_uid
        self.broker_gid = config.broker_gid
        self.socket_mode = config.broker_socket_mode
        self.timeout = max(0.2, min(float(config.lock_timeout_seconds), 60.0))

    def call(self, operation: str, payload: dict) -> object:
        request_id = str(uuid.uuid4())
        request = {
            "schema_version": BROKER_SCHEMA_VERSION,
            "request_id": request_id,
            "operation": operation,
            "payload": payload,
        }
        _validate_socket_leaf(
            self.socket_path,
            expected_uid=self.broker_uid,
            expected_gid=self.broker_gid,
            expected_mode=self.socket_mode,
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.timeout)
        try:
            client.connect(str(self.socket_path))
            _send_frame(client, request)
            response = _receive_frame(client)
        except (OSError, TimeoutError) as exc:
            raise BookingError(
                f"GPUBK broker is unavailable at {self.socket_path}: {exc}"
            ) from exc
        finally:
            client.close()
        if response.get("schema_version") != BROKER_SCHEMA_VERSION:
            raise BookingError("broker returned an unsupported protocol version")
        if response.get("request_id") != request_id:
            raise BookingError("broker response request ID does not match")
        if response.get("ok") is True:
            return response.get("result")
        error = response.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        raise BookingError(str(message or "broker rejected the request"))


class BrokerServer:
    def __init__(
        self,
        config: Config,
        *,
        store: Optional[LedgerStore] = None,
        credential_resolver: Optional[
            Callable[[socket.socket], tuple[int, int, int]]
        ] = None,
        advice_provider: Callable[[Config], GpuAdvice] = build_gpu_advice,
        require_root_config: bool = True,
    ):
        if config.broker_socket is None or config.broker_uid is None:
            raise BookingError("broker_socket and broker_uid are required")
        if os.geteuid() != config.broker_uid:
            raise BookingError(
                f"broker must run as configured service UID {config.broker_uid}; "
                f"current UID is {os.geteuid()}"
            )
        if require_root_config and (
            config.config_file is None or config.config_owner_uid != 0
        ):
            raise BookingError(
                "broker requires a trusted root-owned system configuration"
            )
        self.config = config
        self.socket_path = config.broker_socket
        self.store = store or LedgerStore(
            config.data_dir,
            config.lock_timeout_seconds,
            config.backup_keep,
            config.file_mode,
            config.dir_mode,
            config.storage_gid,
        )
        self._credential_resolver = credential_resolver or _linux_peer_credentials
        self._advice_provider = advice_provider
        self._listener: Optional[socket.socket] = None
        self._socket_identity: Optional[tuple[int, int]] = None
        self._stop = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self._slots = threading.BoundedSemaphore(BROKER_MAX_CLIENTS)
        self._pool = ThreadPoolExecutor(
            max_workers=BROKER_MAX_CLIENTS,
            thread_name_prefix="gpubk-broker",
        )

    def serve_forever(self) -> None:
        self._prepare()
        listener = self._listener
        if listener is None:
            raise BookingError("broker listener was not initialized")
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                if not self._slots.acquire(blocking=False):
                    connection.close()
                    continue
                self._pool.submit(self._handle_and_release, connection)
        finally:
            self.close()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            listener = self._listener
            self._listener = None
            if listener is not None:
                listener.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._remove_owned_socket()

    def request_stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            listener.close()

    def _prepare(self) -> None:
        _validate_service_storage(self.config)
        _validate_socket_parent(self.socket_path.parent, self.config.broker_uid)
        self.store.ensure()
        self.store.load()
        self._remove_stale_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # A client may observe the socket as soon as bind() creates its path.
        # Give it the final mode immediately; chmod below remains a verified
        # repair step for platforms with unusual Unix-socket mode behavior.
        bind_umask = 0o777 & ~self.config.broker_socket_mode
        previous_umask = os.umask(bind_umask)
        try:
            listener.bind(str(self.socket_path))
        finally:
            os.umask(previous_umask)
        try:
            if self.config.broker_gid is not None:
                os.chown(self.socket_path, -1, self.config.broker_gid)
            os.chmod(self.socket_path, self.config.broker_socket_mode)
            _validate_socket_leaf(
                self.socket_path,
                expected_uid=self.config.broker_uid,
                expected_gid=self.config.broker_gid,
                expected_mode=self.config.broker_socket_mode,
            )
            metadata = self.socket_path.lstat()
            self._socket_identity = (metadata.st_dev, metadata.st_ino)
            listener.listen(64)
            listener.settimeout(0.5)
            self._listener = listener
        except BaseException:
            listener.close()
            self._remove_owned_socket()
            raise

    def _handle_and_release(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(BROKER_IO_TIMEOUT_SECONDS)
            self._handle(connection)
        finally:
            connection.close()
            self._slots.release()

    def _handle(self, connection: socket.socket) -> None:
        request_id = None
        try:
            pid, uid, gid = self._credential_resolver(connection)
            actor = _actor_for_uid(uid)
            request = _receive_frame(connection)
            request_id = request.get("request_id")
            result = self._dispatch(request, actor, pid=pid, gid=gid)
            response = {
                "schema_version": BROKER_SCHEMA_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        except (BookingError, ValueError, TypeError, KeyError, OSError) as exc:
            response = {
                "schema_version": BROKER_SCHEMA_VERSION,
                "request_id": request_id,
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        try:
            _send_frame(connection, response)
        except (OSError, TimeoutError):
            return

    def _dispatch(self, request: dict, actor: Actor, *, pid: int, gid: int) -> object:
        del pid, gid
        _require_keys(
            request,
            {"schema_version", "request_id", "operation", "payload"},
            required={"schema_version", "request_id", "operation", "payload"},
            label="broker request",
        )
        if request.get("schema_version") != BROKER_SCHEMA_VERSION:
            raise BookingError("unsupported broker protocol version")
        try:
            uuid.UUID(str(request.get("request_id")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise BookingError("invalid broker request ID") from exc
        operation = request.get("operation")
        payload = request.get("payload")
        if not isinstance(operation, str) or not isinstance(payload, dict):
            raise BookingError("broker operation and payload are required")
        if operation == "ping":
            _require_keys(payload, set(), label="ping payload")
            return {
                "service_uid": self.config.broker_uid,
                "actor_uid": actor.uid,
                "gpu_count": self.config.gpu_count,
            }
        if operation == "ledger.snapshot":
            _require_keys(payload, set(), label="ledger snapshot payload")
            ledger = self.store.load()
            return {"ledger": ledger, "digest": _ledger_digest(ledger)}
        if operation == "ledger.commit-own-job":
            return self._commit_own_job_transaction(payload, actor)
        if operation == BROKER_JOB_PATCH_OPERATION:
            return self._commit_own_job_patch(payload, actor)
        if operation == "booking.add":
            request_item = _decode_booking_request(payload, actor)
            advice = self._advice_provider(self.config)
            request_item = replace(
                request_item,
                gpu_order=list(advice.order),
                gpu_scores=dict(advice.scores),
                gpu_memory_capacity_mb=advice.memory_capacities_mb,
            )
            from .scheduler import add_booking

            return _booking_result_payload(
                add_booking(self.store, self.config, request_item)
            )
        if operation == "booking.edit":
            request_item = _decode_edit_request(payload, actor)
            advice = self._advice_provider(self.config)
            request_item = replace(
                request_item,
                gpu_order=list(advice.order),
                gpu_scores=dict(advice.scores),
                gpu_memory_capacity_mb=advice.memory_capacities_mb,
            )
            from .scheduler import edit_booking

            return _booking_result_payload(
                edit_booking(self.store, self.config, request_item)
            )
        if operation == "booking.cancel":
            _require_keys(payload, {"reservation_id"}, label="cancellation payload")
            reservation_id = payload.get("reservation_id")
            if not isinstance(reservation_id, str) or not reservation_id:
                raise BookingError("reservation_id is required")
            from .scheduler import cancel_booking

            return cancel_booking(self.store, reservation_id, actor)
        raise BookingError(f"unsupported broker operation: {operation}")

    def _commit_own_job_transaction(self, payload: dict, actor: Actor) -> dict:
        _require_keys(
            payload,
            {"base_digest", "ledger", "logs", "changed"},
            required={"base_digest", "ledger", "logs", "changed"},
            label="job transaction payload",
        )
        base_digest = _string(payload.get("base_digest"), "base_digest")
        proposed = payload.get("ledger")
        logs = payload.get("logs")
        changed = _boolean(payload.get("changed"), "changed")
        if not isinstance(proposed, dict) or not isinstance(logs, list):
            raise BookingError("job transaction ledger and logs are required")
        if len(logs) > 256:
            raise BookingError("job transaction contains too many audit events")

        def mutate(current: dict):
            if _ledger_digest(current) != base_digest:
                return current, {"committed": False}, [], False
            sanitized_logs = _validate_own_job_mutation(
                current,
                proposed,
                logs,
                actor,
            )
            actual_changed = proposed != current
            if changed != actual_changed:
                raise BookingError(
                    "job transaction changed flag does not match its ledger"
                )
            return proposed, {"committed": True}, sanitized_logs, actual_changed

        return self.store.transaction(mutate)

    def _commit_own_job_patch(self, payload: dict, actor: Actor) -> dict:
        _require_keys(
            payload,
            {"base_digest", "changes", "logs", "changed"},
            required={"base_digest", "changes", "logs", "changed"},
            label="job patch payload",
        )
        base_digest = _string(payload.get("base_digest"), "base_digest")
        changes = payload.get("changes")
        logs = payload.get("logs")
        changed = _boolean(payload.get("changed"), "changed")
        if not isinstance(changes, list) or not isinstance(logs, list):
            raise BookingError("job patch changes and logs must be arrays")
        if len(changes) > BROKER_MAX_JOB_CHANGES:
            raise BookingError("job patch contains too many reservation changes")
        if len(logs) > BROKER_MAX_JOB_CHANGES:
            raise BookingError("job patch contains too many audit events")

        def mutate(current: dict):
            if _ledger_digest(current) != base_digest:
                return current, {"committed": False}, [], False
            current_reservations = current.get("reservations")
            if not isinstance(current_reservations, list):
                raise BookingError("job patch ledger reservations must be a list")
            proposed = {**current, "reservations": list(current_reservations)}
            seen_indexes = set()
            for raw_change in changes:
                if not isinstance(raw_change, dict):
                    raise BookingError("job patch change must be an object")
                _require_keys(
                    raw_change,
                    {"index", "reservation"},
                    required={"index", "reservation"},
                    label="job patch change",
                )
                index = _integer(raw_change.get("index"), "job patch index")
                replacement = raw_change.get("reservation")
                if index < 0 or index >= len(current_reservations):
                    raise BookingError("job patch index is out of range")
                if index in seen_indexes:
                    raise BookingError("job patch contains a duplicate index")
                if not isinstance(replacement, dict):
                    raise BookingError("job patch reservation must be an object")
                seen_indexes.add(index)
                proposed["reservations"][index] = copy.deepcopy(replacement)

            sanitized_logs = _validate_own_job_mutation(
                current,
                proposed,
                logs,
                actor,
            )
            actual_changed = proposed != current
            if changed != actual_changed:
                raise BookingError(
                    "job patch changed flag does not match its reservation changes"
                )
            return proposed, {"committed": True}, sanitized_logs, actual_changed

        return self.store.transaction(mutate)

    def _remove_stale_socket(self) -> None:
        if not os.path.lexists(self.socket_path):
            return
        metadata = self.socket_path.lstat()
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != self.config.broker_uid
        ):
            raise BookingError(
                f"refusing unsafe existing broker path: {self.socket_path}"
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            self.socket_path.unlink()
        except OSError as exc:
            raise BookingError(f"cannot verify existing broker socket: {exc}") from exc
        else:
            raise BookingError(
                f"another GPUBK broker is already listening: {self.socket_path}"
            )
        finally:
            probe.close()

    def _remove_owned_socket(self) -> None:
        identity = self._socket_identity
        self._socket_identity = None
        if identity is None or not os.path.lexists(self.socket_path):
            return
        metadata = self.socket_path.lstat()
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == self.config.broker_uid
            and (metadata.st_dev, metadata.st_ino) == identity
        ):
            self.socket_path.unlink(missing_ok=True)


def ledger_store_for_config(config: Config) -> LedgerStore:
    if config.broker_socket is not None:
        return BrokerLedgerStore(config)
    return LedgerStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.backup_keep,
        config.file_mode,
        config.dir_mode,
        config.storage_gid,
    )


def run_broker_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bk broker")
    parser.add_argument(
        "--check", action="store_true", help="validate configuration and exit"
    )
    args = parser.parse_args(argv)
    config = load_config()
    server = BrokerServer(config)
    if args.check:
        try:
            _validate_service_storage(config)
            _validate_socket_parent(config.broker_socket.parent, config.broker_uid)
            print(
                f"broker ready: uid={config.broker_uid} socket={config.broker_socket} "
                f"mode={config.broker_socket_mode:04o}"
            )
            return 0
        finally:
            server.close()

    previous = {}

    def stop(signum, frame):
        del signum, frame
        server.request_stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, stop)
    try:
        server.serve_forever()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def _linux_peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise BookingError("GPUBK broker requires Linux SO_PEERCRED")
    size = struct.calcsize("3i")
    raw = connection.getsockopt(socket.SOL_SOCKET, option, size)
    if len(raw) != size:
        raise BookingError("kernel returned malformed peer credentials")
    pid, uid, gid = struct.unpack("3i", raw)
    if pid <= 0 or uid < 0 or gid < 0:
        raise BookingError("kernel returned invalid peer credentials")
    return pid, uid, gid


def _actor_for_uid(uid: int) -> Actor:
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    return Actor(uid=int(uid), username=str(username))


def _validate_service_storage(config: Config) -> None:
    if not os.path.lexists(config.data_dir):
        raise BookingError(f"broker data directory does not exist: {config.data_dir}")
    metadata = config.data_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"broker data path is not a directory: {config.data_dir}")
    if metadata.st_uid != config.broker_uid:
        raise BookingError(
            f"broker data directory must be owned by service UID {config.broker_uid}"
        )
    if stat.S_IMODE(metadata.st_mode) != config.dir_mode:
        raise BookingError(
            f"broker data directory mode must be {config.dir_mode:04o}: {config.data_dir}"
        )


def _validate_socket_parent(path: Path, broker_uid: int) -> None:
    if not os.path.lexists(path):
        raise BookingError(f"broker socket directory does not exist: {path}")
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"broker socket parent is not a real directory: {path}")
    if metadata.st_uid not in {0, broker_uid}:
        raise BookingError(f"broker socket directory has an untrusted owner: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        raise BookingError(
            f"broker socket directory must not be group/other writable: {path}"
        )


def _validate_socket_leaf(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: Optional[int],
    expected_mode: int,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BookingError(f"GPUBK broker socket does not exist: {path}") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise BookingError(f"broker path is not a Unix socket: {path}")
    if metadata.st_uid != expected_uid:
        raise BookingError(f"broker socket must be owned by UID {expected_uid}: {path}")
    if expected_gid is not None and metadata.st_gid != expected_gid:
        raise BookingError(f"broker socket must use GID {expected_gid}: {path}")
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode != expected_mode:
        raise BookingError(
            f"broker socket mode must be {expected_mode:04o}, found {actual_mode:04o}: {path}"
        )


def _ledger_digest(ledger: dict) -> str:
    payload = json.dumps(
        ledger,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_own_job_mutation(
    current: dict,
    proposed: dict,
    logs: list,
    actor: Actor,
) -> list[dict]:
    if set(current) != set(proposed):
        raise BookingError("job transaction cannot add or remove ledger fields")
    for key in current:
        if key != "reservations" and proposed.get(key) != current.get(key):
            raise BookingError(f"job transaction cannot modify ledger field {key}")
    current_reservations = current.get("reservations")
    proposed_reservations = proposed.get("reservations")
    if not isinstance(current_reservations, list) or not isinstance(
        proposed_reservations, list
    ):
        raise BookingError("job transaction reservations must be lists")
    if len(current_reservations) != len(proposed_reservations):
        raise BookingError("job transaction cannot add or remove reservations")

    owned = {}
    for before, after in zip(current_reservations, proposed_reservations):
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise BookingError("job transaction contains an invalid reservation")
        if before.get("id") != after.get("id"):
            raise BookingError("job transaction cannot reorder reservations")
        reservation_id = str(before.get("id", ""))
        if int(before.get("uid", -1)) != actor.uid:
            if after != before:
                raise BookingError(
                    "job transaction cannot modify another UID's reservation"
                )
            continue
        _validate_owned_reservation_job_change(before, after)
        owned[reservation_id] = after

    sanitized = []
    for raw in logs:
        if not isinstance(raw, dict):
            raise BookingError("job transaction audit event must be an object")
        reservation_id = raw.get("reservation_id")
        if not isinstance(reservation_id, str) or reservation_id not in owned:
            raise BookingError(
                "job transaction audit event must reference an owned reservation"
            )
        action = raw.get("action")
        if (
            not isinstance(action, str)
            or not action.startswith("job-")
            or len(action) > 64
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz-" for character in action
            )
        ):
            raise BookingError("job transaction audit action is invalid")
        message = str(raw.get("message", ""))[:1000]
        reservation = owned[reservation_id]
        job = reservation.get("job")
        sanitized.append(
            {
                "ts": to_iso(utc_now()),
                "uid": actor.uid,
                "username": actor.username,
                "action": action,
                "reservation_id": reservation_id,
                "op_id": reservation.get("op_id"),
                "gpus": reservation.get("gpus", []),
                "mode": reservation.get("mode"),
                "start_at": reservation.get("start_at"),
                "end_at": reservation.get("end_at"),
                "result": job.get("status") if isinstance(job, dict) else None,
                "message": message,
            }
        )
    return sanitized


def _validate_owned_reservation_job_change(before: dict, after: dict) -> None:
    mutable = {"status", "updated_at", "job"}
    for key in set(before) | set(after):
        if key not in mutable and before.get(key) != after.get(key):
            raise BookingError(f"job transaction cannot modify reservation field {key}")
    before_status = before.get("status")
    after_status = after.get("status")
    if after_status != before_status and not (
        before_status == "active" and after_status == "expired"
    ):
        raise BookingError("job transaction may only expire its own active reservation")
    if after_status == "expired" and before_status != "expired":
        if parse_iso(_string(before.get("end_at"), "end_at")) > utc_now():
            raise BookingError(
                "job transaction cannot expire a reservation before its end"
            )
    before_job = before.get("job")
    after_job = after.get("job")
    if not isinstance(before_job, dict) or not isinstance(after_job, dict):
        if before_job != after_job:
            raise BookingError("job transaction cannot add or remove a scheduled job")
        return
    for key in set(before_job) | set(after_job):
        if key not in JOB_MUTABLE_FIELDS and before_job.get(key) != after_job.get(key):
            label = "binding" if key in JOB_BINDING_FIELDS else "unknown"
            raise BookingError(f"job transaction cannot modify {label} job field {key}")


def _job_transaction_changes(current: dict, proposed: object) -> list[dict]:
    if not isinstance(proposed, dict):
        raise BookingError("ledger mutator returned an invalid ledger")
    if set(current) != set(proposed):
        raise BookingError("job transaction cannot add or remove ledger fields")
    for key in current:
        if key != "reservations" and proposed.get(key) != current.get(key):
            raise BookingError(f"job transaction cannot modify ledger field {key}")
    current_reservations = current.get("reservations")
    proposed_reservations = proposed.get("reservations")
    if not isinstance(current_reservations, list) or not isinstance(
        proposed_reservations, list
    ):
        raise BookingError("job transaction reservations must be lists")
    if len(current_reservations) != len(proposed_reservations):
        raise BookingError("job transaction cannot add or remove reservations")

    changes = []
    for index, (before, after) in enumerate(
        zip(current_reservations, proposed_reservations)
    ):
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise BookingError("job transaction contains an invalid reservation")
        if before.get("id") != after.get("id"):
            raise BookingError("job transaction cannot reorder reservations")
        if before != after:
            changes.append(
                {
                    "index": index,
                    "reservation": copy.deepcopy(after),
                }
            )
    if len(changes) > BROKER_MAX_JOB_CHANGES:
        raise BookingError("job transaction contains too many reservation changes")
    return changes


def _send_frame(connection: socket.socket, document: dict) -> None:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > BROKER_MAX_FRAME_BYTES:
        raise BookingError("broker message exceeds 1 MiB")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _receive_frame(connection: socket.socket) -> dict:
    header = _receive_exact(connection, 4)
    length = struct.unpack("!I", header)[0]
    if length < 2 or length > BROKER_MAX_FRAME_BYTES:
        raise BookingError("invalid broker frame length")
    payload = _receive_exact(connection, length)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BookingError("broker message is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise BookingError("broker message must be a JSON object")
    return document


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise BookingError("broker connection closed before the frame completed")
        chunks.extend(chunk)
    return bytes(chunks)


def _booking_request_payload(request: BookingRequest) -> dict:
    payload = {
        "count": request.count,
        "duration_seconds": request.duration_seconds,
        "start_at": to_iso(request.start_at),
        "mode": request.mode,
        "preferred_gpus": request.preferred_gpus,
        "op_id": request.op_id,
        "allow_queue": request.allow_queue,
        "job_spec_id": request.job_spec_id,
        "job_digest": request.job_digest,
        "job_summary": request.job_summary,
        "job_digest_aliases": request.job_digest_aliases,
        "expected_memory_mb": request.expected_memory_mb,
        "share_units": request.share_units,
    }
    if request.excluded_gpus is not None:
        payload["excluded_gpus"] = request.excluded_gpus
    return payload


def _edit_request_payload(request: EditRequest) -> dict:
    payload = {
        "reservation_id": request.reservation_id,
        "op_id": request.op_id,
        "start_at": to_iso(request.start_at) if request.start_at is not None else None,
        "duration_seconds": request.duration_seconds,
        "mode": request.mode,
        "preferred_gpus": request.preferred_gpus,
        "count": request.count,
        "allow_queue": request.allow_queue,
        "expected_memory_mb": request.expected_memory_mb,
        "update_expected_memory": request.update_expected_memory,
        "share_units": request.share_units,
        "update_share_units": request.update_share_units,
    }
    if request.excluded_gpus is not None:
        payload["excluded_gpus"] = request.excluded_gpus
    return payload


def _decode_booking_request(payload: dict, actor: Actor) -> BookingRequest:
    allowed = {
        "count",
        "duration_seconds",
        "start_at",
        "mode",
        "preferred_gpus",
        "excluded_gpus",
        "op_id",
        "allow_queue",
        "job_spec_id",
        "job_digest",
        "job_summary",
        "job_digest_aliases",
        "expected_memory_mb",
        "share_units",
    }
    _require_keys(
        payload,
        allowed,
        required={"count", "duration_seconds", "start_at", "mode"},
        label="booking payload",
    )
    return BookingRequest(
        actor=actor,
        count=_integer(payload.get("count"), "count"),
        duration_seconds=_integer(payload.get("duration_seconds"), "duration_seconds"),
        start_at=parse_iso(_string(payload.get("start_at"), "start_at")),
        mode=_string(payload.get("mode"), "mode"),
        preferred_gpus=_optional_int_list(
            payload.get("preferred_gpus"), "preferred_gpus"
        ),
        excluded_gpus=_optional_int_list(
            payload.get("excluded_gpus"), "excluded_gpus"
        ),
        op_id=_optional_string(payload.get("op_id"), "op_id"),
        allow_queue=_boolean(payload.get("allow_queue", False), "allow_queue"),
        job_spec_id=_optional_string(payload.get("job_spec_id"), "job_spec_id"),
        job_digest=_optional_string(payload.get("job_digest"), "job_digest"),
        job_summary=_optional_string(payload.get("job_summary"), "job_summary"),
        job_digest_aliases=_optional_string_list(
            payload.get("job_digest_aliases"),
            "job_digest_aliases",
        ),
        expected_memory_mb=_optional_integer(
            payload.get("expected_memory_mb"),
            "expected_memory_mb",
        ),
        share_units=_optional_integer(payload.get("share_units"), "share_units"),
    )


def _decode_edit_request(payload: dict, actor: Actor) -> EditRequest:
    allowed = {
        "reservation_id",
        "op_id",
        "start_at",
        "duration_seconds",
        "mode",
        "preferred_gpus",
        "excluded_gpus",
        "count",
        "allow_queue",
        "expected_memory_mb",
        "update_expected_memory",
        "share_units",
        "update_share_units",
    }
    _require_keys(payload, allowed, required={"reservation_id"}, label="edit payload")
    start_at = payload.get("start_at")
    return EditRequest(
        actor=actor,
        reservation_id=_string(payload.get("reservation_id"), "reservation_id"),
        op_id=_optional_string(payload.get("op_id"), "op_id"),
        start_at=parse_iso(_string(start_at, "start_at"))
        if start_at is not None
        else None,
        duration_seconds=_optional_integer(
            payload.get("duration_seconds"),
            "duration_seconds",
        ),
        mode=_optional_string(payload.get("mode"), "mode"),
        preferred_gpus=_optional_int_list(
            payload.get("preferred_gpus"), "preferred_gpus"
        ),
        excluded_gpus=_optional_int_list(
            payload.get("excluded_gpus"), "excluded_gpus"
        ),
        count=_optional_integer(payload.get("count"), "count"),
        allow_queue=_boolean(payload.get("allow_queue", False), "allow_queue"),
        expected_memory_mb=_optional_integer(
            payload.get("expected_memory_mb"),
            "expected_memory_mb",
        ),
        update_expected_memory=_boolean(
            payload.get("update_expected_memory", False),
            "update_expected_memory",
        ),
        share_units=_optional_integer(payload.get("share_units"), "share_units"),
        update_share_units=_boolean(
            payload.get("update_share_units", False),
            "update_share_units",
        ),
    )


def _booking_result_payload(result: BookingResult) -> dict:
    return {
        "reservation": result.reservation,
        "created": result.created,
        "message": result.message,
        "queued": result.queued,
    }


def _booking_result(value: object) -> BookingResult:
    if not isinstance(value, dict):
        raise BookingError("broker returned an invalid booking result")
    _require_keys(
        value,
        {"reservation", "created", "message", "queued"},
        required={"reservation", "created", "message", "queued"},
        label="booking result",
    )
    reservation = value.get("reservation")
    if not isinstance(reservation, dict):
        raise BookingError("broker returned an invalid reservation")
    return BookingResult(
        reservation=reservation,
        created=_boolean(value.get("created"), "created"),
        message=_string(value.get("message"), "message"),
        queued=_boolean(value.get("queued"), "queued"),
    )


def _require_keys(
    value: dict,
    allowed: set[str],
    *,
    required: Optional[set[str]] = None,
    label: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise BookingError(f"{label} contains unknown field(s): {', '.join(unknown)}")
    missing = sorted((required or set()) - set(value))
    if missing:
        raise BookingError(f"{label} is missing field(s): {', '.join(missing)}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BookingError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> Optional[str]:
    return None if value is None else _string(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BookingError(f"{label} must be an integer")
    return int(value)


def _optional_integer(value: object, label: str) -> Optional[int]:
    return None if value is None else _integer(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise BookingError(f"{label} must be a boolean")
    return value


def _optional_int_list(value: object, label: str) -> Optional[list[int]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise BookingError(f"{label} must be an integer array")
    return [_integer(item, label) for item in value]


def _optional_string_list(value: object, label: str) -> Optional[list[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise BookingError(f"{label} must be a string array")
    return [_string(item, label) for item in value]
