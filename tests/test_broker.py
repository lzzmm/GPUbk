import os
import stat
import tempfile
import threading
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from bk.broker import (
    BROKER_JOB_PATCH_OPERATION,
    BROKER_MAX_FRAME_BYTES,
    BrokerClient,
    BrokerLedgerStore,
    BrokerServer,
    _booking_request_payload,
    _edit_request_payload,
    _ledger_digest,
)
from bk.config import (
    BROKER_ALL_SOCKET_MODE,
    BROKER_DIR_MODE,
    BROKER_FILE_MODE,
    Config,
)
from bk.granularity import floor_to_slot
from bk.models import Actor, BookingError, BookingRequest, EditRequest
from bk.scheduler import add_booking, cancel_booking, cancel_booking_as_admin, edit_booking
from bk.storage import LedgerStore
from bk.timeparse import to_iso, utc_now


class RunningBroker:
    def __init__(self, server: BrokerServer, socket_path: Path):
        self.server = server
        self.socket_path = socket_path
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 3
        client = BrokerClient(self.server.config)
        client.timeout = 0.2
        last_error = None
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                try:
                    response = client.call("ping", {})
                except BookingError as exc:
                    last_error = exc
                else:
                    if response.get("service_uid") == self.server.config.broker_uid:
                        return self.server
            if not self.thread.is_alive():
                break
            time.sleep(0.01)
        self.server.close()
        self.thread.join(timeout=2)
        detail = f": {last_error}" if last_error is not None else ""
        raise RuntimeError(f"broker did not start{detail}")

    def __exit__(self, exc_type, exc, traceback):
        self.server.close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("broker did not stop")


class BrokerTests(unittest.TestCase):
    def test_optional_gpu_exclusions_are_only_sent_when_requested(self):
        actor = Actor(1001, "alice")
        start = floor_to_slot(utc_now())
        booking = BookingRequest(actor, 1, 1800, start, "shared")
        edit = EditRequest(actor, "reservation-id")

        self.assertNotIn("excluded_gpus", _booking_request_payload(booking))
        self.assertNotIn("excluded_gpus", _edit_request_payload(edit))

        excluded_booking = BookingRequest(
            actor,
            1,
            1800,
            start,
            "shared",
            excluded_gpus=[1],
        )
        excluded_edit = EditRequest(
            actor,
            "reservation-id",
            excluded_gpus=[],
        )
        self.assertEqual(
            _booking_request_payload(excluded_booking)["excluded_gpus"], [1]
        )
        self.assertEqual(_edit_request_payload(excluded_edit)["excluded_gpus"], [])

    def setup_broker(self, root: Path, peer: dict) -> tuple[Config, BrokerServer]:
        data_dir = root / "data"
        data_dir.mkdir(mode=BROKER_DIR_MODE)
        data_dir.chmod(BROKER_DIR_MODE)
        socket_dir = root / "run"
        socket_dir.mkdir(mode=0o700)
        socket_dir.chmod(0o700)
        config = Config(
            data_dir,
            gpu_count=2,
            file_mode=BROKER_FILE_MODE,
            dir_mode=BROKER_DIR_MODE,
            broker_socket=socket_dir / "broker.sock",
            broker_uid=os.geteuid(),
            broker_socket_mode=BROKER_ALL_SOCKET_MODE,
        )
        advice = SimpleNamespace(
            order=[0, 1],
            scores={0: 0.0, 1: 0.0},
            memory_capacities_mb={0: 24_000, 1: 24_000},
        )
        server = BrokerServer(
            config,
            store=LedgerStore(
                data_dir,
                file_mode=BROKER_FILE_MODE,
                dir_mode=BROKER_DIR_MODE,
            ),
            credential_resolver=lambda connection: (
                os.getpid(),
                peer["uid"],
                peer.get("gid", os.getgid()),
            ),
            advice_provider=lambda value: advice,
            require_root_config=False,
        )
        return config, server

    def test_kernel_peer_uid_replaces_spoofed_actor_for_all_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = {"uid": 1001}
            config, server = self.setup_broker(Path(tmp), peer)
            store = BrokerLedgerStore(config)
            start = floor_to_slot(utc_now(), config.slot_minutes) + timedelta(
                minutes=config.slot_minutes
            )

            with RunningBroker(server, config.broker_socket):
                created = add_booking(
                    store,
                    config,
                    BookingRequest(
                        actor=Actor(9999, "spoofed"),
                        count=1,
                        duration_seconds=300,
                        start_at=start,
                        allow_queue=False,
                    ),
                )
                self.assertEqual(created.reservation["uid"], 1001)
                self.assertNotEqual(created.reservation["username"], "spoofed")

                edited = edit_booking(
                    store,
                    config,
                    EditRequest(
                        actor=Actor(9999, "spoofed"),
                        reservation_id=created.reservation["id"],
                        duration_seconds=600,
                    ),
                )
                self.assertEqual(edited.reservation["uid"], 1001)

                peer["uid"] = 1002
                with self.assertRaisesRegex(BookingError, "belongs to another UID"):
                    cancel_booking(
                        store,
                        created.reservation["id"],
                        Actor(1001, "forged-owner"),
                    )

                peer["uid"] = 1001
                cancelled = cancel_booking(
                    store,
                    created.reservation["id"],
                    Actor(0, "root-spoof"),
                    "broker-cancel-1",
                )
                replayed = cancel_booking(
                    store,
                    created.reservation["id"],
                    Actor(0, "root-spoof"),
                    "broker-cancel-1",
                )
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertEqual(replayed["id"], cancelled["id"])

            metadata = config.data_dir.lstat()
            self.assertEqual(stat.S_IMODE(metadata.st_mode), BROKER_DIR_MODE)
            self.assertEqual(metadata.st_mode & 0o022, 0)
            ledger = config.data_dir / "ledger.json"
            self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), BROKER_FILE_MODE)
            self.assertEqual(ledger.stat().st_mode & 0o022, 0)

    def test_broker_rejects_unknown_mutation_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = {"uid": 1001}
            config, server = self.setup_broker(Path(tmp), peer)
            with RunningBroker(server, config.broker_socket):
                with self.assertRaisesRegex(BookingError, "unknown field"):
                    BrokerClient(config).call(
                        "booking.cancel",
                        {"reservation_id": "missing", "uid": 0},
                    )

    def test_broker_authenticates_administrator_cancellation_from_peer_uid(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = {"uid": 1001}
            config, server = self.setup_broker(Path(tmp), peer)
            store = BrokerLedgerStore(config)
            start = floor_to_slot(utc_now(), config.slot_minutes) + timedelta(
                minutes=config.slot_minutes
            )
            with RunningBroker(server, config.broker_socket):
                created = add_booking(
                    store,
                    config,
                    BookingRequest(
                        actor=Actor(9999, "spoofed"),
                        count=1,
                        duration_seconds=300,
                        start_at=start,
                    ),
                )
                peer["uid"] = 1002
                with self.assertRaisesRegex(BookingError, "requires sudo"):
                    cancel_booking_as_admin(
                        store,
                        config,
                        created.reservation["id"],
                        Actor(0, "forged-root"),
                        "maintenance",
                    )
                peer["uid"] = 0
                cancelled = cancel_booking_as_admin(
                    store,
                    config,
                    created.reservation["id"],
                    Actor(1002, "forged-user"),
                    "cooling maintenance",
                )

            self.assertEqual(cancelled["cancel_operation"]["kind"], "administrator")
            self.assertEqual(cancelled["notifications"][0]["actor_uid"], 0)

    def test_worker_transaction_can_update_only_its_own_job_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = {"uid": 1001}
            config, server = self.setup_broker(Path(tmp), peer)
            store = BrokerLedgerStore(config)
            start = floor_to_slot(utc_now(), config.slot_minutes) + timedelta(
                minutes=config.slot_minutes
            )
            with RunningBroker(server, config.broker_socket):
                created = add_booking(
                    store,
                    config,
                    BookingRequest(
                        actor=Actor(9999, "spoofed"),
                        count=1,
                        duration_seconds=600,
                        start_at=start,
                        allow_queue=False,
                        job_spec_id=str(uuid.uuid4()),
                        job_digest="a" * 64,
                        job_summary="python train.py",
                    ),
                )
                reservation_id = created.reservation["id"]

                def mark_running(ledger):
                    reservation = next(
                        item
                        for item in ledger["reservations"]
                        if item["id"] == reservation_id
                    )
                    reservation["job"]["status"] = "running"
                    return (
                        ledger,
                        "updated",
                        [
                            {
                                "action": "job-start",
                                "reservation_id": reservation_id,
                                "uid": 0,
                                "username": "forged",
                                "message": "started",
                            }
                        ],
                        True,
                    )

                self.assertEqual(store.transaction(mark_running), "updated")
                persisted = store.load()
                self.assertEqual(
                    persisted["reservations"][0]["job"]["status"], "running"
                )

                def rewrite_gpu(ledger):
                    ledger["reservations"][0]["gpus"] = [1]
                    return ledger, None, [], True

                with self.assertRaisesRegex(
                    BookingError, "cannot modify reservation field gpus"
                ):
                    store.transaction(rewrite_gpu)

                def inject_job_command(ledger):
                    ledger["reservations"][0]["job"]["argv"] = ["unsafe"]
                    return ledger, None, [], True

                with self.assertRaisesRegex(BookingError, "unknown job field argv"):
                    store.transaction(inject_job_command)

                peer["uid"] = 1002

                def rewrite_other_job(ledger):
                    ledger["reservations"][0]["job"]["status"] = "succeeded"
                    return ledger, None, [], True

                with self.assertRaisesRegex(BookingError, "another UID"):
                    store.transaction(rewrite_other_job)

    def test_worker_patch_handles_a_ledger_larger_than_the_frame_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = {"uid": 1001}
            config, server = self.setup_broker(Path(tmp), peer)
            start = floor_to_slot(utc_now(), config.slot_minutes) + timedelta(
                minutes=config.slot_minutes
            )
            end = start + timedelta(minutes=config.slot_minutes)
            records = []
            for index in range(3500):
                record = {
                    "id": str(uuid.uuid4()),
                    "uid": 1001,
                    "username": "large-ledger-user-" + "x" * 180,
                    "gpus": [index % config.gpu_count],
                    "mode": "shared",
                    "start_at": to_iso(start),
                    "end_at": to_iso(end),
                    "status": "cancelled",
                    "created_at": to_iso(start),
                    "updated_at": to_iso(start),
                }
                records.append(record)
            target = records[0]
            target["status"] = "active"
            target["job"] = {
                "spec_id": str(uuid.uuid4()),
                "digest": "a" * 64,
                "summary": "python train.py",
                "submitted_at": to_iso(start),
                "status": "pending",
            }
            server.store.transaction(
                lambda ledger: (
                    {"version": 1, "reservations": records},
                    None,
                    [],
                    True,
                )
            )
            self.assertGreater(
                (config.data_dir / "ledger.json").stat().st_size,
                BROKER_MAX_FRAME_BYTES,
            )

            store = BrokerLedgerStore(config)

            def mark_running(ledger):
                ledger["reservations"][0]["job"]["status"] = "running"
                return ledger, "updated", [], True

            with RunningBroker(server, config.broker_socket):
                self.assertEqual(store.transaction(mark_running), "updated")

            persisted = server.store.load()
            self.assertEqual(
                persisted["reservations"][0]["job"]["status"],
                "running",
            )

    def test_new_client_falls_back_to_the_legacy_job_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = {"uid": 1001}
            config, server = self.setup_broker(Path(tmp), peer)
            start = floor_to_slot(utc_now(), config.slot_minutes)
            reservation = {
                "id": str(uuid.uuid4()),
                "uid": 1001,
                "username": "alice",
                "gpus": [0],
                "mode": "shared",
                "start_at": to_iso(start),
                "end_at": to_iso(start + timedelta(minutes=5)),
                "status": "active",
                "job": {"status": "pending"},
            }
            server.store.transaction(
                lambda ledger: (
                    {"version": 1, "reservations": [reservation]},
                    None,
                    [],
                    True,
                )
            )
            store = BrokerLedgerStore(config)
            calls = []

            def call(operation, payload):
                calls.append(operation)
                if operation == BROKER_JOB_PATCH_OPERATION:
                    raise BookingError(
                        f"unsupported broker operation: {BROKER_JOB_PATCH_OPERATION}"
                    )
                if operation == "ledger.snapshot":
                    ledger = server.store.load()
                    return {"ledger": ledger, "digest": _ledger_digest(ledger)}
                if operation == "ledger.commit-own-job":
                    return {"committed": True}
                raise AssertionError(operation)

            store._broker.call = call

            def mark_running(ledger):
                ledger["reservations"][0]["job"]["status"] = "running"
                return ledger, "updated", [], True

            self.assertEqual(store.transaction(mark_running), "updated")
            self.assertEqual(
                calls,
                [
                    BROKER_JOB_PATCH_OPERATION,
                    "ledger.snapshot",
                    "ledger.commit-own-job",
                ],
            )

    def test_client_rejects_non_socket_broker_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            socket_path = root / "not-a-socket"
            socket_path.write_text("fake", encoding="utf-8")
            config = Config(
                data_dir,
                gpu_count=1,
                file_mode=BROKER_FILE_MODE,
                dir_mode=BROKER_DIR_MODE,
                broker_socket=socket_path,
                broker_uid=os.geteuid(),
                broker_socket_mode=BROKER_ALL_SOCKET_MODE,
            )

            with self.assertRaisesRegex(BookingError, "not a Unix socket"):
                BrokerClient(config).call("ping", {})

    def test_broker_storage_policy_cannot_be_group_or_other_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "broker storage must use"):
                Config(
                    root / "data",
                    file_mode=0o666,
                    dir_mode=0o777,
                    broker_socket=root / "broker.sock",
                    broker_uid=os.geteuid(),
                    broker_socket_mode=BROKER_ALL_SOCKET_MODE,
                )

    def test_client_request_timeout_is_bounded_by_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = {"uid": 1001}
            config, server = self.setup_broker(Path(tmp), peer)
            config = Config(
                **{
                    **config.__dict__,
                    "lock_timeout_seconds": 0.5,
                }
            )
            with RunningBroker(server, config.broker_socket):
                started = time.monotonic()
                result = BrokerClient(config).call("ping", {})
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(result["actor_uid"], 1001)


if __name__ == "__main__":
    unittest.main()
