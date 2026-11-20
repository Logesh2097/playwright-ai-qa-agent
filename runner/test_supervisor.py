"""Tests for the runner supervisor. Standard library only:

    make runner-test

A stand-in for the Playwright MCP server (a script that listens on the port it
is given and spawns a child process) is started instead of a real browser, so
these tests check process handling, not browsing.
"""

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import supervisor as sup

TOKEN = "runner-test-token-0123456789abcdef"
OTHER = "11111111-1111-4111-8111-111111111111"
FAKE_SERVER = """
import socket, subprocess, sys, time
port = int(sys.argv[sys.argv.index("--port") + 1])
if "--viewport-size" in sys.argv and sys.argv[sys.argv.index("--viewport-size") + 1] == "666x666":
    sys.exit(3)  # stands for a server that fails to start
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])  # the "browser"
open("pids", "w").write(f"{child.pid}")
open("argv.json", "w").write(__import__("json").dumps(sys.argv))
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen()
while True:
    time.sleep(1)
"""


def body(**overrides):
    request = {
        "session_id": "0b0e6a2e-58a5-4a52-9d5e-0c1f8a7f3c11",
        "browser": "chromium",
        "viewport": {"width": 1280, "height": 720},
        "allowed_origins": ["https://shop.example", "http://demo-app:8080"],
        "proxy_url": "http://0b0e6a2e-58a5-4a52-9d5e-0c1f8a7f3c11:s3cr3t_-x@egress-proxy:3128",
        "action_timeout_ms": 5000,
        "navigation_timeout_ms": 30000,
        "ttl_s": 60,
    }
    request.update(overrides)
    return request


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child that nobody has waited for yet still "exists" as a zombie.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except OSError:
        return False


def gone(pid, within=3.0):
    """Signals are delivered asynchronously: give the process a moment to die."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = Path(self.tmp.name) / "fake_server.py"
        fake.write_text(FAKE_SERVER)
        self.config = sup.Config(
            {
                "RUNNER_TOKEN": TOKEN,
                "RUNNER_MAX_SESSIONS": "2",
                "RUNNER_PORT_RANGE": "19000-19001",
                "RUNNER_MCP_COMMAND": f"{sys.executable} {fake}",
                "RUNNER_WORK_DIR": self.tmp.name,
                "RUNNER_START_TIMEOUT_S": "5",
                "RUNNER_SESSION_MAX_SECONDS": "120",
            }
        )
        self.supervisor = sup.Supervisor(self.config)
        self.addCleanup(self.supervisor.stop_all)


class CommandLine(Base):
    def command(self, **overrides):
        return sup.build_command(self.config, body(**overrides), 19000, Path("/tmp/s"))

    def test_command_isolates_the_browser_and_routes_it_through_the_proxy(self):
        command = self.command()
        for flag in ("--headless", "--isolated", "--no-webmcp", "--block-service-workers"):
            self.assertIn(flag, command)
        pairs = dict(zip(command, command[1:]))
        self.assertEqual(pairs["--codegen"], "none")  # generated code would echo passwords
        self.assertEqual(pairs["--allowed-hosts"], "runner:19000")
        self.assertEqual(pairs["--allowed-origins"], "https://shop.example;http://demo-app:8080")
        self.assertEqual(pairs["--viewport-size"], "1280x720")
        self.assertEqual(pairs["--proxy-server"], body()["proxy_url"])
        self.assertEqual(pairs["--proxy-bypass"], "<-loopback>")
        for forbidden in ("--allow-unrestricted-file-access", "--shared-browser-context", "--user-data-dir", "--caps"):
            self.assertNotIn(forbidden, command)

    def test_values_that_could_smuggle_options_or_reach_elsewhere_are_rejected(self):
        bad = [
            {"browser": "chrome --allow-unrestricted-file-access"},
            {"browser": "firefox"},  # not installed in this image
            {"viewport": {"width": "1280", "height": 720}},
            {"viewport": {"width": 100000, "height": 720}},
            {"allowed_origins": []},
            {"allowed_origins": ["https://shop.example;*"]},
            {"allowed_origins": ["https://shop.example --caps devtools"]},
            {"allowed_origins": ["file:///etc"]},
            {"proxy_url": None},  # a browser without the proxy would have open egress
            {"proxy_url": "http://egress-proxy:3128 --no-sandbox"},
            {"proxy_url": "socks5://x:y@evil.example:1080"},
            {"action_timeout_ms": True},
            {"navigation_timeout_ms": 10**9},
        ]
        for overrides in bad:
            with self.subTest(overrides), self.assertRaises(sup.RequestError) as raised:
                self.command(**overrides)
            self.assertEqual(raised.exception.status, 422)


class Lifecycle(Base):
    def start(self, **overrides):
        session = self.supervisor.start(body(**overrides))
        child = int((session.directory / "pids").read_text())
        return session, child

    def test_session_gets_its_own_directory_port_and_process_and_all_are_removed(self):
        session, child = self.start()
        self.assertEqual(session.port, 19000)
        self.assertTrue((session.directory / "config.json").exists())
        config = json.loads((session.directory / "config.json").read_text())
        self.assertIs(config["browser"]["contextOptions"]["acceptDownloads"], False)
        self.assertTrue(alive(session.process.pid) and alive(child))

        self.assertTrue(self.supervisor.stop(session.id))

        self.assertTrue(gone(session.process.pid))
        self.assertTrue(gone(child), "the browser process must not outlive its session")
        self.assertFalse(session.directory.exists())
        self.assertEqual(self.supervisor.count(), 0)
        self.assertFalse(self.supervisor.stop(session.id))

    def test_the_child_gets_a_minimal_environment_without_the_runner_token(self):
        os.environ["RUNNER_TOKEN"] = TOKEN
        self.addCleanup(os.environ.pop, "RUNNER_TOKEN", None)
        session, _ = self.start()
        environ = Path(f"/proc/{session.process.pid}/environ").read_bytes()
        self.assertNotIn(TOKEN.encode(), environ)
        self.assertIn(f"HOME={session.directory}".encode(), environ)

    def test_capacity_and_duplicates_are_refused(self):
        self.start()
        with self.assertRaises(sup.RequestError) as duplicate:
            self.supervisor.start(body())
        self.assertEqual(duplicate.exception.status, 409)
        self.start(session_id="11111111-1111-4111-8111-111111111111")
        with self.assertRaises(sup.RequestError) as full:
            self.supervisor.start(body(session_id="22222222-2222-4222-8222-222222222222"))
        self.assertEqual(full.exception.status, 429)

    def test_a_server_that_fails_to_start_leaves_nothing_behind(self):
        with self.assertRaises(sup.RequestError) as raised:
            self.supervisor.start(body(viewport={"width": 666, "height": 666}))
        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(self.supervisor.count(), 0)
        self.assertEqual(list(self.config.work_dir.iterdir()), [])

    def test_an_invalid_request_reserves_nothing(self):
        with self.assertRaises(sup.RequestError):
            self.supervisor.start(body(browser="nope"))
        with self.assertRaises(sup.RequestError):
            self.supervisor.start(body(session_id="../../etc"))
        self.assertEqual(self.supervisor.count(), 0)

    def test_expired_and_dead_sessions_are_reaped(self):
        expired, child = self.start(ttl_s=10)
        dead, dead_child = self.start(session_id="11111111-1111-4111-8111-111111111111")
        expired.expires_at = time.monotonic() - 1
        os.kill(dead.process.pid, signal.SIGKILL)
        dead.process.wait()

        self.supervisor.reap()

        self.assertEqual(self.supervisor.count(), 0)
        self.assertTrue(gone(child) and gone(dead_child), "orphaned browsers must be killed")

    def test_session_lifetime_is_capped_by_the_runner(self):
        session, _ = self.start(ttl_s=86_400)
        self.assertLessEqual(session.expires_at - time.monotonic(), 120)


class Http(Base):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), sup.make_handler(self.supervisor))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, payload=None, token=TOKEN):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}{path}",
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None

    def test_everything_but_liveness_needs_the_token(self):
        self.assertEqual(self.call("GET", "/livez", token=None)[0], 200)
        for token in (None, "wrong-token"):
            self.assertEqual(self.call("GET", "/healthz", token=token)[0], 401)
            self.assertEqual(self.call("POST", "/sessions", body(), token=token)[0], 401)
            self.assertEqual(self.call("DELETE", f"/sessions/{body()['session_id']}", token=token)[0], 401)
        self.assertEqual(self.supervisor.count(), 0)

    def test_start_health_and_stop_over_http(self):
        status, started = self.call("POST", "/sessions", body())
        self.assertEqual((status, started["port"]), (201, 19000))
        self.assertEqual(self.call("GET", "/healthz")[1]["sessions"], 1)
        self.assertEqual(self.call("POST", "/sessions", body(browser="x", session_id=OTHER))[0], 422)
        self.assertEqual(self.call("DELETE", f"/sessions/{started['session_id']}")[0], 204)
        self.assertEqual(self.call("DELETE", f"/sessions/{started['session_id']}")[0], 404)
        self.assertEqual(self.call("DELETE", "/sessions/not-a-uuid")[0], 404)
        self.assertEqual(self.call("GET", "/healthz")[1]["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
