from __future__ import annotations

import io
import json
import os
import sys
import unittest

from tests.support import REPO, run

from lib.sessionkit_supervisor import adapter as adapter_module
from lib.sessionkit_supervisor import ratchet_module
from lib.sessionkit_supervisor.server import MCPServer, PROTOCOL_VERSION, TOOLS, TOOL_NAMES


class SupervisorMCPTests(unittest.TestCase):
    def test_server_initializes_and_lists_only_frozen_toolset(self) -> None:
        requests = "\n".join(
            json.dumps(value)
            for value in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
        ) + "\n"
        output = io.StringIO()
        self.assertEqual(0, MCPServer().run(io.StringIO(requests), output))
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual("session-kit-supervisor", responses[0]["result"]["serverInfo"]["name"])
        names = tuple(tool["name"] for tool in responses[1]["result"]["tools"])
        self.assertEqual(TOOL_NAMES, names)
        forbidden = {"spawn_workers", "message_workers", "close_workers", "adopt_worker", "prune_recovered_workers", "list_worktrees"}
        self.assertFalse(forbidden.intersection(names))

    def test_module_entry_point_really_runs_over_stdio(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.fspath(REPO / "lib")
        completed = run(
            [sys.executable, "-m", "sessionkit_supervisor"],
            env=env,
            input_text=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}) + "\n",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(list(TOOL_NAMES), [tool["name"] for tool in payload["result"]["tools"]])
        self.assertEqual("", completed.stderr)

    def test_malformed_request_does_not_stop_later_requests(self) -> None:
        source = io.StringIO("not-json\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
        output = io.StringIO()
        MCPServer().run(source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(-32700, responses[0]["error"]["code"])
        self.assertEqual({}, responses[1]["result"])

    def test_initialize_uses_supported_protocol_and_idless_requests_are_silent(self) -> None:
        source = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "99-BOGUS-VERSION"},
                }
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "tools/list"})
            + "\n"
        )
        output = io.StringIO()
        MCPServer().run(source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(1, len(responses))
        self.assertEqual(PROTOCOL_VERSION, responses[0]["result"]["protocolVersion"])

    def test_empty_error_string_is_not_flagged_as_a_tool_error(self) -> None:
        # A delivered send carries "error": "" (empty stderr). isError on key
        # presence made clients retry successful sends and burn budget claims.
        server = MCPServer(adapter=object())
        server.call_tool = lambda name, arguments: {"success": True, "error": ""}  # type: ignore[method-assign]
        request = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "send_message", "arguments": {}},
        }
        response = server.handle(request)
        assert response is not None
        self.assertNotIn("isError", response["result"])

        server.call_tool = lambda name, arguments: {"error": "sp msg failed"}  # type: ignore[method-assign]
        failed = server.handle(request)
        assert failed is not None
        self.assertTrue(failed["result"]["isError"])

    def test_tool_schemas_match_implemented_optional_inputs(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        self.assertNotIn("include_snapshot", schemas["worker_events"]["properties"])
        self.assertNotIn("include_snapshots", schemas["poll_worker_changes"]["properties"])
        self.assertEqual(
            2,
            schemas["wait_idle_workers"]["properties"]["poll_interval"]["minimum"],
        )
        self.assertNotIn(
            "default", schemas["send_message"]["properties"]["category"]
        )
        self.assertIn("category", schemas["send_message"]["required"])
        self.assertIn("authority_event_id", schemas["send_message"]["properties"])
        self.assertIn("authority_scope", schemas["send_message"]["properties"])

    def test_verify_source_event_tool_uses_only_the_exact_id(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self.calls = []

            def verify_source_event(self, event_id):
                self.calls.append(event_id)
                return {"verified": True, "event_id": event_id, "basis": "transcript"}

        adapter = Adapter()
        server = MCPServer(adapter=adapter)  # type: ignore[arg-type]
        event_id = "a" * 64
        result = server.call_tool("verify_source_event", {"event_id": event_id})
        self.assertEqual(event_id, result["event_id"])
        self.assertEqual([event_id], adapter.calls)

    def test_ratchet_is_not_imported_at_package_or_server_start(self) -> None:
        self.assertNotIn("sessionkit_supervisor.ratchet", sys.modules)

    def test_supervisor_cleanup_contracts_stay_explicit(self) -> None:
        self.assertIn("return", ratchet_module.__annotations__)
        self.assertFalse(hasattr(adapter_module, "MAX_LOG_LINES"))


if __name__ == "__main__":
    unittest.main()
