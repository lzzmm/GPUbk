import json
import subprocess
import sys
import unittest
from threading import Event
from unittest import mock

from bk import cluster_transport
from bk.cluster_transport import ClusterNode, invoke_node


class ClusterTransportTests(unittest.TestCase):
    def test_streaming_reader_stops_oversized_output_while_process_runs(self):
        with (
            mock.patch.object(cluster_transport, "MAX_NODE_OUTPUT_BYTES", 64),
            self.assertRaises(cluster_transport._NodeOutputTooLarge),
        ):
            cluster_transport._run_node_process(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"],
                None,
                2,
            )

    def test_streaming_reader_enforces_deadline(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            cluster_transport._run_node_process(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                None,
                0.05,
            )

    def test_streaming_reader_honors_cancellation(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(cluster_transport._NodeRequestCancelled):
            cluster_transport._run_node_process(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                None,
                2,
                cancel_event=cancelled,
            )

    def test_structured_remote_error_is_returned_after_identity_check(self):
        node = ClusterNode(
            "gpu-a",
            "a" * 20,
            "local",
            None,
            "/usr/local/bin/bk",
            0,
            8,
        )
        response = {
            "node": {"id": node.node_id},
            "kind": "error",
            "error": {"message": "capacity full"},
        }
        with mock.patch(
            "bk.cluster_transport._run_node_process",
            return_value=(2, json.dumps(response).encode(), b""),
        ):
            reply = invoke_node(node, ["agent", "context", "--compact"])
        self.assertEqual(reply.error, "capacity full")
        self.assertFalse(reply.timed_out)


if __name__ == "__main__":
    unittest.main()
