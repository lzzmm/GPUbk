import tempfile
import unittest
from pathlib import Path

from bk.config import Config
from bk.mcp_server import BkMcpBackend, create_mcp_server
from bk.storage import LedgerStore

try:
    from mcp.shared.memory import create_connected_server_and_client_session
    from pydantic import AnyUrl

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


@unittest.skipUnless(MCP_AVAILABLE, "install the mcp extra to run protocol integration tests")
class McpProtocolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_client_lists_and_calls_structured_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = Config(
                data_dir=data_dir,
                gpu_count=2,
                job_log_dir=Path(tmp) / "jobs",
            )
            app = create_mcp_server(BkMcpBackend(config, LedgerStore(data_dir)))

            async with create_connected_server_and_client_session(app, raise_exceptions=True) as session:
                tools = await session.list_tools()
                by_name = {item.name: item for item in tools.tools}
                names = set(by_name)
                resources = await session.list_resources()
                context_resource = await session.read_resource(AnyUrl("bk://context"))
                prompts = await session.list_prompts()
                prompt = await session.get_prompt(
                    "plan_gpu_experiment",
                    {"count": "1", "duration": "30m", "expected_memory": "8g"},
                )
                recommendation = await session.call_tool(
                    "recommend_gpu_booking",
                    {"count": 1, "duration": "30m", "mode": "shared"},
                )
                created = await session.call_tool(
                    "create_gpu_booking",
                    {
                        "count": 1,
                        "duration": "30m",
                        "mode": "shared",
                        "expected_memory": "8g",
                        "operation_id": "mcp-protocol-test-1",
                    },
                )
                retried = await session.call_tool(
                    "create_gpu_booking",
                    {
                        "count": 1,
                        "duration": "30m",
                        "mode": "shared",
                        "expected_memory": "8g",
                        "operation_id": "mcp-protocol-test-1",
                    },
                )
                edited = await session.call_tool(
                    "edit_my_gpu_booking",
                    {
                        "reservation_id": created.structuredContent["reservation"]["short_id"],
                        "duration": "35m",
                        "operation_id": "mcp-protocol-edit-1",
                    },
                )
                edit_retried = await session.call_tool(
                    "edit_my_gpu_booking",
                    {
                        "reservation_id": created.structuredContent["reservation"]["short_id"],
                        "duration": "35m",
                        "operation_id": "mcp-protocol-edit-1",
                    },
                )
                cancelled = await session.call_tool(
                    "cancel_my_gpu_booking",
                    {"reservation_id": created.structuredContent["reservation"]["short_id"]},
                )

            self.assertEqual(
                names,
                {
                    "get_gpu_context",
                    "recommend_gpu_booking",
                    "create_gpu_booking",
                    "list_gpu_reservations",
                    "edit_my_gpu_booking",
                    "cancel_my_gpu_booking",
                    "read_my_job_log",
                },
            )
            self.assertEqual(str(resources.resources[0].uri), "bk://context")
            self.assertTrue(by_name["get_gpu_context"].annotations.readOnlyHint)
            self.assertTrue(by_name["recommend_gpu_booking"].annotations.readOnlyHint)
            self.assertTrue(by_name["list_gpu_reservations"].annotations.readOnlyHint)
            self.assertTrue(by_name["read_my_job_log"].annotations.readOnlyHint)
            self.assertTrue(by_name["create_gpu_booking"].annotations.idempotentHint)
            self.assertFalse(by_name["create_gpu_booking"].annotations.destructiveHint)
            self.assertTrue(by_name["edit_my_gpu_booking"].annotations.idempotentHint)
            self.assertFalse(by_name["edit_my_gpu_booking"].annotations.destructiveHint)
            self.assertTrue(by_name["cancel_my_gpu_booking"].annotations.destructiveHint)
            self.assertFalse(by_name["cancel_my_gpu_booking"].annotations.idempotentHint)
            self.assertTrue(all(tool.annotations.openWorldHint is False for tool in by_name.values()))
            self.assertIn('"schema_version": "bk.agent.v1"', context_resource.contents[0].text)
            self.assertEqual(prompts.prompts[0].name, "plan_gpu_experiment")
            self.assertIn("recommend_gpu_booking", prompt.messages[0].content.text)
            self.assertTrue(recommendation.structuredContent["available"])
            self.assertEqual(recommendation.structuredContent["schema_version"], "bk.agent.v1")
            self.assertEqual(created.structuredContent["status"], "created")
            self.assertEqual(created.structuredContent["allocation"]["selected"][0]["gpu"], 0)
            self.assertEqual(retried.structuredContent["status"], "exists")
            self.assertEqual(
                created.structuredContent["reservation"]["id"],
                retried.structuredContent["reservation"]["id"],
            )
            self.assertEqual(edited.structuredContent["status"], "updated")
            self.assertEqual(edit_retried.structuredContent["status"], "exists")
            self.assertEqual(cancelled.structuredContent["reservation"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
