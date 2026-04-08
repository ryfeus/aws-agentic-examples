FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_USE_BEDROCK=1
ENV AWS_REGION=us-east-1
ENV ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6
ENV ANTHROPIC_DEFAULT_SONNET_MODEL=us.anthropic.claude-sonnet-4-6
ENV ANTHROPIC_DEFAULT_HAIKU_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0
ENV CLAUDE_AGENT_PERMISSION_MODE=bypassPermissions
ENV CLAUDE_AGENT_ALLOWED_TOOLS=Read,Glob,Grep,WebFetch,Bash
ENV CLAUDE_AGENT_SANDBOX_ENABLED=true
ENV CLAUDE_AGENT_SANDBOX_AUTO_ALLOW_BASH=true
ENV CLAUDE_AGENT_SANDBOX_ALLOW_UNSANDBOXED=false
ENV CLAUDE_AGENT_SANDBOX_WEAKER_NESTED=true
ENV IS_SANDBOX=1
ENV AGENTCORE_LOG_LEVEL=INFO
ENV CLAUDE_CODE_MAX_OUTPUT_TOKENS=4096
ENV MAX_THINKING_TOKENS=1024
ENV NPM_CONFIG_UPDATE_NOTIFIER=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl git nodejs npm ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir claude-agent-sdk starlette "uvicorn[standard]"

WORKDIR /app

COPY app.py /app/app.py
COPY sticky_session_client.py /app/sticky_session_client.py

EXPOSE 8080

CMD ["python", "/app/app.py"]
