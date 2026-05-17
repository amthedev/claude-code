MAIN=claude_gateway/main.py
DISPLAY_NAME=Claude Gateway
DESCRIPTION=Claude-like chat and Claude Code API gateway with OpenRouter cost controls.
MEMORY=1024
VERSION=recommended
SUBDOMAIN=claude-code-amthedev
AUTORESTART=true
START=uvicorn claude_gateway.main:app --host 0.0.0.0 --port 80
