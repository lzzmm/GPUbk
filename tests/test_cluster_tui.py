import unittest
from pathlib import Path

from bk.cluster import ClusterConfig, ClusterNode, NodeReply
from bk.cluster_tui import (
    FOCUS_RESERVATIONS,
    _reservation_detail_lines,
    render_cluster_lines,
)


class ClusterTuiTests(unittest.TestCase):
    def test_render_selects_one_node_and_keeps_lines_bounded(self):
        first = ClusterNode("gpu-a", "a" * 20, "local", None, "/usr/bin/bk", 0, 8)
        second = ClusterNode("gpu-b", "b" * 20, "ssh", "gpu-b", "/usr/bin/bk", 10, 8)
        config = ClusterConfig(Path("/cluster.json"), (first, second))
        payload = {
            "actor": {"uid": 1003, "username": "user"},
            "policy": {
                "gpu_count": 1,
                "monitoring": {"collector": {"state": "running"}},
            },
            "gpu_advice": {
                "gpus": [
                    {
                        "index": 0,
                        "live": {"status": "idle", "utilization_percent": 2},
                        "memory": {"free_mb": 24576},
                        "history": {"predicted_percent": 4},
                    }
                ]
            },
            "reservations": [
                {
                    "short_id": "123456",
                    "username": "user",
                    "mode": "shared",
                    "gpus": [0],
                    "start_at": "2030-01-01T00:00:00Z",
                    "end_at": "2030-01-01T01:00:00Z",
                    "mine": True,
                }
            ],
        }
        lines = render_cluster_lines(
            config,
            [NodeReply(first, payload, None), NodeReply(second, None, "timeout")],
            0,
            80,
            24,
        )
        self.assertEqual(len(lines), 24)
        self.assertTrue(all(len(line) <= 80 for line in lines))
        self.assertTrue(any(line.startswith(">gpu-a") for line in lines))
        self.assertTrue(any("123456" in line for line in lines))
        self.assertTrue(any("24.0GiB" in line for line in lines))

    def test_render_marks_disabled_node_without_treating_it_as_offline(self):
        node = ClusterNode(
            "gpu-maint",
            "a" * 20,
            "ssh",
            "gpu-maint",
            "/usr/bin/bk",
            0,
            8,
            False,
        )
        config = ClusterConfig(Path("/cluster.json"), (node,))
        lines = render_cluster_lines(
            config,
            [NodeReply(node, None, "disabled by administrator", error_code="disabled")],
            0,
            100,
            12,
        )
        self.assertTrue(any("disabled" in line for line in lines))
        self.assertTrue(any("routing is paused" in line for line in lines))
        self.assertFalse(any("Unavailable:" in line for line in lines))

    def test_render_survives_malformed_optional_context_fields(self):
        node = ClusterNode("gpu-a", "a" * 20, "local", None, "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        payload = {
            "generated_at": "2030-01-01T00:00:00Z",
            "software": [],
            "policy": [],
            "gpu_advice": {"gpus": [None, {"index": "?", "live": []}]},
            "reservations": [None, {"gpus": None}],
            "actor": [],
        }
        lines = render_cluster_lines(
            config,
            [NodeReply(node, payload, None)],
            0,
            100,
            14,
        )
        self.assertEqual(len(lines), 14)
        self.assertTrue(any("gpu-a" in line for line in lines))

    def test_reservation_focus_shows_capacity_memory_and_selected_marker(self):
        node = ClusterNode("gpu-a", "a" * 20, "local", None, "/usr/bin/bk", 0, 8)
        config = ClusterConfig(Path("/cluster.json"), (node,))
        reservation = {
            "id": "12345678-1234-1234-1234-123456789abc",
            "short_id": "12345678",
            "uid": 1003,
            "username": "user",
            "mine": True,
            "mode": "shared",
            "share_units_per_gpu": 3,
            "share_capacity_units_per_gpu": 4,
            "expected_memory_mb_per_gpu": 12288,
            "gpus": [0],
            "start_at": "2030-01-01T00:00:00Z",
            "end_at": "2030-01-01T01:00:00Z",
        }
        payload = {
            "generated_at": "2030-01-01T00:00:00Z",
            "policy": {"gpu_count": 1},
            "reservations": [reservation],
        }
        lines = render_cluster_lines(
            config,
            [NodeReply(node, payload, None)],
            0,
            100,
            16,
            focus=FOCUS_RESERVATIONS,
            selected_reservation=0,
        )
        selected = next(line for line in lines if line.startswith("> "))
        self.assertIn("3/4", selected)
        self.assertIn("12G", selected)
        details = _reservation_detail_lines(node, reservation)
        self.assertIn("Edit:   bk c e gpu-a/12345678 -d 1h", details)
        self.assertIn("Cancel: bk c d gpu-a/12345678", details)

    def test_node_view_pages_to_keep_the_selected_node_visible(self):
        nodes = tuple(
            ClusterNode(
                f"gpu-{index}",
                f"{index:020x}",
                "ssh",
                f"gpu-{index}",
                "/usr/bin/bk",
                index,
                8,
            )
            for index in range(10)
        )
        lines = render_cluster_lines(
            ClusterConfig(Path("/cluster.json"), nodes),
            [],
            9,
            80,
            12,
        )
        self.assertTrue(any(line.startswith(">gpu-9") for line in lines))
        self.assertTrue(any("7-10/10" in line for line in lines))
        self.assertFalse(any(line.startswith(" gpu-0") for line in lines))


if __name__ == "__main__":
    unittest.main()
