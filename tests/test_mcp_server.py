from __future__ import annotations

import os
import asyncio
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
        self.assertEqual(config["env"]["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(config["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-4.6")
        self.assertEqual(config["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-code-pro")

    def test_claude_desktop_server_config_omits_local_dev_token_for_hosted_gateway(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = mcp_server.claude_desktop_server_config(
                repo_root=tmpdir,
                gateway_url=mcp_server.HOSTED_GATEWAY_BASE_URL,
                token="local-dev-token",
                python_executable="/bin/python3",
            )

        self.assertNotIn("MCP_GATEWAY_TOKEN", config["env"])

    def test_gateway_token_prefers_customer_api_key_and_ignores_auth_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MCP_GATEWAY_TOKEN": "",
                "GATEWAY_API_KEY": "",
                "CLAUDE_CUSTOMER_API_KEY": "",
                "GATEWAY_API_KEYS": "admin-token",
                "ANTHROPIC_AUTH_TOKEN": "oauth-token",
                "ANTHROPIC_API_KEY": "customer-token",
            },
            clear=False,
        ):
            self.assertEqual(mcp_server.gateway_token(), "customer-token")

    def test_gateway_token_uses_explicit_mcp_token_first(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MCP_GATEWAY_TOKEN": "customer-token",
                "GATEWAY_API_KEY": "other-token",
                "CLAUDE_CUSTOMER_API_KEY": "customer-env-token",
                "ANTHROPIC_API_KEY": "anthropic-token",
            },
            clear=False,
        ):
            self.assertEqual(mcp_server.gateway_token(), "customer-token")

    def test_gateway_token_uses_claude_customer_api_key_before_anthropic_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MCP_GATEWAY_TOKEN": "",
                "GATEWAY_API_KEY": "",
                "CLAUDE_CUSTOMER_API_KEY": "customer-env-token",
                "ANTHROPIC_API_KEY": "anthropic-token",
            },
            clear=False,
        ):
            self.assertEqual(mcp_server.gateway_token(), "customer-env-token")

    def test_claude_desktop_server_config_does_not_use_admin_gateway_api_keys(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "GATEWAY_API_KEYS": "admin-token",
                    "MCP_GATEWAY_TOKEN": "",
                    "GATEWAY_API_KEY": "",
                    "CLAUDE_CUSTOMER_API_KEY": "",
                },
                clear=False,
            ):
                config = mcp_server.claude_desktop_server_config(
                    repo_root=tmpdir,
                    gateway_url="https://example.test",
                    token=None,
                    python_executable="/bin/python3",
                )

        self.assertNotIn("MCP_GATEWAY_TOKEN", config["env"])

    def test_claude_desktop_server_config_uses_claude_customer_api_key(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "MCP_GATEWAY_TOKEN": "",
                    "GATEWAY_API_KEY": "",
                    "CLAUDE_CUSTOMER_API_KEY": "customer-env-token",
                },
                clear=False,
            ):
                config = mcp_server.claude_desktop_server_config(
                    repo_root=tmpdir,
                    gateway_url="https://example.test",
                    token=None,
                    python_executable="/bin/python3",
                )

        self.assertEqual(config["env"]["MCP_GATEWAY_TOKEN"], "customer-env-token")
        self.assertEqual(config["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
        self.assertEqual(config["env"]["CLAUDE_CODE_MAX_RETRIES"], "2")
        self.assertEqual(config["env"]["API_TIMEOUT_MS"], "60000")

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
        self.assertIn("180 palavras", prompt)
        self.assertIn("sem saudacao generica", prompt)
        self.assertIn("Se houver Project context", prompt)
        self.assertIn("Se nenhum conteudo da conversa foi enviado", prompt)
        self.assertIn("Frontend lives in frontier/client.js.", prompt)
        self.assertIn("Fix the billing button.", prompt)

    def test_build_cowork_prompt_preserves_large_conversation_context(self) -> None:
        large_context = "inicio " + ("x" * 20_000) + " fim"
        prompt = mcp_server.build_cowork_prompt(
            task="Analise a conversa.",
            project_context=large_context,
        )

        self.assertGreater(len(prompt), 20_000)
        self.assertIn("inicio", prompt)
        self.assertIn("fim", prompt)
        self.assertIn("x" * 20_000, prompt)
        self.assertNotIn("trecho central omitido", prompt)

    def test_cowork_gateway_replaces_generic_greeting_with_clear_conversation_message(self) -> None:
        captured = {}

        async def fake_ask_gateway(**_kwargs):
            captured.update(_kwargs)
            return {
                "status_code": 200,
                "response": {
                    "content": [{"type": "text", "text": "Hello! How can I assist you today?"}],
                },
            }

        with patch.object(mcp_server, "ask_gateway", fake_ask_gateway):
            result = asyncio.run(
                mcp_server.cowork_gateway(
                    task="Leia o conteudo da conversa e resuma.",
                    project_context="",
                )
            )

        text = result["response"]["content"][0]["text"]
        self.assertIn("Nao recebi o conteudo da conversa", text)
        self.assertEqual(captured["max_tokens"], mcp_server.COWORK_MAX_OUTPUT_TOKENS)
        self.assertEqual(captured["timeout_seconds"], mcp_server.COWORK_TIMEOUT_SECONDS)
        self.assertEqual(captured["temperature"], 0.2)

    def test_cowork_gateway_replaces_portuguese_generic_help_prompt(self) -> None:
        async def fake_ask_gateway(**_kwargs):
            return {
                "status_code": 200,
                "response": {
                    "content": [{"type": "text", "text": "Claro, como posso ajudar voce hoje?"}],
                },
            }

        with patch.object(mcp_server, "ask_gateway", fake_ask_gateway):
            result = asyncio.run(
                mcp_server.cowork_gateway(
                    task="Leia o conteudo da conversa e resuma.",
                    project_context="",
                )
            )

        text = result["response"]["content"][0]["text"]
        self.assertIn("Nao recebi o conteudo da conversa", text)

    def test_cowork_gateway_rejects_empty_task_and_context(self) -> None:
        result = asyncio.run(mcp_server.cowork_gateway(task="", project_context=""))

        self.assertEqual(result["status_code"], 400)
        self.assertIn("nao recebeu tarefa", result["response"]["detail"])


if __name__ == "__main__":
    unittest.main()
