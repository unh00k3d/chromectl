"""Shared fixtures: a local HTTP server and one real headless Chrome per session.

The browser-driven tests talk to a Chrome that this suite starts and stops itself,
on a free port, with a throwaway profile — so they never touch the user's own
instances or the `~/.chromectl` registry entries they care about.
"""
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CHROME_BINARIES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        # defeat 304s so every test sees a real body and a 200
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


@pytest.fixture(scope="session")
def server():
    """A no-cache static server over tests/fixtures. Yields its base URL."""
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(QuietHandler, directory=FIXTURES))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture(scope="session")
def state_env(tmp_path_factory):
    """Environment pointing the instance registry at a throwaway file.

    Every subprocess in this suite inherits it, so a test run can never add to,
    stop, or purge an instance the developer actually cares about.
    """
    env = dict(os.environ)
    env["CHROMECTL_STATE"] = str(tmp_path_factory.mktemp("registry") / "instances.json")
    return env


@pytest.fixture(scope="session")
def chrome(state_env):
    """Start one headless Chrome for the session; yield its port. Stop it after."""
    if not any(shutil.which(b) for b in CHROME_BINARIES):
        pytest.skip("no Chrome/Chromium on PATH")
    port = free_port()
    name = f"pytest-{port}"
    proc = subprocess.run(
        [sys.executable, "-m", "chromectl", "start", "--name", name,
         "--port", str(port), "--ephemeral"],
        capture_output=True, text=True, timeout=120, env=state_env)
    if proc.returncode != 0:
        pytest.skip(f"could not start Chrome: {proc.stderr[-400:]}")
    try:
        yield port
    finally:
        subprocess.run([sys.executable, "-m", "chromectl", "stop", name, "--purge", "--force"],
                       capture_output=True, text=True, timeout=60, env=state_env)


@pytest.fixture
def cli(chrome, state_env):
    """Run the CLI against the session's Chrome. Returns a CompletedProcess."""
    def run(*args, timeout=90, check=False):
        cmd = [sys.executable, "-m", "chromectl", "--port", str(chrome)] + [str(x) for x in args]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=state_env)
        if check and p.returncode != 0:
            raise AssertionError(f"{' '.join(args)} failed ({p.returncode}):\n"
                                 f"{p.stdout}\n{p.stderr}")
        return p
    return run


@pytest.fixture
def manage(state_env):
    """Run a registry-level command (start/adopt/stop/instances) with no --port prefix."""
    def run(*args, timeout=120):
        cmd = [sys.executable, "-m", "chromectl"] + [str(x) for x in args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=state_env)
    return run


@pytest.fixture
def jcli(cli):
    """Run the CLI with --json and parse stdout. Returns (payload, returncode)."""
    def run(*args, timeout=90):
        p = cli(*args, "--json", timeout=timeout)
        try:
            return json.loads(p.stdout), p.returncode
        except json.JSONDecodeError as e:
            raise AssertionError(f"--json produced unparseable stdout for {args}: {e}\n"
                                 f"stdout={p.stdout!r}\nstderr={p.stderr!r}")
    return run


@pytest.fixture
def page(cli, jcli, server):
    """Open a fixture page and yield its target id; every opened tab is closed after."""
    opened = []

    @contextlib.contextmanager
    def _open(path="/index.html"):
        got, rc = jcli("open", server + path)
        assert rc == 0, got
        tid = got["id"]
        opened.append(tid)
        # a freshly created target can still report about:blank for a beat
        want = path.split("/")[-1]
        for _ in range(40):
            listed, _rc = jcli("list")
            hit = next((t for t in listed if t["id"] == tid), None)
            if hit and want in hit.get("url", ""):
                break
            time.sleep(0.1)
        yield tid

    yield _open
    for tid in opened:
        cli("close", tid)
