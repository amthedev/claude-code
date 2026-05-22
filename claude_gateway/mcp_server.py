from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
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
    return os.getenv(
        "MCP_GATEWAY_TOKEN",
        os.getenv("ANTHROPIC_AUTH_TOKEN", os.getenv("GATEWAY_API_KEY", "local-dev-token")),
    )


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
        "Frontier AI Coding Tools",
        json_response=True,
        stateless_http=True,
        instructions=(
            "Use these tools to inspect and edit the configured workspace, run allowed "
            "verification commands, and ask the Frontier AI/OpenRouter API for coding help."
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
        """Check whether the backing Frontier AI gateway API is reachable."""
        return await gateway_health()

    @mcp.tool()
    async def think_with_gateway(
        prompt: str,
        model: str = "claude-code-pro",
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        """Ask the backing gateway for coding reasoning using OpenRouter routing."""
        return await ask_gateway(prompt=prompt, model=model, max_tokens=max_tokens)

    return mcp


def run() -> None:
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    asyncio.run(_run(transport))


async def _run(transport: str) -> None:
    mcp = build_mcp_server()
    await mcp.run_async(transport=transport)


if __name__ == "__main__":
    run()
