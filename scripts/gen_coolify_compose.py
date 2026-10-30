#!/usr/bin/env python3
"""Generate a self-contained compose for Coolify (monitor embedded via dockerfile_inline). Usage: gen > out.yml"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
script = (ROOT / "monitor/monitor.py").read_text()
config = (ROOT / "monitor/config.json").read_text()
esc = lambda t: t.replace("$", "$$")
ind = lambda t, n: "\n".join((" " * n + l) if l.strip() else "" for l in t.splitlines())
dockerfile = f"""FROM python:3.12-slim
WORKDIR /app
COPY <<'PYEOF' /app/monitor.py
{esc(script)}
PYEOF
COPY <<'JSONEOF' /app/config.json
{esc(config)}
JSONEOF
ENV PYTHONUNBUFFERED=1 MONITOR_DB=/data/leads.db
VOLUME ["/data"]
CMD ["python", "monitor.py", "loop"]
"""
sys.stdout.write(f"""services:
  monitor:
    build:
      context: .
      dockerfile_inline: |
{ind(dockerfile, 8)}
    restart: unless-stopped
    environment:
      MONITOR_DB: /data/leads.db
      REDDIT_CLIENT_ID: "${{REDDIT_CLIENT_ID}}"
      REDDIT_CLIENT_SECRET: "${{REDDIT_CLIENT_SECRET}}"
      REDDIT_USERNAME: "${{REDDIT_USERNAME}}"
      OPENROUTER_API_KEY: "${{OPENROUTER_API_KEY}}"
      OPENROUTER_MODEL: "${{OPENROUTER_MODEL}}"
      TELEGRAM_BOT_TOKEN: "${{TELEGRAM_BOT_TOKEN}}"
      TELEGRAM_CHAT_ID: "${{TELEGRAM_CHAT_ID}}"
      TELEGRAM_ALERT_MENTION: "${{TELEGRAM_ALERT_MENTION}}"
      ALERTS_MUTED: "${{ALERTS_MUTED}}"
    volumes:
      - leads-data:/data

volumes:
  leads-data:
""")
