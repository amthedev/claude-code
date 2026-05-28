from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - exercised only when optional package is absent.
    FastMCP = None  # type: ignore[assignment]


DEFAULT_ALLOWED_COMMANDS = (
    ".venv/bin/python -m pytest -q",
    "python -m pytest -q",
    "pytest -q",
    "npm test",
    "npm run test",
)
HOSTED_GATEWAY_BASE_URL = os.getenv("HOSTED_GATEWAY_BASE_URL", "https://your-subdomain.squareweb.app")
LOCAL_DEV_TOKENS = {"", "local-dev-token"}


def workspace_root() -> Path:
    return Path(os.getenv("MCP_WORKSPACE_ROOT", os.getcwd())).resolve()


def allowed_commands() -> set[str]:
    raw = os.getenv("MCP_ALLOWED_COMMANDS")
    if not raw:
        return set(DEFAULT_ALLOWED_COMMANDS)
    return {command.strip() for command in raw.split(";") if command.strip()}


def gateway_base_url() -> str:
    return os.getenv("MCP_GATEWAY_BASE_URL", "http://127.0.0.1:8787").rstrip("/")


def gateway_token() -> str:
    return (
        os.getenv("MCP_GATEWAY_TOKEN")
        or os.getenv("GATEWAY_API_KEY")
        or _first_csv_value(os.getenv("GATEWAY_API_KEYS", ""))
        or os.getenv("ANTHROPIC_AUTH_TOKEN")
        or "local-dev-token"
    )


def _first_csv_value(value: str) -> str:
    return next((part.strip() for part in value.split(",") if part.strip()), "")


def claude_desktop_config_path() -> Path:
    override = os.getenv("CLAUDE_DESKTOP_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.getenv("APPDATA", str(Path.home()))) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def claude_desktop_server_config(
    *,
    repo_root: str | None = None,
    gateway_url: str | None = None,
    token: str | None = None,
    python_executable: str | None = None,
    enable_write_tools: bool = False,
    enable_commands: bool = False,
) -> dict[str, Any]:
    root = Path(repo_root or os.getcwd()).resolve()
    venv_python = root / ".venv/bin/python"
    command = python_executable or (str(venv_python) if venv_python.exists() else sys.executable)
    resolved_token = token
    if resolved_token is None:
        resolved_token = (
            os.getenv("MCP_GATEWAY_TOKEN")
            or os.getenv("GATEWAY_API_KEY")
        )
    resolved_url = (gateway_url or HOSTED_GATEWAY_BASE_URL).rstrip("/")
    if resolved_url == HOSTED_GATEWAY_BASE_URL and (resolved_token or "") in LOCAL_DEV_TOKENS:
        resolved_token = ""
    env = {
        "PYTHONPATH": str(root),
        "MCP_TRANSPORT": "stdio",
        "MCP_WORKSPACE_ROOT": str(root),
        "MCP_GATEWAY_BASE_URL": resolved_url,
        "MCP_ENABLE_WRITE_TOOLS": "true" if enable_write_tools else "false",
        "MCP_ENABLE_COMMANDS": "true" if enable_commands else "false",
    }
    if resolved_token:
        env["MCP_GATEWAY_TOKEN"] = resolved_token
    return {
        "command": command,
        "args": ["-m", "claude_gateway.mcp_server"],
        "env": env,
    }


def merge_claude_desktop_config(
    existing: dict[str, Any],
    server_name: str,
    server_config: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    servers = dict(merged.get("mcpServers") or {})
    servers[server_name] = server_config
    merged["mcpServers"] = servers
    return merged


def resolve_workspace_path(path: str | None = None) -> Path:
    root = workspace_root()
    candidate = root if not path else (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Path is outside MCP_WORKSPACE_ROOT.")
    return candidate


def list_project_files(pattern: str = "*", limit: int = 200) -> dict[str, Any]:
    root = workspace_root()
    safe_limit = max(1, min(limit, 1000))
    ignored_parts = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
    files: list[str] = []
    for item in root.rglob(pattern or "*"):
        if len(files) >= safe_limit:
            break
        if not item.is_file():
            continue
        relative = item.relative_to(root)
        if any(part in ignored_parts for part in relative.parts):
            continue
        files.append(str(relative))
    return {"root": str(root), "files": sorted(files), "truncated": len(files) >= safe_limit}


def read_project_file(path: str, max_chars: int = 20000) -> dict[str, Any]:
    target = resolve_workspace_path(path)
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    safe_limit = max(1, min(max_chars, 200000))
    text = target.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(target.relative_to(workspace_root())),
        "content": text[:safe_limit],
        "truncated": len(text) > safe_limit,
    }


def write_project_file(path: str, content: str) -> dict[str, Any]:
    if os.getenv("MCP_ENABLE_WRITE_TOOLS", "false").strip().lower() not in {"1", "true", "yes"}:
        raise PermissionError("MCP write tools are disabled. Set MCP_ENABLE_WRITE_TOOLS=true.")
    target = resolve_workspace_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target.relative_to(workspace_root())), "bytes": len(content.encode("utf-8"))}


def project_summary() -> dict[str, Any]:
    root = workspace_root()
    files = list_project_files(limit=500)["files"]
    interesting = [
        path
        for path in files
        if path in {"README.md", "pyproject.toml", "requirements.txt"}
        or path.startswith(("claude_gateway/", "frontier/", "tests/"))
    ]
    return {
        "root": str(root),
        "file_count_sample": len(files),
        "interesting_files": interesting[:120],
        "gateway": {
            "base_url": gateway_base_url(),
            "token_configured": bool(gateway_token()),
        },
        "allowed_test_commands": sorted(allowed_commands()),
    }


def run_allowed_command(command: str, timeout_seconds: int = 120) -> dict[str, Any]:
    if os.getenv("MCP_ENABLE_COMMANDS", "false").strip().lower() not in {"1", "true", "yes"}:
        raise PermissionError("MCP command tools are disabled. Set MCP_ENABLE_COMMANDS=true.")
    normalized = " ".join(shlex.split(command))
    if normalized not in allowed_commands():
        raise ValueError(
            "Command is not allowed. Configure MCP_ALLOWED_COMMANDS with semicolon-separated "
            "exact commands if you need more."
        )

    completed = subprocess.run(
        shlex.split(normalized),
        cwd=workspace_root(),
        text=True,
        capture_output=True,
        timeout=max(1, min(timeout_seconds, 600)),
        check=False,
    )
    return {
        "command": normalized,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20000:],
        "stderr": completed.stderr[-20000:],
    }


def apply_unified_patch(patch: str) -> dict[str, Any]:
    if os.getenv("MCP_ENABLE_WRITE_TOOLS", "false").strip().lower() not in {"1", "true", "yes"}:
        raise PermissionError("MCP patch tools are disabled. Set MCP_ENABLE_WRITE_TOOLS=true.")
    if not patch.strip():
        raise ValueError("Patch is empty.")

    root = workspace_root()
    check = subprocess.run(
        ["git", "apply", "--check", "-"],
        input=patch,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        return {
            "applied": False,
            "check_returncode": check.returncode,
            "stdout": check.stdout,
            "stderr": check.stderr,
        }

    applied = subprocess.run(
        ["git", "apply", "-"],
        input=patch,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "applied": applied.returncode == 0,
        "returncode": applied.returncode,
        "stdout": applied.stdout,
        "stderr": applied.stderr,
    }


async def ask_gateway(
    prompt: str,
    model: str = "claude-code-pro",
    max_tokens: int = 1200,
) -> dict[str, Any]:
    body = {
        "model": model,
        "max_tokens": max(1, min(max_tokens, 4096)),
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Authorization": f"Bearer {gateway_token()}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{gateway_base_url()}/v1/messages", headers=headers, json=body)
    try:
        data: Any = response.json()
    except json.JSONDecodeError:
        data = {"text": response.text}
    if response.status_code == 403:
        data = {
            **(data if isinstance(data, dict) else {"response": data}),
            "hint": (
                "Use a customer/API token generated in the Admin screen. "
                "Admin tokens from GATEWAY_API_KEYS cannot call model endpoints."
            ),
        }
    return {"status_code": response.status_code, "response": data}


def build_cowork_prompt(
    task: str,
    project_context: str = "",
    mode: str = "pair_programming",
) -> str:
    mode_label = {
        "pair_programming": "pair-programming partner",
        "review": "senior code reviewer",
        "debug": "debugging partner",
        "plan": "technical planning partner",
    }.get(mode, "pair-programming partner")
    context = project_context.strip()
    parts = [
        f"Act as a {mode_label} inside a coworking session.",
        "Use practical engineering judgment, be concise, and give concrete next steps.",
        "When code changes are needed, mention exact files and patches conceptually.",
    ]
    if context:
        parts.append(f"Project context:\n{context}")
    parts.append(f"Task:\n{task.strip()}")
    return "\n\n".join(parts)


async def cowork_gateway(
    task: str,
    project_context: str = "",
    mode: str = "pair_programming",
    model: str = "claude-code-pro",
    max_tokens: int = 1600,
) -> dict[str, Any]:
    return await ask_gateway(
        prompt=build_cowork_prompt(task=task, project_context=project_context, mode=mode),
        model=model,
        max_tokens=max_tokens,
    )


async def gateway_models() -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {gateway_token()}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{gateway_base_url()}/v1/models", headers=headers)
    try:
        data: Any = response.json()
    except json.JSONDecodeError:
        data = {"text": response.text}
    return {"status_code": response.status_code, "response": data}


async def gateway_health() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{gateway_base_url()}/health")
    try:
        data: Any = response.json()
    except json.JSONDecodeError:
        data = {"text": response.text}
    return {"status_code": response.status_code, "response": data}


def build_mcp_server() -> Any:
    if FastMCP is None:
        raise RuntimeError('Install the MCP SDK first: pip install "mcp[cli]"')

    mcp = FastMCP(
        "Claude Coding Tools",
        json_response=True,
        stateless_http=True,
        instructions=(
            "Use these tools to inspect and edit the configured workspace, run allowed "
            "verification commands, and ask the Claude Code/OpenRouter API for coding help."
        ),
    )
    mcp.settings.host = os.getenv("MCP_HOST", mcp.settings.host)
    mcp.settings.port = int(os.getenv("MCP_PORT", str(mcp.settings.port)))

    @mcp.tool()
    def analyze_project() -> dict[str, Any]:
        """Summarize the configured coding workspace and available commands."""
        return project_summary()

    @mcp.tool()
    def list_files(pattern: str = "*", limit: int = 200) -> dict[str, Any]:
        """List files under the configured workspace root."""
        return list_project_files(pattern=pattern, limit=limit)

    @mcp.tool()
    def read_file(path: str, max_chars: int = 20000) -> dict[str, Any]:
        """Read a UTF-8 text file from the configured workspace root."""
        return read_project_file(path=path, max_chars=max_chars)

    @mcp.tool()
    def write_file(path: str, content: str) -> dict[str, Any]:
        """Create or replace a UTF-8 text file inside the configured workspace root."""
        return write_project_file(path=path, content=content)

    @mcp.tool()
    def apply_patch(patch: str) -> dict[str, Any]:
        """Apply a git unified diff patch inside the configured workspace."""
        return apply_unified_patch(patch)

    @mcp.tool()
    def run_tests(command: str = ".venv/bin/python -m pytest -q") -> dict[str, Any]:
        """Run one exact command from MCP_ALLOWED_COMMANDS."""
        return run_allowed_command(command)

    @mcp.tool()
    async def gateway_status() -> dict[str, Any]:
        """Check whether the backing Claude gateway API is reachable."""
        return await gateway_health()

    @mcp.tool()
    async def list_gateway_models() -> dict[str, Any]:
        """List the models available through the configured gateway API token."""
        return await gateway_models()

    @mcp.tool()
    async def think_with_gateway(
        prompt: str,
        model: str = "claude-code-pro",
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        """Ask the backing gateway for coding reasoning using OpenRouter routing."""
        return await ask_gateway(prompt=prompt, model=model, max_tokens=max_tokens)

    @mcp.tool()
    async def ask_claude_api(
        prompt: str,
        model: str = "claude-code-pro",
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        """Ask the hosted Claude Code API and return its raw Anthropic-compatible response."""
        return await ask_gateway(prompt=prompt, model=model, max_tokens=max_tokens)

    @mcp.tool()
    async def coworking(
        task: str,
        project_context: str = "",
        mode: str = "pair_programming",
        model: str = "claude-code-pro",
        max_tokens: int = 1600,
    ) -> dict[str, Any]:
        """Run a coworking-style coding session through this project's API."""
        return await cowork_gateway(
            task=task,
            project_context=project_context,
            mode=mode,
            model=model,
            max_tokens=max_tokens,
        )

    return mcp


def run() -> None:
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    asyncio.run(_run(transport))


async def _run(transport: str) -> None:
    mcp = build_mcp_server()
    if hasattr(mcp, "run_async"):
        await mcp.run_async(transport=transport)
        return
    if transport == "stdio":
        await mcp.run_stdio_async()
        return
    if transport == "sse":
        await mcp.run_sse_async()
        return
    if transport == "streamable-http":
        await mcp.run_streamable_http_async()
        return
    raise ValueError(f"Unsupported MCP transport: {transport}")


if __name__ == "__main__":
    run()
