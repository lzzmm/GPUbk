import json
import subprocess
import sys
import time
import unittest
from threading import Event, Timer
from unittest import mock

from bk import cluster_transport
from bk.cluster_transport import ClusterNode, invoke_node, probe_ssh_node


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

    def test_cancelled_request_does_not_start_a_node_process(self):
        node = ClusterNode(
            "gpu-a",
            "a" * 20,
            "local",
            None,
            "/usr/local/bin/bk",
            0,
            8,
        )
        cancelled = Event()
        cancelled.set()
        with mock.patch("bk.cluster_transport._run_node_process") as run_process:
            reply = invoke_node(
                node,
                ["agent", "context", "--compact"],
                cancel_event=cancelled,
            )
        self.assertTrue(reply.cancelled)
        self.assertEqual(reply.error_code, "cancelled")
        run_process.assert_not_called()

    def test_cancellation_remains_responsive_after_child_closes_its_pipes(self):
        cancelled = Event()
        timer = Timer(0.05, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(cluster_transport._NodeRequestCancelled):
                cluster_transport._run_node_process(
                    [
                        sys.executable,
                        "-c",
                        "import os,time; os.close(1); os.close(2); time.sleep(2)",
                    ],
                    None,
                    2,
                    cancel_event=cancelled,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 0.75)

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

    def test_remote_errors_are_safe_and_bounded_at_the_transport_boundary(self):
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
            "error": {"message": "\x1b[31mcapacity\nfull " + "x" * 2000},
        }
        with mock.patch(
            "bk.cluster_transport._run_node_process",
            return_value=(2, json.dumps(response).encode(), b""),
        ):
            reply = invoke_node(node, ["agent", "context", "--compact"])
        self.assertNotIn("\x1b", reply.error)
        self.assertNotIn("\n", reply.error)
        self.assertLessEqual(len(reply.error), cluster_transport.MAX_NODE_ERROR_CHARS)
        self.assertTrue(reply.error.endswith("~"))

    def test_probe_discovers_and_returns_valid_stable_identity(self):
        node = ClusterNode(
            "gpu-b",
            "0" * 20,
            "ssh",
            "user@gpu-b",
            "/usr/local/bin/bk",
            0,
            8,
        )
        response = {
            "node": {"id": "b" * 20},
            "kind": "context",
        }
        with mock.patch(
            "bk.cluster_transport._run_node_process",
            return_value=(0, json.dumps(response).encode(), b""),
        ):
            reply = probe_ssh_node(node, ["agent", "context", "--compact"])
        self.assertIsNone(reply.error)
        self.assertEqual(reply.node.node_id, "b" * 20)

    def test_probe_rejects_malformed_stable_identity(self):
        node = ClusterNode(
            "gpu-b",
            "0" * 20,
            "ssh",
            "user@gpu-b",
            "/usr/local/bin/bk",
            0,
            8,
        )
        response = {"node": {"id": "not-stable"}, "kind": "context"}
        with mock.patch(
            "bk.cluster_transport._run_node_process",
            return_value=(0, json.dumps(response).encode(), b""),
        ):
            reply = probe_ssh_node(node, ["agent", "context", "--compact"])
        self.assertEqual(reply.error_code, "identity")
        self.assertIn("invalid stable node identity", reply.error)


if __name__ == "__main__":
    unittest.main()
