"""chromectl daemon — a resident process that holds warm CDP connections.

The plain CLI pays ~100ms of Python interpreter+import boot and a fresh
discovery+attach on *every* invocation. The daemon pays that once: it runs the
same command dispatch as the CLI (via `cli.dispatch`) over one long-lived
process, keeping the connection pool warm between requests. A client sends an
argv, the daemon runs it and returns the captured stdout/stderr and exit code.

Transport is a user-only Unix socket with a trivial newline-delimited JSON
protocol — one request object per line in, one response object per line out.
That same framing is the seam for the future RPC/stream use (an agent holding
one connection and issuing many commands, boot paid zero times per command).

Requests:
    {"op": "ping"}                      -> {"ok": true}
    {"op": "status"}                    -> {"ok": true, "pid", "uptime", "pooled"}
    {"op": "shutdown"}                  -> {"ok": true, "shutdown": true}; server exits
    {"argv": ["eval", "t", "1+1", "--json"]}
        -> {"ok": bool, "code": int, "stdout": str, "stderr": str}
"""
import contextlib
import io
import json
import os
import socket
import sys
import time

_DIR = os.path.expanduser("~/.chromectl")
SOCK = os.path.join(_DIR, "daemon.sock")
PIDFILE = os.path.join(_DIR, "daemon.pid")

# Commands that stream/block (run until Ctrl-C or --max) or manage OS processes:
# these must run in their own process, never inside the shared daemon loop.
NON_ROUTABLE = {
    "watch", "console", "logs", "intercept", "capture", "dialog", "repl",
    "run", "start", "stop", "daemon", "heapsnapshot", "heap", "download",
}


class _Shutdown(Exception):
    """Raised by the shutdown op to break the accept loop cleanly."""


# --------------------------------------------------------------------------
# wire protocol (both ends) — one JSON object per line over a buffered file.
# A makefile handles framing correctly across many messages on one connection,
# which is what the persistent stream (Client, and the server loop) relies on.
# --------------------------------------------------------------------------
def _send(f, obj):
    f.write((json.dumps(obj) + "\n").encode())
    f.flush()


def _recv(f):
    """Read one message, or None at EOF (a clean disconnect)."""
    line = f.readline()
    return json.loads(line) if line.strip() else None


@contextlib.contextmanager
def _dial(timeout=30):
    """A short-lived connection for one request/response (the CLI-side helpers)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    f = s.makefile("rwb")
    try:
        yield f
    finally:
        with contextlib.suppress(Exception):
            f.close()
        s.close()


# --------------------------------------------------------------------------
# client side
# --------------------------------------------------------------------------
class Client:
    """A resident connection to the daemon: connect once, call many times.

    This is the pattern the daemon exists for — a long-lived agent that issues
    many commands pays the Python boot zero times per command and skips even the
    per-call socket setup:

        with Client() as c:
            c.call(["open", "https://target"])
            c.call(["replay", "--burp", "req.txt", "--as", "a.json", "--json"])
    """

    def __init__(self, timeout=30):
        if not is_running():
            raise ConnectionError("no daemon running (start it: chromectl daemon start)")
        self._s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._s.settimeout(timeout)
        self._s.connect(SOCK)
        self._f = self._s.makefile("rwb")

    def call(self, argv):
        """Run one command; return {ok, code, stdout, stderr}."""
        _send(self._f, {"argv": list(argv)})
        return _recv(self._f) or {}

    def close(self):
        with contextlib.suppress(Exception):
            self._f.close()
            self._s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def is_running():
    """True iff a daemon answers on the socket (a stale socket file → False)."""
    if not os.path.exists(SOCK):
        return False
    try:
        with _dial(timeout=2) as f:
            _send(f, {"op": "ping"})
            return bool((_recv(f) or {}).get("ok"))
    except OSError:
        return False


def route(argv):
    """Forward one argv to the daemon; print its output; return its exit code."""
    with _dial() as f:
        _send(f, {"argv": list(argv)})
        resp = _recv(f) or {}
    sys.stdout.write(resp.get("stdout", ""))
    sys.stderr.write(resp.get("stderr", ""))
    return int(resp.get("code", 0))


def status():
    if not is_running():
        return {"ok": True, "running": False}
    with _dial() as f:
        _send(f, {"op": "status"})
        st = _recv(f) or {}
    st["running"] = True
    return st


def stop():
    """Ask the daemon to shut down; fall back to signalling the pidfile."""
    if is_running():
        try:
            with _dial() as f:
                _send(f, {"op": "shutdown"})
                _recv(f)
            return True
        except OSError:
            pass
    if os.path.exists(PIDFILE):
        try:
            pid = int(open(PIDFILE).read().strip())
            os.kill(pid, 15)
            return True
        except (OSError, ValueError):
            pass
    return False


def spawn_background():
    """Start the daemon in a detached process and wait for it to answer."""
    import subprocess
    if is_running():
        import chromectl.cli as cli
        raise cli.UserError("daemon already running", "exists")
    os.makedirs(_DIR, exist_ok=True)
    log = open(os.path.join(_DIR, "daemon.log"), "a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "chromectl", "daemon", "start", "--foreground"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):                     # up to ~5s for the socket to come up
        if is_running():
            return proc.pid
        time.sleep(0.1)
    import chromectl.cli as cli
    raise cli.UserError("daemon did not come up (see ~/.chromectl/daemon.log)",
                        "launch-failed")


# --------------------------------------------------------------------------
# server side
# --------------------------------------------------------------------------
_PARSER = None


def _parser(cli):
    """Build the argparse tree once and reuse it. Rebuilding it per request cost
    ~11ms — the dominant per-call expense — and argparse parsers are safe to
    parse_args() repeatedly (each call returns a fresh Namespace)."""
    global _PARSER
    if _PARSER is None:
        _PARSER = cli.build_parser()
    return _PARSER


def _run_command(cli, argv):
    """Parse+dispatch one argv inside the daemon, capturing output and code.

    Reuses cli.dispatch (same -i resolution and error mapping as the CLI). The
    lazy consoles are reset so any human-mode rich output lands in our captured
    buffers rather than the daemon's own stdout.
    """
    out, errbuf = io.StringIO(), io.StringIO()
    cli.console._real = None
    cli.err._real = None
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
        try:
            args = _parser(cli).parse_args(argv)
            cli.dispatch(args)
        except SystemExit as e:             # die() and argparse both exit this way
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except BaseException as e:          # never let one bad request kill the loop
            code = 1
            errbuf.write(f"{type(e).__name__}: {e}\n")
    return code, out.getvalue(), errbuf.getvalue()


def _process(req, cli, started):
    """Turn one request into one response dict. A 'shutdown' response also
    carries {"shutdown": true} so the loop knows to break after replying."""
    op = req.get("op")
    if op == "ping":
        return {"ok": True}
    if op == "status":
        return {"ok": True, "pid": os.getpid(),
                "uptime": round(time.time() - started, 1),
                "pooled": len(cli._RUN_POOL)}
    if op == "shutdown":
        return {"ok": True, "shutdown": True}
    argv = req.get("argv")
    if argv is None:
        return {"ok": False, "code": 2, "stdout": "", "stderr": "bad request: no argv\n"}
    code, out, errout = _run_command(cli, argv)
    _evict_dead(cli)
    return {"ok": code == 0, "code": code, "stdout": out, "stderr": errout}


def _handle(conn, cli, started):
    """Serve one connection: handle requests until the client disconnects.

    Persistent — a resident Client sends many requests over one connection, so
    we loop until EOF. One-shot CLI helpers just send a single request and hang
    up, which reads as EOF on the next iteration."""
    conn.settimeout(None)
    f = conn.makefile("rwb")
    try:
        while True:
            req = _recv(f)
            if req is None:                 # client hung up
                return
            resp = _process(req, cli, started)
            _send(f, resp)
            if resp.get("shutdown"):
                raise _Shutdown()
    finally:
        with contextlib.suppress(Exception):
            f.close()


def _evict_dead(cli):
    """Drop pooled connections whose reader thread has died, so the next call
    for that target reconnects instead of raising on a corpse."""
    for ws, c in list(cli._RUN_POOL.items()):
        if not getattr(c, "_alive", False):
            cli._RUN_POOL.pop(ws, None)


def serve():
    """Run the accept loop until a shutdown op or signal. Blocks."""
    import chromectl.cli as cli
    os.makedirs(_DIR, exist_ok=True)
    if is_running():
        raise cli.UserError("daemon already running", "exists")
    if os.path.exists(SOCK):                # stale socket from a crash
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)                   # user-only; no network exposure
    srv.listen(16)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    cli._RUN_ACTIVE = True                  # make connect() pool into cli._RUN_POOL
    started = time.time()
    try:
        while True:
            conn, _ = srv.accept()
            try:
                _handle(conn, cli, started)
            except _Shutdown:
                break
            except OSError:
                pass                        # a client that hung up mid-request
            finally:
                conn.close()
    finally:
        cli._RUN_ACTIVE = False         # don't leak pooling into anything else
        srv.close()
        for c in list(cli._RUN_POOL.values()):
            c._pooled = False
            with contextlib.suppress(Exception):
                c.close()
        cli._RUN_POOL.clear()
        for path in (SOCK, PIDFILE):
            with contextlib.suppress(OSError):
                os.unlink(path)
