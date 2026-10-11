FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BOT_HOST=0.0.0.0 BOT_DB=/app/data/bot.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home bot
COPY app.py core.py engine.py ./
COPY web ./web
COPY scripts ./scripts
RUN mkdir -p /app/data && chown -R bot:bot /app
USER bot
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["python", "app.py"]
