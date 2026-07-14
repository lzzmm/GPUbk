import json
import os
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.cluster import (
    ClusterConfig,
    ClusterNode,
    _invoke_idempotent_write,
    run_cluster_cli,
)
from bk.timeparse import parse_iso


NODE_RUNNER = """\
import os
import sys

import bk.cli
import bk.node_identity
import bk.service


def identity():
    return {
        "schema": 1,
        "id": os.environ["GPUBK_TEST_NODE_ID"],
        "hostname": os.environ["GPUBK_TEST_NODE_NAME"],
    }


bk.node_identity.stable_node_identity = identity
bk.service.stable_node_identity = identity
bk.cli.stable_node_identity = identity
raise SystemExit(bk.cli.main(sys.argv[1:]))
"""


class ClusterProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runner = self.root / "node_runner.py"
        self.runner.write_text(NODE_RUNNER, encoding="utf-8")
        self.first = ClusterNode(
            "gpu-a",
            "a" * 20,
            "ssh",
            "gpu-a",
            "/usr/local/bin/bk",
            0,
            5,
        )
        self.second = ClusterNode(
            "gpu-b",
            "b" * 20,
            "ssh",
            "gpu-b",
            "/usr/local/bin/bk",
            1,
            5,
        )
        self.config = ClusterConfig(
            self.root / "cluster.json",
            (self.first, self.second),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def node_command(self, node, argv):
        environment = dict(os.environ)
        environment.update(
            {
                "BK_CLUSTER_DISABLE": "1",
                "BK_DATA_DIR": str(self.root / node.name),
                "BK_GPU_COUNT": "1",
                "BK_MAX_SHARED_USERS": "2",
                "GPUBK_TEST_NODE_ID": node.node_id,
                "GPUBK_TEST_NODE_NAME": node.name,
            }
        )
        return [sys.executable, str(self.runner), *argv], environment

    def test_automatic_booking_uses_two_independent_node_ledgers(self):
        with (
            mock.patch("bk.cluster.load_cluster_config", return_value=self.config),
            mock.patch(
                "bk.cluster_transport.node_command",
                side_effect=self.node_command,
            ),
        ):
            first_output = StringIO()
            with redirect_stdout(first_output):
                self.assertEqual(
                    run_cluster_cli(["book", "1", "30m", "--mode", "x", "--json"]),
                    0,
                )
            second_output = StringIO()
            with redirect_stdout(second_output):
                self.assertEqual(
                    run_cluster_cli(["book", "1", "30m", "--mode", "x", "--json"]),
                    0,
                )

        first = json.loads(first_output.getvalue())
        second = json.loads(second_output.getvalue())
        self.assertEqual(first["node"]["name"], "gpu-a")
        self.assertEqual(second["node"]["name"], "gpu-b")
        for node in (self.first, self.second):
            ledger = json.loads(
                (self.root / node.name / "ledger.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(ledger["reservations"]), 1)

    def test_concurrent_exclusive_writes_serialize_without_overlap(self):
        def submit(_index):
            operation_id = str(uuid.uuid4())
            return _invoke_idempotent_write(
                self.first,
                ["x", "1", "30m", "--op-id", operation_id, "--json"],
                operation_id,
            )

        with mock.patch(
            "bk.cluster_transport.node_command",
            side_effect=self.node_command,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                replies = list(executor.map(submit, range(2)))

        self.assertTrue(all(reply.error is None for reply in replies))
        reservations = sorted(
            (reply.payload["reservation"] for reply in replies),
            key=lambda item: item["start_at"],
        )
        self.assertLessEqual(
            parse_iso(reservations[0]["end_at"]),
            parse_iso(reservations[1]["start_at"]),
        )
        ledger = json.loads(
            (self.root / self.first.name / "ledger.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(ledger["reservations"]), 2)


if __name__ == "__main__":
    unittest.main()
