from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from claude_gateway import mcp_server


class McpServerHelpersTestCase(unittest.TestCase):
    def test_resolve_workspace_path_blocks_parent_escape(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"MCP_WORKSPACE_ROOT": tmpdir}, clear=False):
                with self.assertRaises(ValueError):
                    mcp_server.resolve_workspace_path("../outside.txt")

    def test_read_and_write_project_file_stay_inside_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {"MCP_WORKSPACE_ROOT": tmpdir, "MCP_ENABLE_WRITE_TOOLS": "true"},
                clear=False,
            ):
                written = mcp_server.write_project_file("src/example.txt", "hello")
                self.assertEqual(written["path"], "src/example.txt")

                read = mcp_server.read_project_file("src/example.txt")
                self.assertEqual(read["content"], "hello")
                self.assertFalse(read["truncated"])

    def test_run_allowed_command_rejects_unlisted_command(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "MCP_WORKSPACE_ROOT": tmpdir,
                    "MCP_ALLOWED_COMMANDS": "python -m pytest -q",
                    "MCP_ENABLE_COMMANDS": "true",
                },
                clear=False,
            ):
                with self.assertRaises(ValueError):
                    mcp_server.run_allowed_command("python -c 'print(1)'")

    def test_claude_desktop_server_config_targets_hosted_gateway_over_stdio(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = mcp_server.claude_desktop_server_config(
                repo_root=tmpdir,
                gateway_url="https://example.test",
                token="sk-test",
                python_executable="/bin/python3",
            )

        self.assertEqual(config["command"], "/bin/python3")
        self.assertEqual(config["args"], ["-m", "claude_gateway.mcp_server"])
        self.assertEqual(config["env"]["MCP_TRANSPORT"], "stdio")
        self.assertEqual(config["env"]["MCP_GATEWAY_BASE_URL"], "https://example.test")
        self.assertEqual(config["env"]["MCP_GATEWAY_TOKEN"], "sk-test")

    def test_claude_desktop_server_config_omits_local_dev_token_for_hosted_gateway(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = mcp_server.claude_desktop_server_config(
                repo_root=tmpdir,
                gateway_url=mcp_server.HOSTED_GATEWAY_BASE_URL,
                token="local-dev-token",
                python_executable="/bin/python3",
            )

        self.assertNotIn("MCP_GATEWAY_TOKEN", config["env"])

    def test_claude_desktop_server_config_does_not_use_admin_gateway_api_keys(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {"GATEWAY_API_KEYS": "admin-token", "MCP_GATEWAY_TOKEN": "", "GATEWAY_API_KEY": ""},
                clear=False,
            ):
                config = mcp_server.claude_desktop_server_config(
                    repo_root=tmpdir,
                    gateway_url="https://example.test",
                    token=None,
                    python_executable="/bin/python3",
                )

        self.assertNotIn("MCP_GATEWAY_TOKEN", config["env"])

    def test_merge_claude_desktop_config_preserves_existing_preferences(self) -> None:
        merged = mcp_server.merge_claude_desktop_config(
            {"preferences": {"theme": "dark"}, "mcpServers": {"old": {"command": "old"}}},
            "claude-code-api",
            {"command": "python3"},
        )

        self.assertEqual(merged["preferences"]["theme"], "dark")
        self.assertIn("old", merged["mcpServers"])
        self.assertEqual(merged["mcpServers"]["claude-code-api"]["command"], "python3")

    def test_build_cowork_prompt_includes_mode_context_and_task(self) -> None:
        prompt = mcp_server.build_cowork_prompt(
            task="Fix the billing button.",
            project_context="Frontend lives in frontier/client.js.",
            mode="debug",
        )

        self.assertIn("debugging partner", prompt)
        self.assertIn("fast mode", prompt)
        self.assertIn("do not use hidden thinking", prompt)
        self.assertIn("Frontend lives in frontier/client.js.", prompt)
        self.assertIn("Fix the billing button.", prompt)


if __name__ == "__main__":
    unittest.main()
