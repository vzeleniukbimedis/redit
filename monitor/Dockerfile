FROM python:3.12-slim
WORKDIR /app
COPY *.py config.json ./
ENV PYTHONUNBUFFERED=1 MONITOR_DB=/data/leads.db
VOLUME ["/data"]
CMD ["python", "monitor.py", "loop"]
