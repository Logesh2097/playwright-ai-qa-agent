#!/usr/bin/env python3
"""Browser runner: starts one Playwright MCP server per session and stops it again.

Standard library only, so the image needs nothing beyond Python and the pinned
Playwright MCP package. One session is one operating-system process group with
its own in-memory browser profile, its own working directory and its own port.

    POST   /sessions        start a session       (201, {"port": n})
    DELETE /sessions/{id}   stop it and delete its files
    GET    /healthz         capacity and versions
    GET    /livez           liveness, no authentication

Every request except /livez needs `Authorization: Bearer $RUNNER_TOKEN`.

The command line passed to Playwright MCP uses only options listed by
`--help` of the pinned version (0.0.86). Values from requests are validated
against strict patterns and passed as separate arguments, never through a shell.
"""

import hmac
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ORIGIN_RE = re.compile(r"^https?://(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(:\d{1,5})?$")
PROXY_RE = re.compile(r"^http://[A-Za-z0-9-]+:[A-Za-z0-9_-]+@[A-Za-z0-9.-]+:\d{1,5}$")
MAX_BODY = 64 * 1024


class Config:
    def __init__(self, env: dict[str, str]) -> None:
        self.token = env.get("RUNNER_TOKEN", "")
        if len(self.token) < 24:
            raise SystemExit("RUNNER_TOKEN must be set to at least 24 characters")
        self.port = int(env.get("RUNNER_PORT", "8900"))
        self.advertise_host = env.get("RUNNER_ADVERTISE_HOST", "runner")
        self.max_sessions = int(env.get("RUNNER_MAX_SESSIONS", "4"))
        self.max_seconds = int(env.get("RUNNER_SESSION_MAX_SECONDS", "900"))
        first, _, last = env.get("RUNNER_PORT_RANGE", "9000-9049").partition("-")
        self.ports = list(range(int(first), int(last) + 1))
        self.browsers = [b for b in env.get("RUNNER_BROWSERS", "chromium").split(",") if b]
        self.require_proxy = env.get("RUNNER_REQUIRE_PROXY", "true").lower() != "false"
        self.mcp_command = env.get("RUNNER_MCP_COMMAND", "node /app/cli.js").split()
        self.work_dir = Path(env.get("RUNNER_WORK_DIR", tempfile.gettempdir())) / "sessions"
        self.start_timeout_s = float(env.get("RUNNER_START_TIMEOUT_S", "20"))
        self.mcp_version = _mcp_version(env.get("RUNNER_MCP_PACKAGE", "/app/package.json"))


def _mcp_version(package_json: str) -> str | None:
    try:
        return str(json.loads(Path(package_json).read_text())["version"])
    except (OSError, ValueError, KeyError):
        return None


class RequestError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _int(body: dict, name: str, low: int, high: int) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RequestError(422, f"{name} must be an integer between {low} and {high}")
    return value


def build_command(config: Config, body: dict, port: int, session_dir: Path) -> list[str]:
    """Validate a request and turn it into the Playwright MCP command line."""
    browser = body.get("browser")
    if browser not in config.browsers:
        raise RequestError(422, f"browser must be one of: {', '.join(config.browsers)}")
    viewport = body.get("viewport")
    if not isinstance(viewport, dict):
        raise RequestError(422, "viewport is required")
    width = _int(viewport, "width", 320, 3840)
    height = _int(viewport, "height", 240, 2160)
    origins = body.get("allowed_origins")
    if (
        not isinstance(origins, list)
        or not 1 <= len(origins) <= 50
        or not all(isinstance(o, str) and ORIGIN_RE.match(o) for o in origins)
    ):
        raise RequestError(422, "allowed_origins must be a list of 1 to 50 http(s) origins")
    proxy = body.get("proxy_url")
    if proxy is None:
        if config.require_proxy:
            raise RequestError(422, "proxy_url is required")
    elif not isinstance(proxy, str) or not PROXY_RE.match(proxy):
        raise RequestError(422, "proxy_url is not an accepted proxy address")
    command = [
        *config.mcp_command,
        "--headless",
        # The profile lives in memory and dies with the process.
        "--isolated",
        "--no-sandbox",
        "--browser", browser,
        "--host", "0.0.0.0",  # noqa: S104 - reachable only on the internal runner network
        "--port", str(port),
        # Requests must name this exact host and port (protects against DNS rebinding).
        "--allowed-hosts", f"{config.advertise_host}:{port}",
        # Do not echo generated code: it would repeat typed text, passwords included.
        "--codegen", "none",
        # Pages must not be able to add tools of their own to the server.
        "--no-webmcp",
        "--block-service-workers",
        "--config", str(session_dir / "config.json"),
        "--output-dir", str(session_dir / "output"),
        "--viewport-size", f"{width}x{height}",
        "--timeout-action", str(_int(body, "action_timeout_ms", 500, 60_000)),
        "--timeout-navigation", str(_int(body, "navigation_timeout_ms", 1_000, 120_000)),
        # Convenience filter inside the browser. The egress proxy is the real boundary.
        "--allowed-origins", ";".join(origins),
    ]  # fmt: skip
    if proxy is not None:
        # "<-loopback>" removes the browser's built-in rule that loopback addresses skip
        # the proxy, so pages cannot reach services inside this container.
        command += ["--proxy-server", proxy, "--proxy-bypass", "<-loopback>"]
    return command


class Session:
    def __init__(self, session_id: str, port: int, directory: Path, expires_at: float) -> None:
        self.id = session_id
        self.port = port
        self.directory = directory
        self.expires_at = expires_at
        self.process: subprocess.Popen | None = None


class Supervisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        config.work_dir.mkdir(parents=True, exist_ok=True)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def start(self, body: dict) -> Session:
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not UUID_RE.match(session_id):
            raise RequestError(422, "session_id must be a lowercase UUID")
        ttl = min(_int(body, "ttl_s", 10, 86_400), self.config.max_seconds)
        with self._lock:
            if session_id in self._sessions:
                raise RequestError(409, "session already exists")
            if len(self._sessions) >= self.config.max_sessions:
                raise RequestError(429, "all browser slots are in use")
            used = {s.port for s in self._sessions.values()}
            port = next((p for p in self.config.ports if p not in used), None)
            if port is None:
                raise RequestError(429, "no free port")
            directory = self.config.work_dir / session_id
            session = Session(session_id, port, directory, time.monotonic() + ttl)
            self._sessions[session_id] = session
        try:
            command = build_command(self.config, body, port, directory)
            self._launch(session, command)
        except BaseException:
            self.stop(session_id)
            raise
        return session

    def _launch(self, session: Session, command: list[str]) -> None:
        directory = session.directory
        (directory / "output").mkdir(parents=True, mode=0o700)
        (directory / "config.json").write_text(
            # Downloads are refused by the browser itself.
            json.dumps({"browser": {"contextOptions": {"acceptDownloads": False}}})
        )
        log = (directory / "server.log").open("wb")
        session.process = subprocess.Popen(  # noqa: S603 - validated argument list, no shell
            command,
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(directory),
                "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
                "NODE_ENV": "production",
            },
        )
        log.close()
        deadline = time.monotonic() + self.config.start_timeout_s
        while time.monotonic() < deadline:
            if session.process.poll() is not None:
                raise RequestError(500, "the browser server exited while starting")
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(("127.0.0.1", session.port)) == 0:
                    return
            time.sleep(0.1)
        raise RequestError(500, "the browser server did not start in time")

    def stop(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        process = session.process
        if process is not None and process.poll() is None:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if process is not None:
            # Browser children can outlive the server; the whole group goes.
            _signal_group(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(session.directory, ignore_errors=True)
        return True

    def reap(self) -> None:
        """Stop sessions whose time is up or whose server has died."""
        now = time.monotonic()
        with self._lock:
            due = [
                s.id
                for s in self._sessions.values()
                if s.expires_at <= now or (s.process is not None and s.process.poll() is not None)
            ]
        for session_id in due:
            self.stop(session_id)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for session_id in ids:
            self.stop(session_id)


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def make_handler(supervisor: Supervisor) -> type[BaseHTTPRequestHandler]:
    config = supervisor.config

    class Handler(BaseHTTPRequestHandler):
        server_version = "runner"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _reply(self, status: int, body: dict | None = None) -> None:
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied.encode(), f"Bearer {config.token}".encode())

        def _handle(self, method: str) -> None:
            if method == "GET" and self.path == "/livez":
                self._reply(200, {"status": "ok"})
                return
            if not self._authorized():
                self._reply(401, {"detail": "unauthorized"})
                return
            try:
                if method == "GET" and self.path == "/healthz":
                    self._reply(
                        200,
                        {
                            "status": "ok",
                            "sessions": supervisor.count(),
                            "max_sessions": config.max_sessions,
                            "browsers": config.browsers,
                            "mcp_version": config.mcp_version,
                        },
                    )
                elif method == "POST" and self.path == "/sessions":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise RequestError(422, "a JSON body is required")
                    try:
                        body = json.loads(self.rfile.read(length))
                    except ValueError:
                        raise RequestError(422, "the body is not valid JSON") from None
                    if not isinstance(body, dict):
                        raise RequestError(422, "the body must be a JSON object")
                    session = supervisor.start(body)
                    log("session_started", session_id=session.id, port=session.port)
                    self._reply(201, {"session_id": session.id, "port": session.port})
                elif method == "DELETE" and self.path.startswith("/sessions/"):
                    session_id = self.path.removeprefix("/sessions/")
                    stopped = supervisor.stop(session_id) if UUID_RE.match(session_id) else False
                    if stopped:
                        log("session_stopped", session_id=session_id)
                    self._reply(204 if stopped else 404, None if stopped else {"detail": "no such session"})
                else:
                    self._reply(404, {"detail": "not found"})
            except RequestError as exc:
                self._reply(exc.status, {"detail": exc.detail})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return Handler


def log(event: str, **fields: object) -> None:
    record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    print(json.dumps({**record, **fields}), flush=True)


def main() -> None:
    config = Config(dict(os.environ))
    supervisor = Supervisor(config)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), make_handler(supervisor))  # noqa: S104

    def reaper() -> None:
        while True:
            time.sleep(5)
            supervisor.reap()

    threading.Thread(target=reaper, daemon=True).start()

    def shutdown(*_: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log("runner_started", port=config.port, mcp_version=config.mcp_version)
    try:
        server.serve_forever()
    finally:
        supervisor.stop_all()
        log("runner_stopped")


if __name__ == "__main__":
    sys.exit(main())
