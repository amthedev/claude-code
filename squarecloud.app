MAIN=claude_gateway/main.py
DISPLAY_NAME=Frontier AI
DESCRIPTION=Frontier AI API and chat with OpenRouter cost controls.
MEMORY=1024
VERSION=recommended
SUBDOMAIN=claude-code-api
AUTORESTART=true
START=python -m pip install -r requirements.txt && python -m uvicorn claude_gateway.main:app --host 0.0.0.0 --port 80
