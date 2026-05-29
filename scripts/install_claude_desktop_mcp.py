#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from claude_gateway.mcp_server import (
    HOSTED_GATEWAY_BASE_URL,
    LOCAL_DEV_TOKENS,
    claude_desktop_config_path,
    claude_desktop_server_config,
    merge_claude_desktop_config,
)


def _load_dotenv_token(path: Path) -> str:
    if not path.exists():
        return ""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() not in {"MCP_GATEWAY_TOKEN", "GATEWAY_API_KEY", "CLAUDE_CUSTOMER_API_KEY"}:
            continue
        values[key.strip()] = value.strip().strip('"').strip("'").split(",", 1)[0].strip()
    for key in ("MCP_GATEWAY_TOKEN", "GATEWAY_API_KEY", "CLAUDE_CUSTOMER_API_KEY"):
        value = values.get(key, "")
        if value not in LOCAL_DEV_TOKENS:
            return value
    return values.get("MCP_GATEWAY_TOKEN") or values.get("GATEWAY_API_KEY") or values.get("CLAUDE_CUSTOMER_API_KEY") or ""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Claude Desktop config is not valid JSON: {path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Claude Desktop config must be a JSON object: {path}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install this project's MCP bridge into Claude Desktop."
    )
    parser.add_argument("--server-name", default="claude-code-api")
    parser.add_argument("--config", default=str(claude_desktop_config_path()))
    parser.add_argument("--repo-root", default=str(Path.cwd()))
    parser.add_argument("--gateway-url", default=os.getenv("MCP_GATEWAY_BASE_URL", HOSTED_GATEWAY_BASE_URL))
    parser.add_argument(
        "--gateway-token",
        default=os.getenv("MCP_GATEWAY_TOKEN") or os.getenv("CLAUDE_CUSTOMER_API_KEY", ""),
    )
    parser.add_argument("--python", dest="python_executable", default="")
    parser.add_argument("--enable-write-tools", action="store_true")
    parser.add_argument("--enable-commands", action="store_true")
    parser.add_argument(
        "--allow-missing-token",
        action="store_true",
        help="Write the MCP config even when no gateway token is available.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).expanduser().resolve()
    token = args.gateway_token or _load_dotenv_token(repo_root / ".env")
    if args.gateway_url.rstrip("/") == HOSTED_GATEWAY_BASE_URL and token in LOCAL_DEV_TOKENS:
        token = ""
    if not token and not args.allow_missing_token:
        raise SystemExit(
            "Missing gateway token. Pass --gateway-token, set MCP_GATEWAY_TOKEN, "
            "or run with --allow-missing-token and edit Claude Desktop config later. "
            "Use a customer/API token from the Admin screen, not GATEWAY_API_KEYS."
        )

    server_config = claude_desktop_server_config(
        repo_root=str(repo_root),
        gateway_url=args.gateway_url,
        token=token,
        python_executable=args.python_executable or None,
        enable_write_tools=args.enable_write_tools,
        enable_commands=args.enable_commands,
    )

    config_path = Path(args.config).expanduser()
    existing = _read_json(config_path)
    merged = merge_claude_desktop_config(existing, args.server_name, server_config)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Installed MCP server '{args.server_name}' in {config_path}")
    print(f"Gateway URL: {server_config['env']['MCP_GATEWAY_BASE_URL']}")
    print("Restart Claude Desktop to load the MCP server.")


if __name__ == "__main__":
    main()
