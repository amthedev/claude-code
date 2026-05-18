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


if __name__ == "__main__":
    unittest.main()
