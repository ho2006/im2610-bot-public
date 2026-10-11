"""Web API and single-worker entry point. Run: python app.py."""
import atexit
import hmac
import logging
import os
import signal
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from core import Competition, Config, Unsafe
from engine import Engine, Paper, Store, process_lock

ROOT = Path(__file__).parent


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def create_app(engine, token):
    if len(token) < 24:
        raise Unsafe("BOT_TOKEN 至少需要 24 个字符；请运行 python scripts/init_env.py")
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=8192)

    @app.before_request
    def authenticate():
        if request.path.startswith("/api/"):
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                return jsonify(error="请输入正确的控制台令牌"), 401
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="禁止跨域控制"), 403

    @app.after_request
    def secure(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/")
    def index():
        return send_from_directory(ROOT / "web", "index.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(ROOT / "web", name)

    @app.get("/healthz")
    def health():
        # A risk latch is healthy monitoring; a dead/stalled worker is not.
        alive = engine.thread is None or engine.thread.is_alive()
        healthy = alive and time.time() - engine.last_cycle < max(120, engine.config.poll_seconds + 90)
        return jsonify(ok=healthy), 200 if healthy else 503

    @app.get("/api/state")
    def state():
        return jsonify(**engine.snapshot, **engine.store.history(), server_time=time.time())

    @app.post("/api/control")
    def control():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"action"}:
            return jsonify(error="需要 action 控制命令"), 400
        if data["action"] == "configure":
            return jsonify(error="请使用参数接口"), 400
        try:
            engine.command(data["action"])
        except (Unsafe, TypeError) as exc:
            return jsonify(error=str(exc) if isinstance(exc, Unsafe) else "命令格式不符"), 400
        return jsonify(queued=True), 202

    @app.put("/api/config")
    def config():
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise Unsafe("参数必须为对象")
            engine.command("configure", data)
        except (Unsafe, TypeError) as exc:
            return jsonify(error=str(exc) if isinstance(exc, Unsafe) else "参数格式不符"), 400
        return jsonify(queued=True), 202

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="请求体过大"), 413

    return app


def main():
    from waitress import serve
    load_env()
    mode = os.environ.get("BOT_MODE", "paper")
    if mode not in ("paper", "competition"):
        raise Unsafe("BOT_MODE 必须是 paper 或 competition")
    token = os.environ.get("BOT_TOKEN", "")
    if len(token) < 24:
        raise Unsafe("请先运行 python scripts/init_env.py 配置控制台令牌")
    path = Path(os.environ.get("BOT_DB", f"data/{mode}.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = process_lock(path)
    store = Store(path)
    broker = Competition(os.environ.get("CFAPIKEY", "")) if mode == "competition" else Paper(store, Config.parse(store.get("config", {})))
    engine = Engine(store, broker, autostart=os.environ.get("BOT_AUTOSTART", "false").lower() == "true")
    engine.process_lock = lock  # atexit retains the engine even if a timed-out main frame exits.
    app = create_app(engine, token)
    atexit.register(engine.stop)

    def shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    engine.start()
    logging.getLogger("waitress").setLevel(logging.WARNING)
    host = os.environ.get("BOT_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"IM2610 {mode} console: http://{host}:{port} (single worker)", flush=True)
    try:
        serve(app, host=host, port=port, threads=4, max_request_body_size=8192,
              channel_timeout=30, clear_untrusted_proxy_headers=True)
    finally:
        engine.stop()
        if not engine.thread or not engine.thread.is_alive():
            lock.close()  # Keep the account lock if shutdown cancellation is still running.


if __name__ == "__main__":
    main()
