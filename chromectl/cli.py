#!/usr/bin/env python3
"""
chromectl - a friendly command-line client for the Chrome DevTools Protocol.

Talks to a Chrome/Chromium instance started with --remote-debugging-port.
Everything is CDP under the hood; this just wraps it in ergonomic subcommands.

Quick start:
    # 1. launch Chrome:
    chromectl start

    # 2. use it:
    chromectl list
    chromectl open https://example.com
    chromectl eval example "document.title"
    chromectl capture https://news.ycombinator.com --type document --bodies
    chromectl watch example
    chromectl raw browser Browser.getVersion
    chromectl proto Network.getResponseBody
    chromectl repl example
"""
import argparse
import base64
import contextlib
import io
import json
import os
import queue
import shlex
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen, Request

import websocket  # websocket-client
from rich.console import Console
from rich.table import Table
from rich.json import JSON
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

console = Console()
err = Console(stderr=True, style="red")


def out_json(obj):
    """Print plain (pipeable) JSON to stdout."""
    print(json.dumps(obj, indent=2, default=str))


class UserError(RuntimeError):
    """A failure caused by the request itself (bad target, timeout, JS throw).

    Carries a machine-readable `kind` so --json callers can branch on it instead
    of scraping prose. Raised instead of exiting so a `run` step can fail alone.
    """

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


# The whole `error.kind` vocabulary, in one place. It used to live in three
# hand-written doc lists that had already drifted apart; now the docs are
# generated from this and a unit test greps the source to prove nothing new
# slipped in unlisted.
ERROR_KINDS = {
    "error": "unclassified failure (the default)",
    "bad-args": "the arguments contradict each other or are missing",
    "no-instance": "no managed instance by that name/port (see: chromectl instances)",
    "no-target": "no target matched, or the one that did can't be driven",
    "not-found": "the thing named does not exist (a file, a binary, an element)",
    "exists": "something with that name/port is already there",
    "timeout": "gave up waiting",
    "js-exception": "the page's JavaScript threw",
    "no-snapshot": "no saved snapshot for this instance (run: chromectl snapshot)",
    "no-history": "nothing to go back/forward to",
    "no-storage": "the target has no accessible web storage",
    "close-failed": "the browser refused to close that target",
    "launch-failed": "the process we started never opened the debug port",
    "missing-dep": "an external tool this command needs is not installed",
    "tool-failed": "an external tool ran but failed",
    "connection": "nothing reachable on that host:port",
    "cdp": "the browser rejected the command (its own message is passed through)",
}


def emit(a, payload, render=None):
    """Agent mode prints the payload as JSON; human mode runs the rich renderer.

    Every command funnels its result through here, so `--json` means the same
    thing everywhere: one JSON value on stdout, nothing else.
    """
    if getattr(a, "json", False):
        out_json(payload)
    elif render is not None:
        render()
    return payload


# --------------------------------------------------------------------------
# HTTP discovery endpoints
# --------------------------------------------------------------------------
def _http(host, port, path, method="GET"):
    url = f"http://{host}:{port}{path}"
    try:
        with urlopen(Request(url, method=method), timeout=10) as r:
            body = r.read().decode()
    except HTTPError as e:
        # The endpoint's real complaint is in the *body* — "Not supported",
        # "No such target id", and so on. urllib's str() throws it away and
        # leaves you with a bare "HTTP Error 500", so read it back out and
        # pass the browser's own words through to the caller.
        try:
            detail = e.read().decode("utf-8", "replace").strip()[:400]
        except Exception:
            detail = ""
        raise CDPError(f"{e.code} {e.reason}" + (f": {detail}" if detail else "")) from None
    return json.loads(body) if body.strip() else {}


def list_targets(host, port):
    return _http(host, port, "/json/list")


def browser_ws(host, port):
    return _http(host, port, "/json/version")["webSocketDebuggerUrl"]


def _target_ws(t):
    """The per-target debugger url, or a clean error saying why there isn't one.

    Not every target can be driven: one that is closing, or a kind the browser
    won't hand out a session for, simply arrives without the key. Subscripting
    it blind turned that into a KeyError traceback.
    """
    ws = t.get("webSocketDebuggerUrl")
    if not ws:
        raise UserError(
            f"target {str(t.get('id', ''))[:16]} (type {t.get('type', '?')}) exposes no "
            f"debugger url — it can't be driven; see: chromectl list", "no-target")
    return ws


def _await_load(host, port, t, want, timeout=10):
    """Wait for a freshly created target to actually be the page that was asked for.

    `Target.createTarget` returns as soon as the target exists, which is before it
    has navigated: it still reports `about:blank`, an empty url, and no title. Any
    command that ran straight afterwards — the documented `run --step 'open URL'
    --step 'read'` pattern — could read the blank page instead. So settle first,
    then re-read the target so the caller gets the real url and title.

    A page that never finishes loading is not an error; we return what we have.
    """
    ws = t.get("webSocketDebuggerUrl")
    if not ws:
        return t
    blank = want.startswith("about:")
    c = CDP(ws)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = c.call("Runtime.evaluate",
                           {"expression": "[location.href, document.readyState]",
                            "returnByValue": True})
                href, state = r["result"]["value"]
                if state == "complete" and (blank or not href.startswith("about:")):
                    break
            except (CDPError, KeyError, TypeError, ValueError):
                pass                       # mid-navigation: the context can go away
            time.sleep(0.1)
    finally:
        c.close()
    for cand in list_targets(host, port):  # url and title are populated now
        if cand["id"] == t["id"]:
            return cand
    return t


def new_target(host, port, url, wait_load=False):
    # Use CDP Target.createTarget (url is a JSON string) instead of /json/new?<url>,
    # which would put a raw, space-containing URL into the HTTP request path.
    c = CDP(browser_ws(host, port))
    try:
        tid = c.call("Target.createTarget", {"url": url})["targetId"]
    except CDPError as e:
        # Not every Chromium embedder has a tab model. Electron answers this
        # with "Not supported" — say so in its own words rather than ours, and
        # point at the windows that *are* open.
        raise UserError(
            f"this browser refused Target.createTarget: {e} — it has no tab model; "
            f"drive a window that is already open (see: chromectl list)", "cdp") from None
    finally:
        c.close()
    for _ in range(30):                       # resolve full target info (incl. its WS url)
        for t in list_targets(host, port):
            if t["id"] == tid:
                return _await_load(host, port, t, url) if wait_load else t
        time.sleep(0.1)
    raise UserError(f"created target {tid} but it never appeared in /json/list", "no-target")


def _close_target(host, port, tid):
    """Close a target; return (ok, why) so the caller can report the real reason."""
    try:
        _http(host, port, f"/json/close/{tid}")
        return True, ""
    except Exception as e:
        return False, str(e)


def close_target(host, port, tid):
    """Teardown-safe close: never raises, so a `finally:` can't mask a real error."""
    return _close_target(host, port, tid)[0]


def get_protocol(host, port):
    """Load the protocol schema: prefer a local cache next to this script, else fetch live."""
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protocol.json")
    if os.path.exists(local):
        with open(local) as f:
            return json.load(f)
    proto = _http(host, port, "/json/protocol")
    try:
        with open(local, "w") as f:
            json.dump(proto, f)
    except Exception:
        pass
    return proto


# --------------------------------------------------------------------------
# CDP session (websocket-client + background reader thread)
# --------------------------------------------------------------------------
class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws_url, timeout=30):
        # suppress_origin: modern Chrome rejects DevTools WS connections that carry
        # a disallowed Origin header (a security fix against websites hitting the port).
        # Sending no Origin (like curl/node do) is accepted.
        self.conn = websocket.create_connection(
            ws_url, max_size=None, timeout=timeout, enable_multithread=True,
            suppress_origin=True,
        )
        self.conn.settimeout(None)
        self._id = 0
        self._lock = threading.Lock()
        self.q = queue.Queue()
        self.buf = []          # stray events seen while waiting on a call()
        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while self._alive:
                data = self.conn.recv()
                if not data:
                    continue
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                self.q.put(json.loads(data))
        except Exception as e:
            if self._alive:
                self.q.put({"__error__": str(e)})

    def send(self, method, params=None, session_id=None):
        """Fire a command without waiting; returns its id."""
        with self._lock:
            self._id += 1
            mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.conn.send(json.dumps(msg))
        return mid

    def call(self, method, params=None, session_id=None, timeout=30):
        """Send a command and wait for its matching reply."""
        mid = self.send(method, params, session_id)
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                break
            if "__error__" in m:
                raise ConnectionError(m["__error__"])
            if m.get("id") == mid:
                if "error" in m:
                    raise CDPError(m["error"].get("message", str(m["error"])))
                return m.get("result", {})
            self.buf.append(m)          # keep events for later consumers
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def close(self):
        if getattr(self, "_pooled", False):
            return                      # kept alive by an active `run` pool
        self._alive = False
        try:
            self.conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# target resolution
# --------------------------------------------------------------------------
def _attachable(targets):
    """Targets we can open a session on, best first.

    A 'page' wins when there is one; otherwise anything carrying a debugger url
    will do. A Chromium embedder need not have a tab model at all, and its
    windows may come back as 'webview' or 'other' — insisting on 'page' there
    means reporting "nothing open" at a browser full of windows.
    """
    ok = [t for t in targets if t.get("webSocketDebuggerUrl") and t.get("type") != "browser"]
    return [t for t in ok if t.get("type") == "page"] or ok


def resolve(host, port, match, require_page=True):
    """Turn a user-supplied match (id / id-prefix / url or title substring /
    'browser' / '' for first attachable target) into a target dict."""
    if match == "browser":
        return {"id": "browser", "type": "browser", "title": "browser",
                "url": "", "webSocketDebuggerUrl": browser_ws(host, port)}
    targets = list_targets(host, port)
    if not match:
        cand = _attachable(targets)
        if not cand:
            kinds = ", ".join(sorted({t.get("type", "?") for t in targets})) or "none"
            raise UserError(f"no attachable target on {host}:{port} "
                            f"(targets present: {kinds}) — see: chromectl list", "no-target")
        return cand[0]
    for t in targets:               # exact id
        if t["id"] == match:
            return t
    for t in targets:               # id prefix
        if t["id"].startswith(match):
            return t
    ml = match.lower()              # url / title substring
    for t in targets:
        if (require_page and t["type"] != "page"):
            continue
        if ml in t.get("url", "").lower() or ml in t.get("title", "").lower():
            return t
    for t in targets:               # last resort: any type
        if ml in t.get("url", "").lower() or ml in t.get("title", "").lower():
            return t
    raise UserError(f"no target matching {match!r}", "no-target")


# --- connection pooling, active only during `run` (one connection per target) ---
_RUN_ACTIVE = False
_RUN_POOL = {}   # ws_url -> CDP


def connect(host, port, match, require_page=True):
    t = resolve(host, port, match, require_page)
    ws = _target_ws(t)
    if _RUN_ACTIVE:
        c = _RUN_POOL.get(ws)
        if c is None:
            c = CDP(ws)
            c._pooled = True
            _RUN_POOL[ws] = c
        c.buf.clear()                 # start each step with a clean event buffer
        return c, t
    return CDP(ws), t


def _pw_page_for(a, browser):
    """Resolve a target to a Playwright page — by unique CDP target id first
    (so identical URLs don't collide), then by URL, then _find_page."""
    tid, url = None, ""
    try:
        t = resolve(a.host, a.port, a.target)
        tid, url = t.get("id"), t.get("url", "")
    except (UserError, SystemExit):
        pass
    pairs = [(c, pg) for c in browser.contexts for pg in c.pages]
    if tid and tid != "browser":
        for c, pg in pairs:
            try:
                s = c.new_cdp_session(pg)
                got = s.send("Target.getTargetInfo")["targetInfo"]["targetId"]
                s.detach()
                if got == tid:
                    return pg
            except Exception:
                pass
    if url:
        for _, pg in pairs:
            if pg.url == url:
                return pg
        for _, pg in pairs:
            if url in pg.url or pg.url in url:
                return pg
    return _find_page(browser, a.target)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_list(a):
    targets = list_targets(a.host, a.port)
    if getattr(a, "json", False):
        out_json(targets)
        return
    tbl = Table(title=f"targets on {a.host}:{a.port}", header_style="bold cyan")
    tbl.add_column("type"); tbl.add_column("id", style="dim")
    tbl.add_column("title", max_width=40); tbl.add_column("url", max_width=60)
    for t in targets:
        style = "green" if t["type"] == "page" else ""
        tbl.add_row(t["type"], t["id"][:16] + "…", t.get("title", "")[:40],
                    t.get("url", "")[:60], style=style)
    console.print(tbl)
    console.print(f"[dim]{len(targets)} targets. Use an id-prefix or a url/title substring to select one.[/dim]")


def _surface():
    """Introspect the parser into a compact, structured command list (self-maintaining)."""
    parser = build_parser()
    subact = next(x for x in parser._actions if isinstance(x, argparse._SubParsersAction))
    help_map = {ca.dest: (ca.help or "") for ca in subact._choices_actions}
    by_parser, order = {}, []
    for name, sp in subact.choices.items():
        if id(sp) in by_parser:
            by_parser[id(sp)]["aliases"].append(name)
            continue
        by_parser[id(sp)] = {"command": name, "aliases": [], "sp": sp}
        order.append(id(sp))
    rows = []
    for key in order:
        rec = by_parser[key]
        sp = rec["sp"]
        pos, opts = [], []
        for act in sp._actions:
            if act.dest == "help":
                continue
            if not act.option_strings:
                mv = act.metavar or act.dest
                if act.nargs == "?":
                    pos.append(f"[{mv}]")
                elif act.nargs in ("+", "*"):
                    pos.append(f"{mv}...")
                else:
                    pos.append(f"<{mv}>")
            else:
                flag = act.option_strings[-1]
                if isinstance(act, (argparse._StoreTrueAction, argparse._StoreFalseAction)) or act.nargs == 0:
                    opts.append(flag)
                elif act.choices:
                    opts.append(f"{flag} {{{','.join(map(str, act.choices))}}}")
                else:
                    opts.append(f"{flag} {act.metavar or act.dest.upper()}")
        rows.append({"command": rec["command"], "aliases": rec["aliases"],
                     "args": pos, "options": opts, "help": help_map.get(rec["command"], "")})
    return rows


def cmd_cheat(a):
    rows = _surface()
    if a.json:
        out_json(rows)
        return
    from rich.markup import escape
    console.print("[bold]cdp commands[/bold]  [dim]target = id-prefix | url/title substring | 'browser' | empty=first page[/dim]\n")
    for r in rows:
        alias = f" ({'/'.join(r['aliases'])})" if r["aliases"] else ""
        usage = escape(" ".join(r["args"] + r["options"]))
        console.print(f"[bold cyan]{r['command']}[/bold cyan][dim]{alias}[/dim] {usage}")
        if r["help"]:
            console.print(f"    [dim]{escape(r['help'])}[/dim]")


CHROME_BINARIES = ["google-chrome", "google-chrome-stable", "chromium",
                   "chromium-browser", "chrome", "chrome.exe"]


def _default_profile_dir():
    """Best-effort location of the user's real Chrome/Chromium user-data-dir."""
    import platform
    home = os.path.expanduser("~")
    if platform.system() == "Darwin":
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        return os.path.join(base, "Google", "Chrome", "User Data")
    for name in ("google-chrome", "google-chrome-stable", "chromium"):
        d = os.path.join(home, ".config", name)
        if os.path.isdir(d):
            return d
    return os.path.join(home, ".config", "google-chrome")


# --- instance registry (managed Chrome processes) ---
# CHROMECTL_STATE lets a test (or a sandbox) point the registry somewhere else,
# so a subprocess-driven run never touches the user's real instances.
STATE_FILE = os.environ.get("CHROMECTL_STATE") or os.path.expanduser("~/.chromectl/instances.json")


def _inst_kind(i):
    """What's on the other end: 'chrome' or 'app'. Entries predating the field are Chrome."""
    return i.get("kind", "chrome")


def _inst_managed(i):
    """Did *we* spawn this process? Entries predating the field were all ours."""
    return i.get("managed", True)


def _load_instances():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_instances(items):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(items, f, indent=2)


def _port_alive(host, port):
    try:
        _http(host, port, "/json/version")
        return True
    except Exception:
        return False


def _free_port(host, start=9222, span=200):
    import socket
    bind_host = "127.0.0.1" if host in ("localhost", "127.0.0.1", "") else host
    for p in range(start, start + span):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((bind_host, p))
                return p
            except OSError:
                continue
    raise CDPError(f"no free port in {start}..{start + span}")


def _find_instance(sel):
    """Resolve a name or port string to a registry entry (or None)."""
    for i in _load_instances():
        if i.get("name") == sel or str(i.get("port")) == str(sel):
            return i
    return None


# --- chrome flags: user-supplied extras + proxy ---
def _switch_name(flag):
    return flag.split("=", 1)[0]


def _switch_value(flags, name):
    for f in flags:
        if _switch_name(f) == name:
            return f.split("=", 1)[1] if "=" in f else ""
    return None


def _merge_chrome_flags(base, extra):
    """Fold user flags into ours. Same switch twice = the user's value wins, in place."""
    out = list(base)
    for arg in extra:
        for idx, cur in enumerate(out):
            if _switch_name(cur) == _switch_name(arg):
                out[idx] = arg
                break
        else:
            out.append(arg)
    return out


def _extra_chrome_args(a):
    """--chrome-arg X (repeatable) plus anything after a bare `--`."""
    extra = []
    for arg in (a.chrome_arg or []):
        extra.append(arg if arg.startswith("-") else "--" + arg)
    for arg in (a.chrome_args or []):
        if not arg.startswith("-"):
            raise UserError(f"stray argument {arg!r} after `--` — Chrome flags start "
                            f"with a dash", "bad-args")
        extra.append(arg)
    return extra


def _proxy_plan(a):
    """Validate the proxy options up front.

    Returns (chrome_flags, upstream_needing_auth_or_None, registry_fields).
    Chrome takes a proxy but never credentials — it opens a login dialog, which
    is no use headless — so a proxy with a username/password gets a local relay
    (see `_start_relay`) that adds them on the way upstream.
    """
    from . import proxyrelay
    flags, info = [], {}
    if a.proxy and a.proxy_pac:
        raise UserError("--proxy and --proxy-pac are two ways to pick a proxy — keep one",
                        "bad-args")
    if a.proxy_auth and not a.proxy:
        raise UserError("--proxy-auth needs a --proxy to authenticate against", "bad-args")
    if a.proxy_pac:
        flags.append(f"--proxy-pac-url={a.proxy_pac}")
        info["proxy"] = f"pac:{a.proxy_pac}"
    if a.proxy_bypass:
        flags.append(f"--proxy-bypass-list={a.proxy_bypass}")
    if not a.proxy:
        return flags, None, info

    user = password = None
    if a.proxy_auth:
        user, _, password = a.proxy_auth.partition(":")
    try:
        up = proxyrelay.parse_proxy(a.proxy, user, password)
    except ValueError as e:
        raise UserError(str(e), "bad-args") from None
    info["proxy"] = proxyrelay.proxy_str(up)
    if not up["user"]:                              # no credentials: Chrome can do it alone
        flags.append(f"--proxy-server={up['scheme']}://{up['host']}:{up['port']}")
        return flags, None, info
    if up["scheme"] == "socks4":
        raise UserError("SOCKS4 has no password auth — use socks5:// or an http:// proxy",
                        "bad-args")
    return flags, up, info


def _start_relay(up, info, port_hint):
    """Run the authenticating relay for `up`; returns the Chrome flag pointing at it."""
    import socket as _socket
    import subprocess
    relay_port = _free_port("127.0.0.1", start=max(port_hint + 1000, 9500))
    # credentials go through the environment, never argv — argv is world-readable in `ps`
    env = dict(os.environ, CHROMECTL_PROXY_USER=up["user"],
               CHROMECTL_PROXY_PASSWORD=up["password"] or "")
    relay = subprocess.Popen(
        [sys.executable, "-m", "chromectl.proxyrelay", "--listen", str(relay_port),
         "--upstream", f"{up['scheme']}://{up['host']}:{up['port']}"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if relay.poll() is not None:
            raise UserError("proxy relay exited immediately — check the --proxy URL",
                            "launch-failed")
        try:
            with _socket.create_connection(("127.0.0.1", relay_port), 0.3):
                break
        except OSError:
            time.sleep(0.1)
    else:
        relay.kill()
        raise UserError(f"proxy relay did not come up on 127.0.0.1:{relay_port}",
                        "launch-failed")
    console.print(f"[cyan]proxy[/cyan] {info['proxy']} "
                  f"[dim](authenticating relay on 127.0.0.1:{relay_port}, pid {relay.pid})[/dim]")
    info["relay_pid"], info["relay_port"] = relay.pid, relay_port
    return f"--proxy-server=http://127.0.0.1:{relay_port}"


def _kill_relay(inst):
    """Stop the authenticating relay (if any) that belongs to an instance."""
    import signal
    pid = inst.get("relay_pid")
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as e:
        err.print(f"relay pid {pid}: {e}")


def _launch_flags(a, port, profile):
    """The command line we hand the process we're about to start.

    `profile` is None when we must not dictate one. An app instance exists to
    drive the user's real, logged-in application; --user-data-dir would send it
    to an empty one instead, which defeats the entire point. Isolation stays
    opt-in, via --profile/--ephemeral.

    An app also gets exactly ONE flag from us. The rest are Chrome-shaped and an
    app's own argv parser has every right to choke on them — some treat an
    unknown switch as a file to open. The user can still add any flag with
    --chrome-arg or after a bare `--`, and theirs win. We omit, never forbid.
    """
    flags = [f"--remote-debugging-port={port}"]
    if profile:
        flags.append(f"--user-data-dir={profile}")
    if not getattr(a, "app", None):
        flags += ["--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"]
        if not a.headful:
            flags.insert(0, "--headless=new")
    return _merge_chrome_flags(flags, _extra_chrome_args(a))


def _tail(path, n=800):
    """Last n characters of a log file, for reporting a launch that went nowhere."""
    if not path:
        return ""
    try:
        with open(path, errors="replace") as f:
            return f.read()[-n:].strip()
    except Exception:
        return ""


def _version_info(host, port):
    """(kind, browser, agent) for whatever is on that port.

    Electron reports `Browser: Chrome/<version>` just like Chrome does — the
    only honest marker is the Electron token in the user agent, e.g.
    `… Slack/4.33.84 Chrome/114.0.5735.289 Electron/25.3.1 Safari/537.36`.
    One generic substring, deliberately not a table of known applications.
    """
    v = _http(host, port, "/json/version")
    agent = v.get("User-Agent", "")
    return ("app" if "Electron/" in agent else "chrome"), v.get("Browser", ""), agent


def cmd_start(a):
    import shutil
    import subprocess
    import tempfile
    if a.app and a.binary:
        raise UserError("--app and --binary both name the executable — keep one", "bad-args")
    if a.app:
        binary = shutil.which(a.app) or (a.app if os.path.isfile(a.app) else None)
        if not binary:
            raise UserError(f"no such executable: {a.app}", "not-found")
        if a.copy_profile or a.from_profile:
            raise UserError("--copy-profile clones a Chrome profile; an app instance "
                            "already has its own (drop the flag, or use --profile)", "bad-args")
    else:
        binary = a.binary or next((b for b in CHROME_BINARIES if shutil.which(b)), None)
        if not binary:
            raise UserError("no Chrome/Chromium found on PATH — pass --binary /path/to/chrome "
                            "(or --app PATH for an Electron app)", "not-found")
    proxy_flags, needs_auth, proxy_info = _proxy_plan(a)   # fail on a bad proxy before anything runs
    port = _free_port(a.host) if a.auto_port else a.port
    label = a.name or (f"app-{port}" if a.app else f"chrome-{port}")
    existing = _find_instance(label)
    if a.profile:                                  # explicit dir wins
        profile = os.path.expanduser(a.profile)
    elif a.ephemeral:                              # throwaway, fresh each run
        profile = f"/tmp/chromectl-profile-{port}"
    elif a.app:                                    # an app keeps its OWN data dir — see _launch_flags
        profile = None
    elif existing and existing.get("profile"):     # reuse this instance's previous dir
        profile = existing["profile"]
    else:                                          # stable, persistent per name (logins survive restarts)
        profile = os.path.expanduser(os.path.join("~", ".chromectl", "profiles", label))

    if a.copy_profile or a.from_profile:
        src = a.from_profile or _default_profile_dir()
        if not os.path.isdir(src):
            raise UserError(f"source profile not found: {src}  (pass --from-profile PATH)", "not-found")
        if os.path.abspath(src) == os.path.abspath(profile):
            raise UserError("source and destination profile are the same — pick a different --profile",
                            "bad-args")
        ignore = shutil.ignore_patterns(
            "Cache", "Code Cache", "GPUCache", "ShaderCache", "GraphiteDawnCache",
            "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker", "CacheStorage",
            "Crashpad", "*.log", "Singleton*", "component_crx_cache", "optimization_guide*")
        console.print(f"[cyan]copying profile[/cyan] {src} → {profile}  [dim](skipping caches; may take a moment)[/dim]")
        try:
            shutil.copytree(src, profile, ignore=ignore, dirs_exist_ok=True, symlinks=True)
        except Exception as e:
            raise UserError(f"copy failed: {e} (close Chrome using that profile first, "
                            f"or point --from-profile at a copy)", "bad-args") from None
        console.print("[yellow]note:[/yellow] this profile carries your real cookies/logins — "
                      "anyone who reaches the debug port can act as you. Keep it local; delete when done.")
    extra = _extra_chrome_args(a)
    if a.proxy and any(_switch_name(f) == "--proxy-server" for f in extra):
        raise UserError("--proxy and a hand-passed --proxy-server do the same job — keep one",
                        "bad-args")
    flags = _launch_flags(a, port, profile)            # user flags override ours
    port = int(_switch_value(flags, "--remote-debugging-port") or port)
    profile = _switch_value(flags, "--user-data-dir") or profile
    if _port_alive(a.host, port):
        raise UserError(f"port {port} already has a live browser — drive it with "
                        f"`chromectl adopt {port}`, or pick another --port / --auto-port", "exists")
    if needs_auth:
        proxy_flags.append(_start_relay(needs_auth, proxy_info, port))
    flags = _merge_chrome_flags(flags, proxy_flags)
    argv = [binary] + flags
    # An app's own stdout/stderr is the only account we'll get of a launch that
    # goes nowhere, so keep it — in a file, not a PIPE, because a chatty app
    # fills the 64K pipe buffer and deadlocks with nobody reading.
    logpath, logfh = None, subprocess.DEVNULL
    if a.app:
        fd, logpath = tempfile.mkstemp(prefix="chromectl-app-", suffix=".log")
        logfh = os.fdopen(fd, "w")
    try:
        proc = subprocess.Popen(argv, stdout=logfh, stderr=subprocess.STDOUT if a.app else logfh,
                                start_new_session=True)
    finally:
        if a.app:
            logfh.close()
    span = a.wait if a.wait is not None else (45.0 if a.app else 12.0)
    deadline = time.time() + span
    while time.time() < deadline:
        if _port_alive(a.host, port):
            name = a.name or (f"app-{port}" if a.app else f"chrome-{port}")
            kind, browser, agent = _version_info(a.host, port)
            inst = {"name": name, "host": a.host, "port": port, "pid": proc.pid,
                    "profile": profile, "headful": bool(a.headful), "binary": binary,
                    "kind": kind, "managed": True, "browser": browser, "agent": agent,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"), **proxy_info}
            if extra:
                inst["chrome_args"] = extra
            if logpath:
                inst["log"] = logpath
            stale = [i for i in _load_instances()
                     if (i.get("port") == port and i.get("host") == a.host) or i.get("name") == name]
            for i in stale:
                # reap a previous run's relay, but never one whose browser is still up
                # on another port — that browser still needs it to reach the network
                same_slot = i.get("port") == port and i.get("host") == a.host
                if same_slot or not _port_alive(i.get("host", "localhost"), i.get("port")):
                    _kill_relay(i)
            items = [i for i in _load_instances() if i not in stale]
            items.append(inst)
            _save_instances(items)

            def render():
                what = "app up" if kind == "app" else "Chrome up"
                where = f"profile {profile}" if profile else "its own profile"
                console.print(f"[green]{what}[/green] [bold]{name}[/bold] on {a.host}:{port}  "
                              f"[dim]({browser or '?'}, pid {proc.pid}, {where})[/dim]")
                if kind == "app":
                    console.print("[yellow]note:[/yellow] this is the application's real, "
                                  "signed-in session — anyone who reaches the debug port is you. "
                                  "Keep it on localhost.")
                console.print(f"[dim]target it with:  chromectl -i {name} <cmd>   (or --port {port})[/dim]")

            return emit(a, {"ok": True, "name": name, "host": a.host, "port": port,
                            "pid": proc.pid, "kind": kind, "browser": browser,
                            "profile": profile, "binary": binary}, render)
        if proc.poll() is not None:            # it died, or handed off and exited
            break
        time.sleep(0.3)
    _kill_relay(proxy_info)
    rc, what = proc.poll(), os.path.basename(binary)
    if rc is not None:
        msg = (f"{what} exited (code {rc}) without opening port {port}. "
               f"If it was already running, a single-instance lock forwards the second "
               f"launch to the first process and drops our flags — quit it fully and retry. "
               f"If it is not Chromium/Electron-based it will not accept "
               f"--remote-debugging-port at all.")
    else:
        msg = (f"{what} is running but never opened port {port} within {span:g}s "
               f"(raise it with --wait SECONDS).")
    tail = _tail(logpath)
    if logpath:
        with contextlib.suppress(OSError):
            os.unlink(logpath)
    raise UserError(msg + (f"\n--- its output ---\n{tail}" if tail else ""), "launch-failed")


def cmd_instances(a):
    items = _load_instances()
    for i in items:
        i["up"] = _port_alive(i.get("host", "localhost"), i.get("port"))
    if a.prune:
        for i in items:
            if not i["up"]:
                _kill_relay(i)
        items = [i for i in items if i["up"]]
        _save_instances([{k: v for k, v in i.items() if k != "up"} for i in items])
    if a.json:
        out_json(items)
        return
    if not items:
        console.print("[dim]no managed instances (start one: chromectl start)[/dim]")
        return
    tbl = Table(header_style="bold cyan", title="chromectl instances")
    cols = ["name", "status", "host:port", "pid", "profile", "started"]
    show_proxy = any(i.get("proxy") for i in items)
    show_kind = any(_inst_kind(i) != "chrome" for i in items)
    if show_kind:
        cols.insert(2, "kind")
    if show_proxy:
        cols.insert(5 + show_kind, "proxy")
    for col in cols:
        tbl.add_column(col)
    for i in items:
        status = "[green]up[/green]" if i["up"] else "[red]down[/red]"
        if not _inst_managed(i):
            status += " [dim](adopted)[/dim]"
        row = [i.get("name", "?"), status]
        if show_kind:
            row.append(_inst_kind(i))
        row += [f"{i.get('host')}:{i.get('port')}", str(i.get("pid") or "-"),
                (i.get("profile") or "-")[-32:]]
        if show_proxy:
            row.append(i.get("proxy", "-"))
        row.append(i.get("created", ""))
        tbl.add_row(*row)
    console.print(tbl)


def _ua_product(agent):
    """The application's own product token out of a user agent, if there is one.

    `… Slack/4.33.84 Chrome/114.0.5735.289 Electron/25.3.1 Safari/537.36` → 'slack'.
    Purely to suggest a default instance name; nothing branches on the result.
    """
    import re
    generic = {"mozilla", "applewebkit", "khtml", "gecko", "chrome", "chromium",
               "headlesschrome", "electron", "safari", "version", "like", "edg", "mobile"}
    # Drop the platform parenthetical — "(X11; Linux x86_64)" is not a product.
    for token in re.sub(r"\([^)]*\)", " ", agent).split():
        if "/" not in token:
            continue                     # a product token is always Name/Version
        name = token.split("/", 1)[0].lower()
        if name and name not in generic and name.isidentifier():
            return name
    return ""


def cmd_adopt(a):
    """Record a debug port somebody else opened, so `-i NAME` works against it.

    We start nothing here, and `stop` will refuse to kill what we did not start —
    on the other end of that port may be the user's real, signed-in application.
    """
    port = int(a.port_arg) if a.port_arg else a.port
    if not _port_alive(a.host, port):
        raise UserError(f"nothing is listening on {a.host}:{port} — start the app with "
                        f"--remote-debugging-port={port} first (it must not already be "
                        f"running: a single-instance lock swallows the flag)", "connection")
    v = _http(a.host, port, "/json/version")
    if not v.get("webSocketDebuggerUrl"):
        raise UserError(f"{a.host}:{port} answers HTTP but is not a DevTools endpoint "
                        f"(no webSocketDebuggerUrl in /json/version)", "connection")
    agent = v.get("User-Agent", "")
    kind = "app" if "Electron/" in agent else "chrome"
    name = a.name or _ua_product(agent) or f"{kind}-{port}"
    already = next((i for i in _load_instances()
                    if i.get("port") == port and i.get("host") == a.host), None)
    if already and _inst_managed(already) and not a.force:
        raise UserError(f"port {port} is already the managed instance "
                        f"{already.get('name')!r} — drive it with `-i {already.get('name')}` "
                        f"(--force relabels it without forgetting we started it)", "exists")
    clash = _find_instance(name)
    if clash and not a.force:
        raise UserError(f"instance {name!r} already exists on port {clash.get('port')} — "
                        f"pass --name, or --force to replace it", "exists")
    # Whether we started the process is a fact about history, not a label, so
    # re-adopting a port we launched keeps its pid and stays managed. Dropping
    # them would strand a browser we own: `stop` would refuse to touch it and
    # nothing else knows the pid.
    inst = {"name": name, "host": a.host, "port": port,
            "pid": already.get("pid") if already else None,
            "profile": already.get("profile") if already else None,
            "kind": kind, "managed": bool(already and _inst_managed(already)),
            "browser": v.get("Browser", ""), "agent": agent,
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    if already and already.get("log"):
        inst["log"] = already["log"]
    items = [i for i in _load_instances()
             if i.get("name") != name and not (i.get("port") == port and i.get("host") == a.host)]
    items.append(inst)
    _save_instances(items)
    n = len(_attachable(list_targets(a.host, port)))

    def render():
        console.print(f"[green]adopted[/green] [bold]{name}[/bold] on {a.host}:{port}  "
                      f"[dim]({inst['browser'] or '?'}, {kind}, {n} attachable target"
                      f"{'' if n == 1 else 's'})[/dim]")
        if not inst["managed"]:
            console.print(f"[dim]not started by chromectl — `stop {name}` will refuse "
                          f"to kill it[/dim]")
        console.print(f"[dim]drive it with:  chromectl -i {name} <cmd>   (or --port {port})[/dim]")

    return emit(a, {**inst, "ok": True, "targets": n}, render)


def cmd_stop(a):
    import signal
    items = _load_instances()
    if a.all:
        targets = list(items)
    else:
        if not a.which:
            raise UserError("say which: a name, a port, or --all", "bad-args")
        hit = _find_instance(a.which)
        if hit:
            targets = [hit]
        elif str(a.which).isdigit():          # a bare port not in the registry
            targets = [{"name": a.which, "host": a.host, "port": int(a.which), "pid": None}]
        else:
            raise UserError(f"no instance named/port {a.which!r} (see: chromectl instances)",
                            "no-instance")
    stopped_keys, forgotten, results = set(), set(), []
    say = (lambda *_: None) if getattr(a, "json", False) else console.print
    for t in targets:
        pid, port, host = t.get("pid"), t.get("port"), t.get("host", "localhost")
        name = t.get("name", "?")
        if not _inst_managed(t) and not a.force:
            # We adopted this one. Behind that port is somebody's real application,
            # and the pkill fallback below would take it down with no warning.
            if a.forget or a.all:
                forgotten.add((host, port))
                results.append({"name": name, "port": port, "action": "forgot"})
                say(f"[yellow]forgot[/yellow] {name} [dim](port {port}) — "
                    f"not started by chromectl; left running[/dim]")
                continue
            raise UserError(
                f"{name} was adopted, not started by chromectl — it is running as pid "
                f"{pid if pid else 'unknown'} and stopping it would quit the real application. "
                f"Use `stop {name} --forget` to drop it from the registry, or "
                f"`stop {name} --force` to actually kill it.", "bad-args")
        if a.forget:                           # drop the entry, leave the process alone
            forgotten.add((host, port))
            results.append({"name": name, "port": port, "action": "forgot"})
            say(f"[yellow]forgot[/yellow] {name} [dim](port {port}) — left running[/dim]")
            continue
        ok = False
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                ok = True
            except ProcessLookupError:
                ok = True                      # already gone
            except Exception as e:
                err.print(f"{name}: {e}")
        if not ok or pid is None:
            # A managed app can outlive the pid we recorded: an Electron single-instance
            # lock hands our launch to the first process, which then exits. The port is
            # still ours, and we started it with exactly this flag, so matching on it is safe.
            os.system(f'pkill -f "remote-debugging-port={port}" 2>/dev/null')
        _kill_relay(t)
        msg = f"[green]stopped[/green] {name} [dim](port {port})[/dim]"
        if a.purge and t.get("profile"):
            import shutil
            import time as _t
            _t.sleep(0.3)                      # let Chrome release the profile
            shutil.rmtree(t["profile"], ignore_errors=True)
            msg += f"  [yellow]purged[/yellow] {t['profile']}"
        if t.get("log"):
            with contextlib.suppress(OSError):
                os.unlink(t["log"])
        say(msg)
        results.append({"name": name, "port": port, "action": "stopped"})
        stopped_keys.add((host, port))
    gone = stopped_keys | forgotten
    remaining = [i for i in items if (i.get("host", "localhost"), i.get("port")) not in gone]
    _save_instances(remaining)
    if getattr(a, "json", False):
        out_json({"ok": True, "instances": results})


def cmd_version(a):
    v = _http(a.host, a.port, "/json/version")
    return emit(a, v, lambda: console.print(
        Panel(JSON(json.dumps(v)), title="Browser", border_style="cyan")))


def cmd_open(a):
    # wait for it to land: the tab we hand back is immediately readable, and the
    # url/title we report are the page's, not the blank one it started as
    t = new_target(a.host, a.port, a.url, wait_load=True)

    def render():
        console.print(f"[green]opened[/green] {t.get('url') or a.url}")
        console.print(f"  id:  [dim]{t['id']}[/dim]")

    emit(a, {"ok": True, "id": t["id"], "url": t.get("url") or a.url,
             "title": t.get("title", "")}, render)
    return t


def cmd_close(a):
    t = resolve(a.host, a.port, a.target)
    ok, why = _close_target(a.host, a.port, t["id"])
    if not ok:
        raise UserError(f"could not close {t.get('url','')}: {why}", "close-failed")
    return emit(a, {"ok": True, "id": t["id"], "url": t.get("url", "")},
                lambda: console.print(f"[green]closed[/green] {t.get('url','')}"))


def cmd_goto(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("Page.enable")
        loaded = threading.Event()
        c.call("Page.navigate", {"url": a.url})
        # drain buffered + wait briefly for load event
        deadline = time.time() + a.timeout
        seen = list(c.buf); c.buf.clear()
        for m in seen:
            if m.get("method") == "Page.loadEventFired":
                loaded.set()
        while not loaded.is_set() and time.time() < deadline:
            try:
                m = c.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if m.get("method") == "Page.loadEventFired":
                loaded.set()
        return emit(a, {"ok": True, "url": a.url, "from": t.get("url", ""),
                        "loaded": loaded.is_set()},
                    lambda: console.print(
                        f"[green]navigated[/green] {t.get('url','')} → {a.url}"
                        f" {'(loaded)' if loaded.is_set() else '(load not confirmed)'}"))
    finally:
        c.close()


def cmd_eval(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        expr = " ".join(a.js)
        r = c.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
            "userGesture": True,
        })
        if "exceptionDetails" in r:
            ex = r["exceptionDetails"]
            raise UserError(ex.get("exception", {}).get("description", ex.get("text", "")), "js-exception")
        val = r["result"].get("value", r["result"].get("description"))

        def render():
            if isinstance(val, (dict, list)):
                console.print(JSON(json.dumps(val)))
            else:
                console.print(val)

        return emit(a, val, render)
    finally:
        c.close()


def cmd_html(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        r = c.call("Runtime.evaluate",
                   {"expression": "document.documentElement.outerHTML", "returnByValue": True})
        html = r["result"]["value"]
        if a.out:
            with open(a.out, "w") as f:
                f.write(html)
            return emit(a, {"ok": True, "chars": len(html), "out": a.out},
                        lambda: console.print(f"[green]wrote[/green] {len(html)} chars → {a.out}"))

        def render():
            console.print(Syntax(html[:a.max], "html", theme="ansi_dark", word_wrap=True))
            if len(html) > a.max:
                console.print(f"[dim]… truncated ({len(html)} total). Use --out to save all.[/dim]")

        return emit(a, {"ok": True, "chars": len(html), "html": html}, render)
    finally:
        c.close()


def cmd_text(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        r = c.call("Runtime.evaluate",
                   {"expression": "document.body.innerText", "returnByValue": True})
        txt = r["result"].get("value", "") or ""
        return emit(a, {"ok": True, "chars": len(txt), "text": txt},
                    lambda: console.print(txt))
    finally:
        c.close()


def cmd_cookies(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        if a.clear:
            c.call("Network.clearBrowserCookies")
            return emit(a, {"ok": True, "cleared": True},
                        lambda: console.print("[green]cleared[/green] all cookies"))
        changed = []
        for spec in a.set:
            if "=" not in spec:
                raise UserError(f"bad --set {spec!r} — use name=value", "bad-args")
            name, value = spec.split("=", 1)
            ck = {"name": name, "value": value, "url": a.url or t.get("url", "")}
            if not ck["url"]:
                raise UserError("no URL for the cookie — pass --url", "bad-args")
            if a.domain:
                ck["domain"] = a.domain
            c.call("Network.setCookie", ck)
            changed.append(name)
        for name in a.delete:
            c.call("Network.deleteCookies", {"name": name,
                                             "url": a.url or t.get("url", "")})
            changed.append(name)
        cookies = c.call("Network.getCookies").get("cookies", [])
        if changed and not a.json:
            console.print(f"[green]updated[/green] {', '.join(changed)}")
        if a.json:
            out_json(cookies)
            return cookies
        tbl = Table(title=f"cookies for {t.get('url','')}", header_style="bold cyan")
        for col in ("name", "value", "domain", "path", "flags"):
            tbl.add_column(col, max_width=40 if col == "value" else None)
        for ck in cookies:
            flags = " ".join(f for f, on in (
                ("HttpOnly", ck.get("httpOnly")), ("Secure", ck.get("secure")),
                ("Session", ck.get("session")),
                (f"SameSite={ck.get('sameSite')}", ck.get("sameSite"))) if on)
            tbl.add_row(ck["name"], ck["value"][:40], ck.get("domain", ""),
                        ck.get("path", ""), flags)
        console.print(tbl)
        console.print(f"[dim]{len(cookies)} cookies[/dim]")
    finally:
        c.close()


def cmd_screenshot(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("Page.enable")
        params = {"format": "png"}
        if a.full:
            m = c.call("Page.getLayoutMetrics")
            css = m.get("cssContentSize") or m.get("contentSize")
            params["clip"] = {"x": 0, "y": 0, "width": css["width"],
                              "height": css["height"], "scale": 1}
            params["captureBeyondViewport"] = True
        data = c.call("Page.captureScreenshot", params)["data"]
        out = a.out or f"screenshot-{int(time.time())}.png"
        with open(out, "wb") as f:
            f.write(base64.b64decode(data))
        return emit(a, {"ok": True, "out": os.path.abspath(out), "full": bool(a.full)},
                    lambda: console.print(f"[green]saved[/green] → {out}"))
    finally:
        c.close()


def cmd_pdf(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("Page.enable")
        data = c.call("Page.printToPDF", {"printBackground": True})["data"]
        out = a.out or f"page-{int(time.time())}.pdf"
        with open(out, "wb") as f:
            f.write(base64.b64decode(data))
        return emit(a, {"ok": True, "out": os.path.abspath(out)},
                    lambda: console.print(f"[green]saved[/green] → {out}"))
    finally:
        c.close()


# ---- Tier 1: emulation, input, dialogs, upload, heap ----
KEYMAP = {  # name -> (windowsVirtualKeyCode, text)
    "Enter": (13, "\r"), "Tab": (9, "\t"), "Escape": (27, ""), "Backspace": (8, ""),
    "Delete": (46, ""), "Space": (32, " "), "ArrowUp": (38, ""), "ArrowDown": (40, ""),
    "ArrowLeft": (37, ""), "ArrowRight": (39, ""), "Home": (36, ""), "End": (35, ""),
    "PageUp": (33, ""), "PageDown": (34, ""),
}

# name -> (offline, latency_ms, download_Bps, upload_Bps) | None to disable
THROTTLE = {
    "offline": (True, 0, 0, 0),
    "slow-3g": (False, 400, 50 * 1024, 50 * 1024),
    "fast-3g": (False, 150, 180 * 1024, 84 * 1024),
    "4g":      (False, 20, 500 * 1024, 500 * 1024),
    "none":    None,
}


def _press_key(c, key):
    if key in KEYMAP:
        vk, text = KEYMAP[key]
        down = {"type": "keyDown", "key": key, "code": key, "windowsVirtualKeyCode": vk}
        if text:
            down["text"] = text
        c.call("Input.dispatchKeyEvent", down)
        c.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": key,
                                          "windowsVirtualKeyCode": vk})
    elif len(key) == 1:
        c.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "text": key})
        c.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
    else:
        raise CDPError(f"unknown key {key!r} (single char, or one of: {', '.join(KEYMAP)})")


def _focus(c, selector):
    r = c.call("Runtime.evaluate", {
        "expression": f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
                      f"if(!e)return false;e.focus();return true;}})()",
        "returnByValue": True})
    if not r["result"].get("value"):
        raise UserError(f"selector not found: {selector}", "not-found")


def cmd_emulate(a):
    c, t = connect(a.host, a.port, a.target)
    applied = []
    try:
        if getattr(a, "clear", False):
            c.call("Emulation.clearDeviceMetricsOverride")
            try:
                c.call("Emulation.clearGeolocationOverride")
            except CDPError:
                pass
            c.call("Emulation.setEmulatedMedia", {"features": []})
            c.call("Network.enable")
            c.call("Network.emulateNetworkConditions",
                   {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
            console.print("[green]cleared overrides[/green] "
                          "[dim](note: reverts anyway once a session closes)[/dim]")
            return
        if a.width and a.height:
            c.call("Emulation.setDeviceMetricsOverride",
                   {"width": a.width, "height": a.height,
                    "deviceScaleFactor": a.scale, "mobile": a.mobile})
            applied.append(f"viewport {a.width}x{a.height}{' mobile' if a.mobile else ''}")
        if getattr(a, "geo", None):
            lat, lon = (float(x) for x in a.geo.split(","))
            c.call("Emulation.setGeolocationOverride",
                   {"latitude": lat, "longitude": lon, "accuracy": 1})
            applied.append(f"geo {lat},{lon}")
        if getattr(a, "color", None):
            c.call("Emulation.setEmulatedMedia",
                   {"features": [{"name": "prefers-color-scheme", "value": a.color}]})
            applied.append(f"prefers-color-scheme:{a.color}")
        if getattr(a, "ua", None):
            c.call("Emulation.setUserAgentOverride", {"userAgent": a.ua})
            applied.append("user-agent")
        if getattr(a, "throttle", None):
            c.call("Network.enable")
            prof = THROTTLE[a.throttle]
            if prof is None:
                c.call("Network.emulateNetworkConditions",
                       {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
            else:
                off, lat, down, up = prof
                c.call("Network.emulateNetworkConditions",
                       {"offline": off, "latency": lat,
                        "downloadThroughput": int(down), "uploadThroughput": int(up)})
            applied.append(f"network:{a.throttle}")
        if not applied:
            raise UserError("nothing to emulate — see --help", "bad-args")
        result = {"ok": True, "applied": applied}
        if not getattr(a, "json", False):
            console.print("[green]applied[/green] " + "; ".join(applied))
        if getattr(a, "shot", None):
            c.call("Page.enable")
            data = c.call("Page.captureScreenshot",
                          {"format": "png", "captureBeyondViewport": True})["data"]
            with open(a.shot, "wb") as f:
                f.write(base64.b64decode(data))
            result["shot"] = os.path.abspath(a.shot)
            if not getattr(a, "json", False):
                console.print(f"[green]screenshot[/green] → {a.shot}")
        if getattr(a, "json", False):
            out_json(result)
            if not getattr(a, "hold", False):
                return result
        if getattr(a, "hold", False):
            if not getattr(a, "json", False):
                console.print("[dim]holding session so overrides persist — Ctrl-C to release[/dim]")
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                if not getattr(a, "json", False):
                    console.print("\n[dim]released (overrides revert)[/dim]")
        elif not getattr(a, "shot", None) and not getattr(a, "json", False):
            console.print("[dim]note: CDP overrides revert when this connection closes; "
                          "use --hold to keep them active, or --shot to capture now.[/dim]")
    finally:
        c.close()


def cmd_resize(a):
    # thin wrapper over emulate's device-metrics path
    a.geo = a.color = a.ua = a.throttle = None
    a.clear = False
    cmd_emulate(a)


def cmd_press(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        if a.selector:
            _focus(c, a.selector)
        for key in a.keys:
            _press_key(c, key)
        return emit(a, {"ok": True, "pressed": list(a.keys)},
                    lambda: console.print(f"[green]pressed[/green] {' '.join(a.keys)}"))
    finally:
        c.close()


def cmd_type(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        if a.selector:
            _focus(c, a.selector)
        text = " ".join(a.text)
        c.call("Input.insertText", {"text": text})
        if a.enter:
            _press_key(c, "Enter")
        return emit(a, {"ok": True, "chars": len(text), "enter": bool(a.enter)},
                    lambda: console.print(f"[green]typed[/green] {len(text)} chars"
                                          + (" + Enter" if a.enter else "")))
    finally:
        c.close()


def cmd_upload(a):
    files = [os.path.abspath(f) for f in a.files]
    for f in files:
        if not os.path.exists(f):
            raise UserError(f"no such file: {f}", "bad-args")
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("DOM.enable")
        r = c.call("Runtime.evaluate", {"expression": f"document.querySelector({json.dumps(a.selector)})"})
        obj = r["result"].get("objectId")
        if not obj:
            raise UserError(f"selector not found (or not an element): {a.selector}", "not-found")
        c.call("DOM.setFileInputFiles", {"files": files, "objectId": obj})
        return emit(a, {"ok": True, "files": files, "selector": a.selector},
                    lambda: console.print(f"[green]set[/green] {len(files)} file(s) on {a.selector}"))
    finally:
        c.close()


def cmd_dialog(a):
    c, t = connect(a.host, a.port, a.target)
    verb = "accept" if a.accept else "dismiss"
    console.print(Panel(f"auto-{verb} JS dialogs on {t.get('url', t['id'])}  —  Ctrl-C to stop",
                        border_style="cyan"))

    def handle(m):
        if m.get("method") == "Page.javascriptDialogOpening":
            p = m["params"]
            console.print(f"[yellow]dialog[/yellow] {p.get('type')}: {p.get('message','')!r} "
                          f"→ [green]{verb}[/green]")
            c.call("Page.handleJavaScriptDialog",
                   {"accept": a.accept, "promptText": a.text or ""})

    try:
        c.call("Page.enable")
        for m in list(c.buf):
            handle(m)
        c.buf.clear()
        start = time.time()
        while True:
            if a.max and time.time() - start > a.max:
                break
            try:
                m = c.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if "__error__" in m:
                break
            handle(m)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
    finally:
        c.close()


def cmd_heapsnapshot(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("HeapProfiler.enable")
        chunks = []
        mid = c.send("HeapProfiler.takeHeapSnapshot", {"reportProgress": False})
        deadline = time.time() + 180
        got = False
        with console.status("[cyan]capturing heap snapshot…[/cyan]"):
            while time.time() < deadline:
                try:
                    m = c.q.get(timeout=1)
                except queue.Empty:
                    if got:
                        break
                    continue
                if "__error__" in m:
                    raise ConnectionError(m["__error__"])
                if m.get("method") == "HeapProfiler.addHeapSnapshotChunk":
                    chunks.append(m["params"]["chunk"])
                elif m.get("id") == mid:
                    got = True
                    deadline = time.time() + 1  # brief drain for trailing chunks
        data = "".join(chunks)
        out = a.out or f"heap-{int(time.time())}.heapsnapshot"
        with open(out, "w") as f:
            f.write(data)
        def render():
            console.print(f"[green]saved[/green] {len(data):,} bytes → {out}")
            console.print("[dim]load in Chrome DevTools ▸ Memory ▸ Load profile[/dim]")

        return emit(a, {"ok": True, "out": os.path.abspath(out), "bytes": len(data)}, render)
    finally:
        c.close()


# ---- Higher-level: read / extract / links (data) and wait / fill-form (flows) ----
def cmd_links(a):
    c, t = connect(a.host, a.port, a.target)
    js = r"""(() => { const seen=new Set(), out=[];
      for (const a of document.querySelectorAll('a[href]')) {
        if (seen.has(a.href)) continue; seen.add(a.href);
        out.push({text:(a.innerText||'').replace(/\s+/g,' ').trim().slice(0,100), href:a.href, rel:a.rel||''});
      } return {host: location.host, links: out}; })()"""
    try:
        r = c.call("Runtime.evaluate", {"expression": js, "returnByValue": True})["result"]["value"]
    finally:
        c.close()
    host, links = r["host"], r["links"]

    def internal(u):
        try:
            return urlsplit(u).netloc in ("", host)
        except Exception:
            return False
    if a.internal:
        links = [l for l in links if internal(l["href"])]
    elif a.external:
        links = [l for l in links if not internal(l["href"])]
    if a.json:
        out_json(links)
        return
    tbl = Table(header_style="bold cyan", title=f"{len(links)} links")
    tbl.add_column("text", max_width=45); tbl.add_column("href", max_width=70)
    for l in links:
        tbl.add_row(l["text"] or "—", l["href"])
    console.print(tbl)


def _parse_field(spec):
    if "=" not in spec:
        raise CDPError(f"bad --field {spec!r} — use name=selector[@attr][]")
    name, sel = spec.split("=", 1)
    attr, allm = None, False
    if "@" in sel:
        sel, attr = sel.rsplit("@", 1)
    if sel.endswith("[]"):
        allm, sel = True, sel[:-2]
    return {"name": name.strip(), "sel": sel.strip(), "attr": attr, "all": allm}


def cmd_extract(a):
    specs = [_parse_field(s) for s in a.field]
    js = (r"""(() => { const specs = __SPECS__;
      const g = (el, at) => at ? (el.getAttribute(at) ?? el[at] ?? null)
                                : ((el.innerText || el.value || '').replace(/\s+/g,' ').trim());
      const out = {};
      for (const s of specs) {
        if (s.all) out[s.name] = Array.from(document.querySelectorAll(s.sel)).map(e => g(e, s.attr));
        else { const e = document.querySelector(s.sel); out[s.name] = e ? g(e, s.attr) : null; }
      } return out; })()""").replace("__SPECS__", json.dumps(specs))
    c, t = connect(a.host, a.port, a.target)
    try:
        r = c.call("Runtime.evaluate", {"expression": js, "returnByValue": True})["result"]["value"]
    finally:
        c.close()
    out_json(r)          # extract is structured data → always JSON


def cmd_read(a):
    import re
    from readability import Document
    from markdownify import markdownify as mdify
    c, t = connect(a.host, a.port, a.target)
    try:
        html = c.call("Runtime.evaluate",
                      {"expression": "document.documentElement.outerHTML", "returnByValue": True})["result"]["value"]
        url = c.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})["result"]["value"]
    finally:
        c.close()
    doc = Document(html)
    title = doc.short_title()
    md = mdify(doc.summary(html_partial=True), heading_style="ATX").strip()
    md = re.sub(r"\n{3,}", "\n\n", md)
    if a.json:
        out_json({"url": url, "title": title, "markdown": md, "chars": len(md)})
        return
    if a.out:
        with open(a.out, "w") as f:
            f.write(f"# {title}\n\n{md}\n")
        console.print(f"[green]wrote[/green] {len(md):,} chars → {a.out}")
        return
    from rich.markdown import Markdown
    console.print(f"[bold]{title}[/bold]  [dim]{url}[/dim]\n")
    console.print(Markdown(md[:a.max]))
    if len(md) > a.max:
        console.print(f"[dim]… truncated ({len(md):,} chars). Use --out to save all, or --json.[/dim]")


def cmd_wait(a):
    import re
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    what = ""
    with sync_playwright() as p:
        b = _connect_pw(p, a.host, a.port)
        try:
            page = _pw_page_for(a, b)
            state = "hidden" if a.gone else "visible"
            try:
                if a.selector:
                    what = f"selector {a.selector!r}" + (" gone" if a.gone else "")
                    page.wait_for_selector(a.selector, state=state, timeout=a.timeout)
                elif a.text:
                    what = f"text {a.text!r}" + (" gone" if a.gone else "")
                    page.get_by_text(a.text).first.wait_for(state=state, timeout=a.timeout)
                elif a.url:
                    what = f"url ~ {a.url!r}"
                    page.wait_for_url(re.compile(re.escape(a.url)), timeout=a.timeout)
                elif a.network_idle:
                    what = "network idle"
                    page.wait_for_load_state("networkidle", timeout=a.timeout)
                else:
                    raise UserError("specify --selector / --text / --url / --network-idle", "bad-args")
            except PWTimeout:
                raise UserError(f"timeout waiting for {what}", "timeout")
            if a.json:
                out_json({"ok": True, "waited": what, "url": page.url})
            else:
                console.print(f"[green]ready[/green] {what}  [dim]({page.url})[/dim]")
        finally:
            b.close()


def cmd_fillform(a):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    fields = []
    for s in a.set:
        if "=" not in s:
            raise UserError(f"bad --set {s!r} — use selector=value", "bad-args")
        sel, val = s.split("=", 1)
        fields.append((sel, val))
    results, submitted, url = [], False, ""
    with sync_playwright() as p:
        b = _connect_pw(p, a.host, a.port)
        try:
            page = _pw_page_for(a, b)
            for sel, val in fields:
                try:
                    page.fill(sel, val, timeout=a.timeout)
                    results.append({"selector": sel, "ok": True})
                except PWTimeout:
                    results.append({"selector": sel, "ok": False})
            if a.submit:
                try:
                    page.click(a.submit, timeout=a.timeout)
                    submitted = True
                except PWTimeout:
                    results.append({"submit": a.submit, "ok": False})
            elif a.enter and fields:
                page.locator(fields[-1][0]).press("Enter")
                submitted = True
            page.wait_for_timeout(150)
            url = page.url
        finally:
            b.close()
    if a.json:
        out_json({"fields": results, "submitted": submitted, "url": url})
        return
    for r in results:
        if "selector" in r:
            console.print(f"{'[green]✓[/green]' if r['ok'] else '[red]✗[/red]'} {r['selector']}")
        else:
            console.print(f"[red]✗ submit {r['submit']}[/red]")
    if submitted:
        console.print(f"[green]submitted[/green] → {url}")


# ---- Tier 3: performance (Core Web Vitals + trace) and Lighthouse ----
VITALS_JS = r"""(() => {
  window.__v = {cls:0, lcp:0, fcp:0, ttfb:0, inp:0};
  const obs = (type, cb) => { try { new PerformanceObserver(l => { for (const e of l.getEntries()) cb(e); })
      .observe({type, buffered:true}); } catch (e) {} };
  obs('largest-contentful-paint', e => { window.__v.lcp = e.renderTime || e.startTime; });
  obs('layout-shift', e => { if (!e.hadRecentInput) window.__v.cls += e.value; });
  obs('paint', e => { if (e.name === 'first-contentful-paint') window.__v.fcp = e.startTime; });
  try { new PerformanceObserver(l => { for (const e of l.getEntries())
      if (e.duration > window.__v.inp) window.__v.inp = e.duration; })
      .observe({type:'event', buffered:true, durationThreshold:16}); } catch (e) {}
  window.__vitals = () => { const n = performance.getEntriesByType('navigation')[0];
    if (n) window.__v.ttfb = n.responseStart; return window.__v; };
})()"""

# metric -> (good_max, needs_max, unit) per web.dev thresholds
VITAL_THRESH = {
    "LCP": (2500, 4000, "ms"), "CLS": (0.1, 0.25, ""), "INP": (200, 500, "ms"),
    "FCP": (1800, 3000, "ms"), "TTFB": (800, 1800, "ms"),
}


def _scratch_target(a, hint):
    """A blank tab to work in, or the browser's own refusal plus the way around it.

    Opening a scratch tab is how the URL form of perf/capture/seo works. A browser
    with no tab model can't, and the useful thing to say then is not "it failed"
    but which flag reaches a window that is already open.
    """
    try:
        return new_target(a.host, a.port, "about:blank")
    except UserError as e:
        raise UserError(f"{e} {hint}", e.kind) from None


def _rate(metric, val):
    good, needs, _ = VITAL_THRESH[metric]
    if val <= good:
        return "green", "good"
    if val <= needs:
        return "yellow", "needs-improvement"
    return "red", "poor"


def cmd_perf(a):
    if a.attach:
        c, t = connect(a.host, a.port, a.attach)
    else:
        t = _scratch_target(a, "— use `--attach TARGET` to measure one.")
        c = CDP(_target_ws(t))
    trace_events = []
    do_trace = bool(a.out)
    try:
        c.call("Page.enable")
        # register the vitals collector so it runs before the page's own scripts
        c.call("Page.addScriptToEvaluateOnNewDocument", {"source": VITALS_JS})
        if do_trace:
            c.call("Tracing.start", {"transferMode": "ReportEvents",
                                     "traceConfig": {"recordMode": "recordUntilFull"}})
        url = a.attach and t.get("url") or a.url
        if not a.attach:
            c.call("Page.navigate", {"url": a.url})
        elif a.reload:
            c.call("Page.reload")

        loaded = any(m.get("method") == "Page.loadEventFired" for m in c.buf)
        c.buf.clear()
        deadline = time.time() + 20
        with console.status("[cyan]measuring…[/cyan]"):
            while not loaded and time.time() < deadline:
                try:
                    if c.q.get(timeout=0.3).get("method") == "Page.loadEventFired":
                        loaded = True
                except queue.Empty:
                    pass
            time.sleep(a.wait)   # let LCP/CLS settle

        v = c.call("Runtime.evaluate",
                   {"expression": "window.__vitals ? window.__vitals() : null",
                    "returnByValue": True})["result"].get("value") or {}

        if do_trace:
            mid = c.send("Tracing.end")
            done = False
            end = time.time() + 30
            while time.time() < end:
                try:
                    m = c.q.get(timeout=0.5)
                except queue.Empty:
                    if done:
                        break
                    continue
                if m.get("method") == "Tracing.dataCollected":
                    trace_events.extend(m["params"].get("value", []))
                elif m.get("method") == "Tracing.tracingComplete":
                    done = True
                    end = time.time() + 0.5
                elif m.get("id") == mid:
                    pass
    finally:
        if not a.attach:
            close_target(a.host, a.port, t["id"])
        c.close()

    console.print(Rule(f"[bold]Core Web Vitals[/bold] · {url}"))
    tbl = Table(header_style="bold cyan")
    tbl.add_column("metric"); tbl.add_column("value", justify="right"); tbl.add_column("rating")
    rows = [("LCP", v.get("lcp", 0)), ("CLS", v.get("cls", 0)), ("INP", v.get("inp", 0)),
            ("FCP", v.get("fcp", 0)), ("TTFB", v.get("ttfb", 0))]
    for metric, val in rows:
        color, label = _rate(metric, val)
        unit = VITAL_THRESH[metric][2]
        shown = f"{val:.3f}" if metric == "CLS" else f"{val:.0f}{unit}"
        note = "  [dim](needs interaction)[/dim]" if metric == "INP" and val == 0 else ""
        tbl.add_row(metric, shown, f"[{color}]{label}[/{color}]{note}")
    console.print(tbl)
    if do_trace:
        with open(a.out, "w") as f:
            json.dump({"traceEvents": trace_events}, f)
        console.print(f"[green]trace[/green] ({len(trace_events):,} events) → {a.out}  "
                      f"[dim](DevTools ▸ Performance ▸ Load profile)[/dim]")


def cmd_lighthouse(a):
    import shutil
    import subprocess
    lh = shutil.which("lighthouse")
    if not lh:
        nvm = os.path.expanduser("~/.nvm/versions/node")
        if os.path.isdir(nvm):
            for ver in sorted(os.listdir(nvm), reverse=True):
                cand = os.path.join(nvm, ver, "bin", "lighthouse")
                if os.path.exists(cand):
                    lh = cand
                    break
    if not lh:
        raise UserError("lighthouse CLI not found. Install it with:  npm i -g lighthouse", "missing-dep")

    cats = a.categories.split(",") if a.categories else None
    cmd = [lh, a.url, f"--port={a.port}", "--output=json", "--output-path=stdout",
           "--quiet", "--chrome-flags=--headless=new"]
    if cats:
        cmd.append("--only-categories=" + ",".join(cats))
    if a.preset == "desktop":
        cmd.append("--preset=desktop")
    node_bin = os.path.dirname(lh)
    env = dict(os.environ, PATH=node_bin + os.pathsep + os.environ.get("PATH", ""))
    with console.status("[cyan]running Lighthouse…[/cyan]"):
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    if p.returncode != 0 or not p.stdout.strip():
        raise UserError(f"lighthouse failed: {p.stderr[-800:]}", "tool-failed")
    report = json.loads(p.stdout)
    if a.out:
        with open(a.out, "w") as f:
            f.write(p.stdout)

    console.print(Rule(f"[bold]Lighthouse[/bold] · {report.get('finalUrl', a.url)}"))
    tbl = Table(header_style="bold cyan")
    tbl.add_column("category"); tbl.add_column("score", justify="right")
    for key, cat in report.get("categories", {}).items():
        score = cat.get("score")
        if score is None:
            pct, color = "n/a", "dim"
        else:
            pct = f"{round(score*100)}"
            color = "green" if score >= 0.9 else "yellow" if score >= 0.5 else "red"
        tbl.add_row(cat.get("title", key), f"[{color}]{pct}[/{color}]")
    console.print(tbl)

    audits = report.get("audits", {})
    metrics = [("first-contentful-paint", "FCP"), ("largest-contentful-paint", "LCP"),
               ("cumulative-layout-shift", "CLS"), ("total-blocking-time", "TBT"),
               ("speed-index", "Speed Index"), ("interactive", "TTI")]
    mt = Table(header_style="bold cyan", title="key metrics")
    mt.add_column("metric"); mt.add_column("value", justify="right")
    for key, label in metrics:
        au = audits.get(key)
        if au and au.get("displayValue"):
            mt.add_row(label, au["displayValue"])
    console.print(mt)
    if a.out:
        console.print(f"[green]full report[/green] → {a.out}")


# ---- Tier 2: Playwright-over-CDP interaction (lazy-imported) ----
SNAP_DIR = os.path.expanduser("~/.chromectl/snaps")


def _snap_file(a):
    """Where `snapshot` parks its refs: per instance, never in the user's CWD.

    A single shared file in the working directory both littered whatever repo an
    agent happened to be in and let two instances overwrite each other's refs.
    """
    os.makedirs(SNAP_DIR, exist_ok=True)
    return os.path.join(SNAP_DIR, f"{getattr(a, 'host', 'localhost')}-{getattr(a, 'port', 9222)}.json")

SNAPSHOT_JS = r"""() => {
  function cssPath(el){
    if(el.id) return '#'+CSS.escape(el.id);
    const parts=[];
    while(el && el.nodeType===1 && el!==document.body){
      let sel=el.nodeName.toLowerCase();
      const p=el.parentNode;
      if(p){const sibs=Array.from(p.children).filter(c=>c.nodeName===el.nodeName);
        if(sibs.length>1) sel+=':nth-of-type('+(sibs.indexOf(el)+1)+')';}
      parts.unshift(sel); el=el.parentNode;
    }
    return parts.length ? ('body > '+parts.join(' > ')) : 'body';
  }
  const sel='a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[contenteditable=""],[onclick]';
  const roleMap={A:'link',BUTTON:'button',INPUT:'textbox',SELECT:'combobox',TEXTAREA:'textbox'};
  const seen=new Set(), out=[];
  for(const e of document.querySelectorAll(sel)){
    const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    if(r.width<=0||r.height<=0||s.visibility==='hidden'||s.display==='none') continue;
    const role=e.getAttribute('role')||roleMap[e.nodeName]||e.nodeName.toLowerCase();
    const name=(e.getAttribute('aria-label')||e.innerText||e.value||e.placeholder||e.getAttribute('title')||'')
      .replace(/\s+/g,' ').trim().slice(0,70);
    const path=cssPath(e);
    if(seen.has(path)) continue; seen.add(path);
    out.push({role, name, selector: path});
    if(out.length>=200) break;
  }
  return out;
}"""


def _connect_pw(p, host, port):
    return p.chromium.connect_over_cdp(f"http://{host}:{port}")


def _find_page(browser, match):
    pages = [pg for c in browser.contexts for pg in c.pages]
    if not pages:
        raise CDPError("no pages open")
    if not match:
        cand = [pg for pg in pages if not pg.url.startswith(("chrome://", "devtools://"))]
        return (cand or pages)[0]
    ml = match.lower()
    for pg in pages:
        try:
            title = pg.title()
        except Exception:
            title = ""
        if ml in pg.url.lower() or ml in (title or "").lower():
            return pg
    raise CDPError(f"no page matching {match!r}")


def _locate(page, a):
    """Return (locator, human-description) from --ref/--selector/--text/--role[--name]."""
    if getattr(a, "ref", None) is not None:
        snap = _snap_file(a)
        if not os.path.exists(snap):
            raise UserError("no saved snapshot — run `chromectl snapshot` first", "no-snapshot")
        data = json.load(open(snap))
        els = data.get("elements", [])
        if not (0 <= a.ref < len(els)):
            raise UserError(f"--ref {a.ref} out of range (0..{len(els)-1})", "bad-args")
        e = els[a.ref]
        return page.locator(e["selector"]).first, f"ref {a.ref} ({e['role']} {e['name']!r})"
    if getattr(a, "selector", None):
        return page.locator(a.selector).first, f"selector {a.selector!r}"
    if getattr(a, "role", None):
        kw = {"name": a.name} if getattr(a, "name", None) else {}
        return page.get_by_role(a.role, **kw).first, f"role={a.role} name={getattr(a,'name',None)!r}"
    if getattr(a, "text", None):
        return page.get_by_text(a.text).first, f"text {a.text!r}"
    raise UserError("specify a target element: --ref N | --selector CSS | --text STR | "
                    "--role ROLE [--name STR]", "bad-args")


def cmd_snapshot(a):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = _connect_pw(p, a.host, a.port)
        try:
            page = _pw_page_for(a, b)
            els = page.evaluate(SNAPSHOT_JS)
            url = page.url
        finally:
            b.close()
    out = a.out or _snap_file(a)
    with open(out, "w") as f:
        json.dump({"url": url, "elements": els}, f, indent=2)
    for i, e in enumerate(els):
        e["ref"] = i

    def render():
        tbl = Table(title=f"interactive elements · {url[:60]}", header_style="bold cyan")
        tbl.add_column("#", justify="right"); tbl.add_column("role")
        tbl.add_column("name", max_width=48); tbl.add_column("selector", max_width=38, style="dim")
        for e in els:
            tbl.add_row(str(e["ref"]), e["role"], e["name"] or "—", e["selector"])
        console.print(tbl)
        console.print(f"[dim]{len(els)} elements → refs saved to {out}. "
                      f"Use e.g. `chromectl click {a.target or ''} --ref 0`[/dim]")

    return emit(a, {"ok": True, "url": url, "count": len(els),
                    "refs": out, "elements": els}, render)


def cmd_click(a):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    with sync_playwright() as p:
        b = _connect_pw(p, a.host, a.port)
        try:
            page = _pw_page_for(a, b)
            loc, desc = _locate(page, a)
            try:
                loc.click(timeout=a.timeout)
            except PWTimeout:
                raise UserError(f"click timed out on {desc} (not found / not visible / "
                                f"not actionable within {int(a.timeout)}ms)", "timeout")
            page.wait_for_timeout(150)

            def render():
                console.print(f"[green]clicked[/green] {desc}")
                console.print(f"[dim]url now: {page.url}[/dim]")

            return emit(a, {"ok": True, "clicked": desc, "url": page.url}, render)
        finally:
            b.close()


def cmd_fill(a):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    value = " ".join(a.value)
    with sync_playwright() as p:
        b = _connect_pw(p, a.host, a.port)
        try:
            page = _pw_page_for(a, b)
            loc, desc = _locate(page, a)
            try:
                loc.fill(value, timeout=a.timeout)
            except PWTimeout:
                raise UserError(f"fill timed out on {desc}", "timeout")
            if a.enter:
                loc.press("Enter")
            return emit(a, {"ok": True, "filled": desc, "value": value, "enter": bool(a.enter),
                            "url": page.url},
                        lambda: console.print(f"[green]filled[/green] {desc} = {value!r}"
                                              + (" + Enter" if a.enter else "")))
        finally:
            b.close()


def cmd_hover(a):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    with sync_playwright() as p:
        b = _connect_pw(p, a.host, a.port)
        try:
            page = _pw_page_for(a, b)
            loc, desc = _locate(page, a)
            try:
                loc.hover(timeout=a.timeout)
            except PWTimeout:
                raise UserError(f"hover timed out on {desc}", "timeout")
            return emit(a, {"ok": True, "hovered": desc, "url": page.url},
                        lambda: console.print(f"[green]hovered[/green] {desc}"))
        finally:
            b.close()


def cmd_run(a):
    """Run a sequence of chromectl steps in ONE process over pooled connections.

    Steps come from --step (repeatable), a FILE, or stdin. One opened tab becomes
    the implicit target for later steps. State persists across steps.

    With --json every step runs in its own --json mode and the whole batch comes
    back as one array, so a caller can read what each step actually returned
    rather than scraping a rendered transcript.
    """
    global _RUN_ACTIVE
    if a.step:
        raw = a.step
    else:
        src = sys.stdin if (not a.file or a.file == "-") else open(a.file)
        raw = list(src)
    steps = [s.strip() for s in raw if s.strip() and not s.strip().startswith("#")]
    if not steps:
        raise UserError("no steps given (use --step, a FILE, or stdin)", "bad-args")

    agent = getattr(a, "json", False)
    parser = build_parser()
    _RUN_ACTIVE = True
    current = a.target or ""
    results, failed = [], 0
    try:
        for i, line in enumerate(steps, 1):
            if not agent:
                console.print(Rule(f"[dim]step {i}/{len(steps)}[/dim] [cyan]{escape_rule(line)}[/cyan]",
                                   align="left"))
            tokens = shlex.split(line)
            sa = None
            with contextlib.redirect_stderr(io.StringIO()):   # hush argparse's tentative errors
                try:
                    sa = parser.parse_args(tokens)
                except SystemExit:
                    # maybe a required target is missing — retry with the current tab injected
                    if current and tokens:
                        try:
                            sa = parser.parse_args([tokens[0], current] + tokens[1:])
                        except SystemExit:
                            sa = None
            if sa is None:
                msg = f"could not parse {line!r}"
                results.append({"step": i, "cmd": line, "ok": False,
                                "error": {"kind": "bad-args", "message": msg}})
                failed += 1
                if not agent:
                    err.print(f"step {i}: {msg}")
                if not a.keep_going:
                    break
                continue
            sa.host, sa.port = a.host, a.port
            if hasattr(sa, "target") and not getattr(sa, "target") and current:
                sa.target = current          # implicit current tab (optional-target commands)
            if agent and hasattr(sa, "json"):
                sa.json = True               # every step speaks JSON inside a --json run
            try:
                buf = io.StringIO()
                # a step's own rendering is captured, never interleaved with the batch JSON
                with contextlib.redirect_stdout(buf) if agent else contextlib.nullcontext():
                    res = sa.fn(sa)
                if sa.fn is cmd_open and isinstance(res, dict):
                    current = res.get("id", current)   # opened tab → implicit target
                if agent:
                    results.append({"step": i, "cmd": line, "ok": True,
                                    "result": _step_payload(buf.getvalue(), res)})
            except (UserError, CDPError, TimeoutError, ConnectionError, OSError, ValueError) as e:
                kind = getattr(e, "kind", type(e).__name__.lower().replace("error", "") or "error")
                results.append({"step": i, "cmd": line, "ok": False,
                                "error": {"kind": kind, "message": str(e)}})
                failed += 1
                if not agent:
                    err.print(f"step {i} failed: {e}")
                if not a.keep_going:
                    break
    finally:
        _RUN_ACTIVE = False
        for c in _RUN_POOL.values():
            c._pooled = False
            c.close()
        _RUN_POOL.clear()
    if agent:
        out_json({"ok": failed == 0, "steps": len(steps), "failed": failed,
                  "results": results})
    elif failed:
        console.print(f"[red]{failed} step(s) failed[/red]")
    else:
        console.print(f"[green]done[/green] · {len(steps)} steps")
    if failed:
        sys.exit(1)
    return results


def _step_payload(captured, returned):
    """Best-effort structured result for one step of a --json run."""
    text = captured.strip()
    if text:
        try:
            return json.loads(text)
        except ValueError:
            return {"output": text}
    if isinstance(returned, (dict, list, str, int, float, bool)) or returned is None:
        return returned
    return {"output": str(returned)}


def escape_rule(s):
    from rich.markup import escape
    return escape(s[:100])


def cmd_raw(a):
    is_browser = a.target == "browser"
    c, t = connect(a.host, a.port, a.target, require_page=not is_browser)
    try:
        params = json.loads(a.params) if a.params else {}
        r = c.call(a.method, params)
        console.print(JSON(json.dumps(r)))
    except json.JSONDecodeError as e:
        raise UserError(f"bad JSON params: {e}", "bad-args")
    finally:
        c.close()


def cmd_repl(a):
    c, t = connect(a.host, a.port, a.target, require_page=(a.target != "browser"))
    console.print(Panel(
        "Interactive CDP. Type: [cyan]Domain.method[/cyan] [dim]{\"json\": \"params\"}[/dim]\n"
        "Commands: [cyan].targets  .help  .quit[/cyan]   (Ctrl-D to exit)",
        title=f"repl → {t.get('url', t['id'])}", border_style="cyan"))
    try:
        while True:
            try:
                line = console.input("[bold green]cdp>[/bold green] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if line in (".quit", ".q", ".exit"):
                break
            if line == ".targets":
                for tt in list_targets(a.host, a.port):
                    console.print(f"  [dim]{tt['id'][:16]}…[/dim] {tt['type']:12} {tt.get('url','')[:60]}")
                continue
            if line == ".help":
                console.print("  <Domain.method> [params-json]  e.g.  Runtime.evaluate {\"expression\":\"1+1\",\"returnByValue\":true}")
                continue
            parts = line.split(None, 1)
            method = parts[0]
            try:
                params = json.loads(parts[1]) if len(parts) > 1 else {}
            except json.JSONDecodeError as e:
                err.print(f"bad JSON: {e}")
                continue
            try:
                r = c.call(method, params)
                console.print(JSON(json.dumps(r)))
            except (CDPError, TimeoutError, ConnectionError) as e:
                err.print(str(e))
    finally:
        c.close()


def cmd_proto(a):
    proto = get_protocol(a.host, a.port)["domains"]
    q = a.query

    def typ(x):
        if "$ref" in x:
            return x["$ref"]
        if x.get("type") == "array":
            it = x.get("items", {})
            return (it.get("$ref") or it.get("type", "?")) + "[]"
        return x.get("type", "?")

    def show_params(items, label):
        if not items:
            return
        console.print(f"  [bold]{label}:[/bold]")
        for p in items:
            opt = " [dim](optional)[/dim]" if p.get("optional") else ""
            console.print(f"    [cyan]{p['name']}[/cyan]: [yellow]{typ(p)}[/yellow]{opt}")
            d = p.get("description", "").replace("\n", " ")
            if d:
                console.print(f"        [dim]{d[:110]}[/dim]")

    if not q:  # list all domains
        tbl = Table(title="CDP domains", header_style="bold cyan")
        tbl.add_column("domain"); tbl.add_column("cmds", justify="right")
        tbl.add_column("events", justify="right")
        for d in proto:
            tbl.add_row(d["domain"], str(len(d.get("commands", []))), str(len(d.get("events", []))))
        console.print(tbl)
        return
    if "." in q:
        dom, name = q.split(".", 1)
        d = next((x for x in proto if x["domain"] == dom), None)
        if not d:
            err.print(f"no domain {dom}"); sys.exit(1)
        cmd = next((c for c in d.get("commands", []) if c["name"] == name), None)
        ev = next((e for e in d.get("events", []) if e["name"] == name), None)
        item, kind = (cmd, "COMMAND") if cmd else (ev, "EVENT")
        if not item:
            err.print(f"no command/event {q}"); sys.exit(1)
        console.print(f"[bold magenta]{kind}[/bold magenta] [bold]{q}[/bold]")
        if item.get("description"):
            console.print(f"  [dim]{item['description'].replace(chr(10),' ')[:200]}[/dim]")
        show_params(item.get("parameters"), "parameters")
        if kind == "COMMAND":
            show_params(item.get("returns"), "returns")
    else:
        d = next((x for x in proto if x["domain"] == q), None)
        if not d:
            err.print(f"no domain {q}"); sys.exit(1)
        console.print(f"[bold]DOMAIN {q}[/bold]")
        console.print("  [bold]commands:[/bold] " + ", ".join(c["name"] for c in d.get("commands", [])))
        console.print("  [bold]events:[/bold]   " + ", ".join(e["name"] for e in d.get("events", [])))


# --------------------------------------------------------------------------
# network capture (the Burp-style workhorse)
# --------------------------------------------------------------------------
def _fmt_headers(h):
    return "\n".join(f"{k}: {v}" for k, v in (h or {}).items())


def _record_to_raw(r, bodycap):
    from urllib.parse import urlsplit as us
    req = r.get("req", {})
    u = us(req.get("url", "http://?"))
    reqH = r.get("reqRawHeaders") or req.get("headers") or {}
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    out = ["==================== REQUEST ===================="]
    out.append(f"{req.get('method','?')} {path} HTTP/2")
    out.append(f"Host: {u.netloc}")
    out.append(_fmt_headers({k: v for k, v in reqH.items() if k.lower() != "host"}))
    if req.get("postData"):
        out.append("\n" + req["postData"][:bodycap])
    resp = r.get("resp", {})
    respH = r.get("respRawHeaders") or resp.get("headers") or {}
    out.append("\n==================== RESPONSE ====================")
    out.append(f"HTTP/2 {r.get('statusCode') or resp.get('status','?')}")
    out.append(_fmt_headers(respH))
    out.append("--- body ---")
    if r.get("bodyErr"):
        out.append(f"[body unavailable: {r['bodyErr']}]")
    elif r.get("b64"):
        out.append(f"[binary body, ~{int(len(r.get('body',''))*3/4)} bytes, omitted]")
    elif r.get("body") is not None:
        body = r["body"]
        if "json" in (resp.get("mimeType", "")).lower():
            try:
                body = json.dumps(json.loads(body), indent=2)
            except Exception:
                pass
        trunc = len(body) > bodycap
        out.append(body[:bodycap] + (f"\n… [truncated, {len(body)} chars]" if trunc else ""))
    return "\n".join(out)


def _build_har(records, page_url):
    entries = []
    for r in records:
        req = r.get("req", {})
        resp = r.get("resp", {})
        reqH = r.get("reqRawHeaders") or req.get("headers") or {}
        respH = r.get("respRawHeaders") or resp.get("headers") or {}
        body = r.get("body")
        content = {"size": len(body) if body else 0,
                   "mimeType": resp.get("mimeType", "")}
        if body is not None:
            content["text"] = body
            if r.get("b64"):
                content["encoding"] = "base64"
        entries.append({
            "startedDateTime": r.get("wallTime", ""),
            "time": 0,
            "request": {
                "method": req.get("method", ""),
                "url": req.get("url", ""),
                "httpVersion": "HTTP/2",
                "headers": [{"name": k, "value": str(v)} for k, v in reqH.items()],
                "queryString": [], "cookies": [],
                "headersSize": -1,
                "bodySize": len(req.get("postData", "")) if req.get("postData") else 0,
                "postData": ({"mimeType": reqH.get("content-type", ""),
                              "text": req["postData"]} if req.get("postData") else None),
            },
            "response": {
                "status": r.get("statusCode") or resp.get("status", 0),
                "statusText": resp.get("statusText", ""),
                "httpVersion": "HTTP/2",
                "headers": [{"name": k, "value": str(v)} for k, v in respH.items()],
                "cookies": [], "content": content,
                "redirectURL": respH.get("location", ""),
                "headersSize": -1, "bodySize": content["size"],
            },
            "cache": {},
            "timings": {"send": 0, "wait": 0, "receive": 0},
        })
    return {"log": {"version": "1.2",
                    "creator": {"name": "cdp.py", "version": "1.0"},
                    "pages": [{"startedDateTime": "", "id": "page_1",
                               "title": page_url, "pageTimings": {}}],
                    "entries": entries}}


def cmd_capture(a):
    if a.attach:
        c, t = connect(a.host, a.port, a.attach)
        page_url = t.get("url", "")
        console.print(f"[cyan]attached to[/cyan] {page_url}")
    else:
        t = _scratch_target(a, "— use `--attach TARGET` to capture one.")
        c = CDP(_target_ws(t))
        page_url = a.url

    records = {}          # requestId -> record
    body_reqs = {}        # getResponseBody call id -> requestId
    loaded = {"v": a.attach and not a.reload}   # in attach mode w/o reload, don't wait for load
    fetch_bodies = not a.no_bodies

    def rec(rid):
        return records.setdefault(rid, {})

    def handle(m):
        if "__error__" in m:
            raise ConnectionError(m["__error__"])
        mid = m.get("id")
        if mid is not None:                     # a response (to a body fetch)
            rid = body_reqs.pop(mid, None)
            if rid and "result" in m:
                r = records.get(rid)
                if r:
                    r["body"] = m["result"]["body"]
                    r["b64"] = m["result"]["base64Encoded"]
            elif rid:
                records.get(rid, {})["bodyErr"] = m.get("error", {}).get("message", "?")
            return
        method = m.get("method"); p = m.get("params", {})
        if method == "Network.requestWillBeSent":
            r = rec(p["requestId"]); r["req"] = p["request"]; r["type"] = p.get("type")
            r["wallTime"] = p.get("wallTime")
        elif method == "Network.requestWillBeSentExtraInfo":
            rec(p["requestId"])["reqRawHeaders"] = p.get("headers")
        elif method == "Network.responseReceived":
            r = rec(p["requestId"]); r["resp"] = p["response"]
        elif method == "Network.responseReceivedExtraInfo":
            r = rec(p["requestId"]); r["respRawHeaders"] = p.get("headers")
            r["statusCode"] = p.get("statusCode")
        elif method == "Network.loadingFinished":
            if fetch_bodies:
                bid = c.send("Network.getResponseBody", {"requestId": p["requestId"]})
                body_reqs[bid] = p["requestId"]
        elif method == "Network.loadingFailed":
            r = rec(p["requestId"]); r["bodyErr"] = p.get("errorText", "failed")
        elif method == "Page.loadEventFired":
            loaded["v"] = True

    try:
        c.call("Network.enable")
        c.call("Page.enable")
        if not a.attach:
            c.call("Page.navigate", {"url": a.url})
        elif a.reload:
            c.call("Page.reload")

        # drain events already buffered during the setup calls
        for m in c.buf:
            handle(m)
        c.buf.clear()

        start = time.time(); last = time.time()
        with console.status("[cyan]capturing…[/cyan]") as status:
            while True:
                try:
                    m = c.q.get(timeout=0.3)
                    handle(m); last = time.time()
                    status.update(f"[cyan]capturing… {len(records)} requests[/cyan]")
                except queue.Empty:
                    pass
                elapsed = time.time() - start
                idle = time.time() - last
                done = loaded["v"] and not body_reqs and idle > a.quiet and records
                if done or elapsed > a.max:
                    break
    finally:
        rows = [r for r in records.values() if r.get("req")]
        if not a.attach:
            close_target(a.host, a.port, t["id"])
        c.close()

    # filter
    if a.type:
        tl = a.type.lower()
        rows = [r for r in rows if tl in (r.get("type", "") or "").lower()]

    # summary
    by_type, by_status = {}, {}
    for r in rows:
        by_type[r.get("type", "?")] = by_type.get(r.get("type", "?"), 0) + 1
        s = r.get("statusCode") or r.get("resp", {}).get("status", "-")
        by_status[s] = by_status.get(s, 0) + 1
    agent = getattr(a, "json", False)
    if not agent:
        console.print(Rule(f"[bold]{len(rows)} transactions[/bold] for {page_url}"))
        console.print("  by type:   " + "  ".join(f"{k}={v}" for k, v in by_type.items()))
        console.print("  by status: " + "  ".join(f"{k}={v}" for k, v in by_status.items()))
        # render to console
        for r in rows[:a.print]:
            req = r.get("req", {})
            console.print(Rule(f"[green]{req.get('method','?')}[/green] {req.get('url','')[:80]}",
                               style="dim"))
            console.print(_record_to_raw(r, a.bodycap))

    # save raw
    if a.out:
        with open(a.out, "w") as f:
            f.write("\n\n".join(_record_to_raw(r, 10 ** 9) for r in rows))
        if not agent:
            console.print(f"[green]raw dump[/green] ({len(rows)}) → {a.out}")
    # save HAR
    if a.har:
        with open(a.har, "w") as f:
            json.dump(_build_har(rows, page_url), f, indent=2)
        if not agent:
            console.print(f"[green]HAR[/green] → {a.har}  "
                          f"(import into DevTools ▸ Network, or Burp)")
    if agent:
        # bodies are dropped here on purpose: --out/--har carry them, and a full
        # capture inlined into a tool result is nearly always too big to be useful
        out_json({"ok": True, "url": page_url, "count": len(rows),
                  "by_type": by_type, "by_status": {str(k): v for k, v in by_status.items()},
                  "out": os.path.abspath(a.out) if a.out else None,
                  "har": os.path.abspath(a.har) if a.har else None,
                  "transactions": [{"method": r.get("req", {}).get("method"),
                                    "url": r.get("req", {}).get("url"),
                                    "type": r.get("type"),
                                    "status": r.get("statusCode") or r.get("resp", {}).get("status"),
                                    "mime": r.get("resp", {}).get("mimeType"),
                                    "bytes": len(r.get("body") or "")} for r in rows]})
    return rows


SEO_JS = r"""(() => {
  const q = s => document.querySelector(s);
  const qa = s => Array.from(document.querySelectorAll(s));
  const meta = n => { const e = q('meta[name="'+n+'"]'); return e ? e.content : null; };
  const prop = n => { const e = q('meta[property="'+n+'"]'); return e ? e.content : null; };
  const title = (document.title || '').trim();
  const desc = meta('description');
  const canonical = (q('link[rel="canonical"]') || {}).href || null;
  const robots = meta('robots');
  const viewport = meta('viewport');
  const lang = document.documentElement.lang || null;
  const h1 = qa('h1').map(e => (e.innerText || '').trim()).filter(Boolean);
  const headings = { h1: qa('h1').length, h2: qa('h2').length, h3: qa('h3').length };
  const imgs = qa('img');
  const imgsNoAlt = imgs.filter(i => !i.getAttribute('alt')).length;
  const text = document.body ? (document.body.innerText || '') : '';
  const words = (text.match(/\S+/g) || []).length;
  const links = qa('a[href]');
  let internal = 0, external = 0, nofollow = 0;
  for (const a of links) {
    try { const u = new URL(a.href, location.href); (u.host === location.host ? internal++ : external++); } catch (e) {}
    if (((a.rel || '') + '').includes('nofollow')) nofollow++;
  }
  const og = { title: prop('og:title'), description: prop('og:description'),
               image: prop('og:image'), type: prop('og:type'), url: prop('og:url') };
  const twitter = { card: meta('twitter:card'), title: meta('twitter:title'), image: meta('twitter:image') };
  const hreflang = qa('link[rel="alternate"][hreflang]').map(l => ({ lang: l.hreflang, href: l.href }));
  const ld = qa('script[type="application/ld+json"]').map(s => {
    try { const j = JSON.parse(s.textContent);
      return Array.isArray(j) ? j.map(x => x['@type']) : (j['@type'] || '?'); }
    catch (e) { return 'PARSE_ERROR'; }
  });
  return { url: location.href, title, titleLen: title.length, desc, descLen: desc ? desc.length : 0,
    canonical, robots, viewport, charset: document.characterSet, lang, h1, headings,
    images: imgs.length, imgsNoAlt, words, links: links.length, internal, external, nofollow,
    og, twitter, hreflang, jsonld: ld.flat() };
})()"""


def cmd_seo(a):
    opened = False
    if a.target.startswith("http://") or a.target.startswith("https://"):
        t = _scratch_target(a, "— pass a target selector instead of a URL.")
        c = CDP(_target_ws(t)); opened = True
        c.call("Page.enable"); c.call("Page.navigate", {"url": a.target})
        deadline = time.time() + 15
        loaded = any(m.get("method") == "Page.loadEventFired" for m in c.buf); c.buf.clear()
        while not loaded and time.time() < deadline:
            try:
                if c.q.get(timeout=0.3).get("method") == "Page.loadEventFired":
                    loaded = True
            except queue.Empty:
                pass
        time.sleep(0.5)
    else:
        c, t = connect(a.host, a.port, a.target)
    try:
        r = c.call("Runtime.evaluate", {"expression": SEO_JS, "returnByValue": True})["result"]["value"]
    finally:
        if opened:
            close_target(a.host, a.port, t["id"])
        c.close()

    if a.json:
        console.print(JSON(json.dumps(r)))
        return

    def status(ok, warn=False):
        return "[green]✓[/green]" if ok else ("[yellow]⚠[/yellow]" if warn else "[red]✗[/red]")

    console.print(Rule(f"[bold]SEO audit[/bold] · {r['url']}"))
    tbl = Table(header_style="bold cyan", show_lines=False)
    tbl.add_column(""); tbl.add_column("check"); tbl.add_column("value", max_width=70)
    title_ok = 30 <= r["titleLen"] <= 60
    tbl.add_row(status(r["titleLen"] > 0, warn=not title_ok),
                "title", f"{r['title']!r} ({r['titleLen']} chars)")
    desc_ok = 70 <= r["descLen"] <= 160
    tbl.add_row(status(r["descLen"] > 0, warn=not desc_ok),
                "meta description", (f"{r['desc'][:80]!r} ({r['descLen']} chars)" if r["desc"] else "[red]missing[/red]"))
    tbl.add_row(status(bool(r["canonical"])), "canonical", r["canonical"] or "[red]missing[/red]")
    noindex = (r["robots"] or "").lower().find("noindex") >= 0
    tbl.add_row(status(not noindex, warn=noindex), "robots meta",
                (f"[red]{r['robots']}[/red]" if noindex else (r["robots"] or "(none = indexable)")))
    tbl.add_row(status(bool(r["viewport"])), "viewport", r["viewport"] or "[red]missing[/red]")
    tbl.add_row(status(bool(r["lang"])), "html lang", r["lang"] or "[yellow]not set[/yellow]")
    tbl.add_row(status(r["headings"]["h1"] == 1, warn=r["headings"]["h1"] != 1),
                "H1", f"{r['headings']['h1']}× " + (f"→ {r['h1'][0][:60]!r}" if r["h1"] else "") +
                f"   (h2={r['headings']['h2']}, h3={r['headings']['h3']})")
    alt_ok = r["imgsNoAlt"] == 0
    tbl.add_row(status(alt_ok, warn=not alt_ok), "image alt",
                f"{r['images'] - r['imgsNoAlt']}/{r['images']} have alt"
                + (f"  [yellow]{r['imgsNoAlt']} missing[/yellow]" if r["imgsNoAlt"] else ""))
    thin = r["words"] < 300
    tbl.add_row(status(not thin, warn=thin), "word count",
                f"{r['words']} words" + ("  [yellow](thin)[/yellow]" if thin else ""))
    tbl.add_row("", "links", f"{r['links']} total · {r['internal']} internal · {r['external']} external · {r['nofollow']} nofollow")
    og_ok = bool(r["og"]["title"] and r["og"]["description"] and r["og"]["image"])
    tbl.add_row(status(og_ok, warn=not og_ok), "Open Graph",
                "title/desc/image " + ("all present" if og_ok else "incomplete") +
                (f"  img={r['og']['image'][:40]}" if r["og"]["image"] else ""))
    tw_ok = bool(r["twitter"]["card"])
    tbl.add_row(status(tw_ok, warn=not tw_ok), "Twitter card", r["twitter"]["card"] or "[yellow]none[/yellow]")
    sd_ok = bool(r["jsonld"])
    tbl.add_row(status(sd_ok, warn=not sd_ok), "structured data (JSON-LD)",
                (", ".join(map(str, r["jsonld"])) if r["jsonld"] else "[yellow]none[/yellow]"))
    if r["hreflang"]:
        tbl.add_row("[green]✓[/green]", "hreflang", ", ".join(h["lang"] for h in r["hreflang"]))
    console.print(tbl)


def cmd_console(a):
    """Tail console.* messages, JS exceptions, and browser log entries."""
    c, t = connect(a.host, a.port, a.target)
    lvl_color = {"log": "white", "info": "cyan", "debug": "dim", "warning": "yellow",
                 "error": "red", "verbose": "dim"}
    quiet = getattr(a, "json", False)
    collected = []

    def render_arg(arg):
        if "value" in arg:
            return json.dumps(arg["value"]) if isinstance(arg["value"], (dict, list)) else str(arg["value"])
        return arg.get("description", arg.get("type", "?"))

    if not quiet:
        console.print(Panel(f"console on {t.get('url', t['id'])}  —  Ctrl-C to stop",
                            border_style="cyan"))
    try:
        c.call("Runtime.enable")
        c.call("Log.enable")
        # process anything already buffered from the enable calls (Log replays entries)
        pending = list(c.buf); c.buf.clear()
        started = time.time()
        while True:
            for m in pending:
                method = m.get("method"); p = m.get("params", {})
                if method == "Runtime.consoleAPICalled":
                    typ = p.get("type", "log")
                    text = " ".join(render_arg(x) for x in p.get("args", []))
                    collected.append({"kind": "console", "level": typ, "text": text})
                    if not quiet:
                        color = lvl_color.get(typ, "white")
                        console.print(f"[{color}]console.{typ}[/{color}] {text}")
                elif method == "Runtime.exceptionThrown":
                    ex = p.get("exceptionDetails", {})
                    desc = ex.get("exception", {}).get("description") or ex.get("text", "")
                    collected.append({"kind": "exception", "level": "error", "text": desc})
                    if not quiet:
                        console.print(f"[bold red]UNCAUGHT[/bold red] "
                                      f"{desc.splitlines()[0] if desc else ''}")
                elif method == "Log.entryAdded":
                    e = p.get("entry", {})
                    collected.append({"kind": "log", "level": e.get("level", "log"),
                                      "source": e.get("source", ""), "text": e.get("text", ""),
                                      "url": e.get("url", "")})
                    if not quiet:
                        color = lvl_color.get(e.get("level", "log"), "white")
                        console.print(f"[{color}]{e.get('source','')}/{e.get('level','')}[/{color}] "
                                      f"{e.get('text','')}  [dim]{e.get('url','')}[/dim]")
            pending = []
            if a.max and time.time() - started > a.max:
                break
            try:
                pending = [c.q.get(timeout=0.5)]
            except queue.Empty:
                continue
            if pending and "__error__" in pending[0]:
                break
    except KeyboardInterrupt:
        if not quiet:
            console.print("\n[dim]stopped[/dim]")
    finally:
        c.close()
    if quiet:
        out_json({"ok": True, "url": t.get("url", ""), "count": len(collected),
                  "messages": collected})
    return collected


def cmd_watch(a):
    """Tail network activity.

    Bounded by --max seconds so an agent can call it without hanging forever;
    with --json the whole tail comes back as one array at the end.
    """
    c, t = connect(a.host, a.port, a.target)
    quiet = getattr(a, "json", False)
    reqs, records = {}, []
    if not quiet:
        stop = f"{a.max}s" if a.max else "Ctrl-C"
        console.print(Panel(f"live network on {t.get('url', t['id'])}  —  {stop} to stop",
                            border_style="cyan"))
    try:
        c.call("Network.enable")
        c.buf.clear()
        started = time.time()
        while True:
            if a.max and time.time() - started > a.max:
                break
            try:
                m = c.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if "__error__" in m:
                break
            method = m.get("method"); p = m.get("params", {})
            if method == "Network.requestWillBeSent":
                reqs[p["requestId"]] = p["request"]
            elif method == "Network.responseReceived":
                resp = p["response"]; rtype = p.get("type", "")
                status = resp.get("status", 0)
                records.append({"status": status, "type": rtype, "url": resp.get("url", ""),
                                "mime": resp.get("mimeType", "")})
                if not quiet:
                    color = "green" if status < 300 else "yellow" if status < 400 else "red"
                    console.print(f"[{color}]{status}[/{color}] [dim]{rtype:11}[/dim] "
                                  f"{resp.get('url','')[:90]}")
            elif method == "Network.loadingFailed":
                url = reqs.get(p["requestId"], {}).get("url", "")
                records.append({"status": None, "type": p.get("type", ""), "url": url,
                                "error": p.get("errorText", "")})
                if not quiet:
                    console.print(f"[red]ERR[/red] [dim]{p.get('type',''):11}[/dim] "
                                  f"{url[:90]} [red]{p.get('errorText','')}[/red]")
    except KeyboardInterrupt:
        if not quiet:
            console.print("\n[dim]stopped[/dim]")
    finally:
        c.close()
    if quiet:
        out_json({"ok": True, "url": t.get("url", ""), "count": len(records),
                  "requests": records})
    return records


# --------------------------------------------------------------------------
# navigation history, storage, auth state, a11y, interception, downloads
# --------------------------------------------------------------------------
def _history_move(a, delta):
    """Step the tab's navigation history by delta entries (-1 back, +1 forward)."""
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("Page.enable")
        h = c.call("Page.getNavigationHistory")
        idx, entries = h["currentIndex"], h["entries"]
        want = idx + delta
        if not (0 <= want < len(entries)):
            where = "back" if delta < 0 else "forward"
            raise UserError(f"no history to go {where}", "no-history")
        target_entry = entries[want]
        c.call("Page.navigateToHistoryEntry", {"entryId": target_entry["id"]})
        time.sleep(0.3)
        url = target_entry.get("url", "")
        word = "back" if delta < 0 else "forward"
        return emit(a, {"ok": True, "url": url, "index": want, "entries": len(entries)},
                    lambda: console.print(f"[green]{word}[/green] → {url}"))
    finally:
        c.close()


def cmd_back(a):
    return _history_move(a, -1)


def cmd_forward(a):
    return _history_move(a, +1)


def cmd_reload(a):
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("Page.enable")
        c.call("Page.reload", {"ignoreCache": bool(a.hard)})
        loaded = False
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            try:
                m = c.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if m.get("method") == "Page.loadEventFired":
                loaded = True
                break
        return emit(a, {"ok": True, "url": t.get("url", ""), "hard": bool(a.hard),
                        "loaded": loaded},
                    lambda: console.print(f"[green]reloaded[/green] {t.get('url','')}"
                                          f" {'(loaded)' if loaded else '(load not confirmed)'}"))
    finally:
        c.close()


# --- web storage -----------------------------------------------------------
def _storage_area(a):
    return "sessionStorage" if getattr(a, "session", False) else "localStorage"


def _storage_eval(c, expr):
    r = c.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                    "awaitPromise": True})
    if "exceptionDetails" in r:
        ex = r["exceptionDetails"]
        msg = ex.get("exception", {}).get("description", ex.get("text", ""))
        # a page on about:blank or a sandboxed origin has no storage at all
        raise UserError(f"storage unavailable on this page: {msg}", "no-storage")
    return r["result"].get("value")


def cmd_storage(a):
    """Read or write localStorage / sessionStorage for the tab's origin."""
    area = _storage_area(a)
    c, t = connect(a.host, a.port, a.target)
    try:
        if a.clear:
            _storage_eval(c, f"{area}.clear()")
            return emit(a, {"ok": True, "area": area, "cleared": True},
                        lambda: console.print(f"[green]cleared[/green] {area}"))
        changed = []
        for item in a.set:
            if "=" not in item:
                raise UserError(f"bad --set {item!r} — use key=value", "bad-args")
            k, v = item.split("=", 1)
            _storage_eval(c, f"{area}.setItem({json.dumps(k)},{json.dumps(v)})")
            changed.append(k)
        for k in a.remove:
            _storage_eval(c, f"{area}.removeItem({json.dumps(k)})")
            changed.append(k)
        if a.get:
            val = _storage_eval(c, f"{area}.getItem({json.dumps(a.get)})")
            return emit(a, {"ok": True, "area": area, "key": a.get, "value": val},
                        lambda: console.print(val if val is not None else "[dim]null[/dim]"))
        items = _storage_eval(c, f"Object.fromEntries(Object.entries({area}))") or {}

        def render():
            if changed:
                console.print(f"[green]updated[/green] {area}: {', '.join(changed)}")
            tbl = Table(title=f"{area} · {t.get('url','')[:50]}", header_style="bold cyan")
            tbl.add_column("key"); tbl.add_column("value", max_width=60)
            for k, v in items.items():
                tbl.add_row(k, str(v)[:60])
            console.print(tbl)
            console.print(f"[dim]{len(items)} keys[/dim]")

        return emit(a, {"ok": True, "area": area, "url": t.get("url", ""),
                        "changed": changed, "items": items}, render)
    finally:
        c.close()


# --- auth / storage state --------------------------------------------------
def _origin_of(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") else ""


def cmd_auth(a):
    """Save or restore a login: cookies browser-wide + per-origin web storage.

    The point is to log in once and replay the session into a fresh --ephemeral
    instance, instead of copying a whole real Chrome profile just for its cookies.
    """
    if a.action == "save":
        return _auth_save(a)
    return _auth_load(a)


def _auth_save(a):
    c, _ = connect(a.host, a.port, "browser", require_page=False)
    try:
        cookies = c.call("Storage.getCookies").get("cookies", [])
    finally:
        c.close()
    origins = []
    pages = _attachable(list_targets(a.host, a.port))
    wanted = {_origin_of(u) for u in a.origin} if a.origin else None
    for t in pages:
        origin = _origin_of(t.get("url", ""))
        if not origin or (wanted is not None and origin not in wanted):
            continue
        if any(o["origin"] == origin for o in origins):
            continue
        pc = CDP(_target_ws(t))
        try:
            local = _storage_eval(pc, "Object.fromEntries(Object.entries(localStorage))") or {}
            session = _storage_eval(pc, "Object.fromEntries(Object.entries(sessionStorage))") or {}
        except UserError:
            local, session = {}, {}      # about:blank and friends simply have none
        finally:
            pc.close()
        origins.append({"origin": origin, "localStorage": local, "sessionStorage": session})
    state = {"version": 1, "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "cookies": cookies, "origins": origins}
    with open(a.file, "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(a.file, 0o600)              # it is a live session — don't leave it world-readable

    def render():
        console.print(f"[green]saved[/green] {len(cookies)} cookies, "
                      f"{len(origins)} origin(s) → {a.file}")
        console.print("[yellow]note:[/yellow] this file is a live login. Treat it like a password.")

    return emit(a, {"ok": True, "file": os.path.abspath(a.file), "cookies": len(cookies),
                    "origins": [o["origin"] for o in origins]}, render)


def _auth_load(a):
    if not os.path.exists(a.file):
        raise UserError(f"no such file: {a.file}", "bad-args")
    with open(a.file) as f:
        state = json.load(f)
    cookies = state.get("cookies", [])
    c, _ = connect(a.host, a.port, "browser", require_page=False)
    try:
        if cookies:
            c.call("Storage.setCookies", {"cookies": cookies})
    finally:
        c.close()
    restored, skipped = [], []
    open_docs = _attachable(list_targets(a.host, a.port))
    for o in state.get("origins", []):
        if not (o.get("localStorage") or o.get("sessionStorage")):
            continue
        # Web storage is per-origin and only reachable from a document on it.
        # Prefer a window already sitting on that origin — it saves a round trip,
        # and on a browser with no tab model it is the only way in. Only park a
        # fresh tab when there isn't one.
        hit = next((t for t in open_docs if _origin_of(t.get("url", "")) == o["origin"]), None)
        opened = hit is None
        if opened:
            try:
                t = new_target(a.host, a.port, o["origin"] + "/")
            except UserError as e:
                # No tab model, so this origin simply cannot be reached. The cookies
                # already landed, so report the gap instead of throwing all of it away.
                skipped.append({"origin": o["origin"], "reason": str(e)})
                continue
        else:
            t = hit
        pc = CDP(_target_ws(t))
        try:
            for area, items in (("localStorage", o.get("localStorage") or {}),
                                ("sessionStorage", o.get("sessionStorage") or {})):
                for k, v in items.items():
                    _storage_eval(pc, f"{area}.setItem({json.dumps(k)},{json.dumps(str(v))})")
            restored.append(o["origin"])
        except UserError as e:          # origin unreachable (offline/DNS) — cookies still landed
            skipped.append({"origin": o["origin"], "reason": str(e)})
        finally:
            pc.close()
            if opened and not a.keep_tabs:   # never close a window we found already open
                close_target(a.host, a.port, t["id"])

    def render():
        console.print(f"[green]restored[/green] {len(cookies)} cookies"
                      + (f", storage for {len(restored)} origin(s)" if restored else ""))
        for s in skipped:
            console.print(f"[yellow]skipped[/yellow] {s['origin']}: {s['reason']}")

    return emit(a, {"ok": not skipped, "cookies": len(cookies),
                    "origins": restored, "skipped": skipped}, render)


# --- accessibility tree ----------------------------------------------------
BORING_ROLES = {"none", "presentation", "generic", "InlineTextBox", "StaticText", "LineBreak"}


def cmd_a11y(a):
    """Dump the accessibility tree — what a screen reader (and an agent) sees."""
    c, t = connect(a.host, a.port, a.target)
    try:
        c.call("Accessibility.enable")
        nodes = c.call("Accessibility.getFullAXTree", timeout=60).get("nodes", [])
    finally:
        c.close()

    def prop(n, key):
        v = n.get(key) or {}
        return v.get("value") if isinstance(v, dict) else v

    by_id = {n["nodeId"]: n for n in nodes}
    kept = []
    for n in nodes:
        role = prop(n, "role") or ""
        if n.get("ignored"):
            continue
        if not a.all and role in BORING_ROLES:
            continue
        kept.append({
            "id": n["nodeId"],
            "role": role,
            "name": prop(n, "name") or "",
            "value": prop(n, "value"),
            "description": prop(n, "description") or "",
            "depth": 0,
            "parent": n.get("parentId"),
        })
    index = {k["id"]: k for k in kept}
    for k in kept:                       # depth against the kept subset, not the raw tree
        d, p = 0, k["parent"]
        while p is not None and d < 64:
            if p in index:
                d += 1
            p = by_id.get(p, {}).get("parentId")
        k["depth"] = d
    if a.max:
        kept = kept[:a.max]

    def render():
        console.print(f"[bold]accessibility tree[/bold] [dim]{t.get('url','')[:70]}[/dim]")
        for k in kept:
            pad = "  " * min(k["depth"], 12)
            name = f" [white]{k['name'][:60]!r}[/white]" if k["name"] else ""
            val = f" [dim]= {str(k['value'])[:30]}[/dim]" if k["value"] not in (None, "") else ""
            console.print(f"{pad}[cyan]{k['role']}[/cyan]{name}{val}")
        console.print(f"[dim]{len(kept)} nodes[/dim]")

    for k in kept:
        k.pop("parent", None)
    return emit(a, {"ok": True, "url": t.get("url", ""), "count": len(kept), "nodes": kept},
                render)


# --- request interception --------------------------------------------------
def cmd_intercept(a):
    """Block, stub, or rewrite requests while the session is held.

    `capture` reads traffic; this one changes it — block a tracker, serve a fixed
    JSON body for an endpoint, or add a header to everything.
    """
    stubs = []
    for spec in a.stub:
        if "=" not in spec:
            raise UserError(f"bad --stub {spec!r} — use PATTERN=FILE", "bad-args")
        pat, path = spec.split("=", 1)
        if not os.path.exists(path):
            raise UserError(f"no such stub file: {path}", "bad-args")
        with open(path, "rb") as f:
            stubs.append((pat, f.read(), path))
    headers = {}
    for h in a.header:
        if ":" not in h:
            raise UserError(f"bad --header {h!r} — use 'Name: value'", "bad-args")
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()
    if not (a.block or stubs or headers):
        raise UserError("nothing to do — pass --block, --stub or --header", "bad-args")

    import fnmatch

    def matches(url, pat):
        return fnmatch.fnmatch(url, pat) or pat in url

    quiet = getattr(a, "json", False)
    c, t = connect(a.host, a.port, a.target)
    log = []
    try:
        patterns = [{"urlPattern": "*", "requestStage": "Request"}]
        c.call("Fetch.enable", {"patterns": patterns})
        if not quiet:
            stop = f"{a.max}s" if a.max else "Ctrl-C"
            console.print(Panel(f"intercepting on {t.get('url', t['id'])}  —  {stop} to stop",
                                border_style="cyan"))
        started = time.time()
        while True:
            if a.max and time.time() - started > a.max:
                break
            try:
                m = c.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if "__error__" in m:
                break
            if m.get("method") != "Fetch.requestPaused":
                continue
            p = m["params"]
            rid, url = p["requestId"], p["request"]["url"]
            action, detail = "continue", ""
            try:
                hit = next((s for s in stubs if matches(url, s[0])), None)
                if any(matches(url, b) for b in a.block):
                    c.call("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"})
                    action, detail = "block", ""
                elif hit:
                    c.call("Fetch.fulfillRequest", {
                        "requestId": rid, "responseCode": a.status,
                        "responseHeaders": [{"name": "content-type", "value": a.content_type}],
                        "body": base64.b64encode(hit[1]).decode()})
                    action, detail = "stub", hit[2]
                elif headers:
                    merged = dict(p["request"].get("headers", {}))
                    merged.update(headers)
                    c.call("Fetch.continueRequest", {
                        "requestId": rid,
                        "headers": [{"name": k, "value": v} for k, v in merged.items()]})
                    action = "headers"
                else:
                    c.call("Fetch.continueRequest", {"requestId": rid})
            except CDPError:
                continue                 # request died before we answered — nothing to do
            log.append({"action": action, "url": url, "detail": detail})
            if not quiet and action != "continue":
                color = {"block": "red", "stub": "yellow", "headers": "cyan"}[action]
                console.print(f"[{color}]{action:8}[/{color}] {url[:90]} "
                              f"[dim]{detail}[/dim]")
    except KeyboardInterrupt:
        if not quiet:
            console.print("\n[dim]stopped[/dim]")
    finally:
        try:
            c.call("Fetch.disable", timeout=5)
        except Exception:
            pass
        c.close()
    counts = {}
    for e in log:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
    if quiet:
        out_json({"ok": True, "counts": counts, "count": len(log), "requests": log})
    elif not a.max:
        console.print(f"[dim]{counts}[/dim]")
    return log


# --- downloads -------------------------------------------------------------
def cmd_download(a):
    """Arm downloads to a directory and wait for them to finish.

    Headless Chrome drops downloads on the floor unless download behavior is set
    on the session, so this arms it, optionally navigates, and waits.
    """
    outdir = os.path.abspath(os.path.expanduser(a.dir))
    os.makedirs(outdir, exist_ok=True)
    c, t = connect(a.host, a.port, a.target)
    quiet = getattr(a, "json", False)
    done, seen = [], {}
    try:
        c.call("Browser.setDownloadBehavior",
               {"behavior": "allowAndName", "downloadPath": outdir, "eventsEnabled": True})
        c.call("Page.enable")
        if a.url:
            c.call("Page.navigate", {"url": a.url})
        if not quiet:
            console.print(Panel(f"downloads → {outdir}  —  waiting up to {a.wait}s",
                                border_style="cyan"))
        deadline = time.time() + a.wait
        while time.time() < deadline:
            try:
                m = c.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if "__error__" in m:
                break
            method, p = m.get("method"), m.get("params", {})
            if method == "Browser.downloadWillBegin":
                seen[p["guid"]] = p.get("suggestedFilename", p["guid"])
                if not quiet:
                    console.print(f"[cyan]start[/cyan] {seen[p['guid']]}")
            elif method == "Browser.downloadProgress" and p.get("state") in ("completed", "canceled"):
                name = seen.get(p["guid"], p["guid"])
                # allowAndName stores the file under its guid, not its display name
                stored = os.path.join(outdir, p["guid"])
                final = os.path.join(outdir, name)
                if p["state"] == "completed" and os.path.exists(stored):
                    try:
                        os.replace(stored, final)
                    except OSError:
                        final = stored
                done.append({"name": name, "state": p["state"],
                             "path": final if p["state"] == "completed" else None,
                             "bytes": int(p.get("receivedBytes") or 0)})
                if not quiet:
                    color = "green" if p["state"] == "completed" else "red"
                    console.print(f"[{color}]{p['state']}[/{color}] {name}")
                if not a.all:
                    break
    finally:
        c.close()
    if not done and not quiet:
        console.print("[yellow]no downloads completed[/yellow] "
                      "[dim](pass --url, or trigger one in the tab while this runs)[/dim]")
    return emit(a, {"ok": bool(done), "dir": outdir, "count": len(done), "downloads": done},
                lambda: None)


# --------------------------------------------------------------------------
# agent skill (teach a coding agent this CLI, wherever it is installed)
# --------------------------------------------------------------------------
SKILL_DEFAULT_DIR = os.path.expanduser("~/.claude/skills")


def _command_table():
    """The full command surface as a Markdown table, generated from the parser."""
    lines = ["| command | usage | what |", "|---|---|---|"]
    for r in _surface():
        alias = f" ({'/'.join(r['aliases'])})" if r["aliases"] else ""
        usage = " ".join(r["args"] + r["options"]).replace("|", "\\|")
        lines.append(f"| `{r['command']}{alias}` | `{usage}` | {r['help']} |")
    return "\n".join(lines)


def _error_kind_list():
    """The `error.kind` vocabulary as one inline Markdown line, from ERROR_KINDS."""
    return ", ".join(f"`{k}`" for k in ERROR_KINDS if k != "error")


def _skill_text():
    """SKILL.md with the generated bits filled in, so they can never drift."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SKILL.md")
    if not os.path.exists(path):
        raise UserError("SKILL.md is missing from the installed package", "missing-dep")
    with open(path) as f:
        body = f.read()
    return (body.replace("<!-- COMMANDS -->", _command_table())
                .replace("<!-- ERROR-KINDS -->", _error_kind_list()))


def cmd_skill(a):
    """Print or install the agent skill.

    AGENTS.md only helps inside this repo, but chromectl is installed globally —
    so the docs have to ship with the binary and land where an agent will look.
    """
    text = _skill_text()
    if a.action == "print":
        print(text)
        return text
    root = os.path.expanduser(a.dir or SKILL_DEFAULT_DIR)
    dest_dir = os.path.join(root, "chromectl")
    dest = os.path.join(dest_dir, "SKILL.md")
    if os.path.exists(dest) and not a.force:
        raise UserError(f"{dest} already exists — pass --force to overwrite", "exists")
    os.makedirs(dest_dir, exist_ok=True)
    with open(dest, "w") as f:
        f.write(text)

    def render():
        console.print(f"[green]installed[/green] skill → {dest}")
        console.print("[dim]agents that read this directory will pick it up next session[/dim]")

    return emit(a, {"ok": True, "path": dest, "chars": len(text)}, render)


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="chromectl", description="Friendly CLI for the Chrome DevTools Protocol.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Quick start:")[1] if "Quick start:" in __doc__ else "")
    p.add_argument("--host", default=os.environ.get("CDP_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CDP_PORT", "9222")))
    p.add_argument("-i", "--instance", metavar="NAME",
                   help="target a managed instance by name/port (from `chromectl instances`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    jsonopt = argparse.ArgumentParser(add_help=False)
    jsonopt.add_argument("--json", action="store_true", help="machine-readable JSON output")

    def target_arg(sp, required=False):
        sp.add_argument("target", nargs=None if required else "?", default="",
                        help="target: id-prefix, url/title substring, 'browser', or empty=first page")

    sp = sub.add_parser("list", aliases=["ls"], parents=[jsonopt], help="list open targets (tabs)")
    sp.set_defaults(fn=cmd_list)
    sp = sub.add_parser("start", parents=[jsonopt],
                        help="launch a browser (headless by default) or any Electron app")
    # `chromectl start --port N` is the spelling everyone reaches for, but --port is a
    # global. SUPPRESS lets it be accepted here too without clobbering the global default.
    sp.add_argument("--port", type=int, default=argparse.SUPPRESS,
                    help="debug port to listen on (default 9222)")
    sp.add_argument("--host", default=argparse.SUPPRESS, help="bind host (default localhost)")
    sp.add_argument("--name", help="label this instance (target later with -i NAME)")
    sp.add_argument("--auto-port", action="store_true", help="pick a free port instead of --port")
    sp.add_argument("--profile", help="user-data-dir (default: persistent ~/.chromectl/profiles/<name>)")
    sp.add_argument("--ephemeral", action="store_true",
                    help="use a throwaway profile in /tmp (no persistence) instead of the default")
    sp.add_argument("--headful", action="store_true", help="show the window")
    sp.add_argument("--binary", help="path to a Chrome/Chromium binary")
    sp.add_argument("--app", metavar="PATH",
                    help="launch any Electron-based app instead of Chrome (a name on PATH or a "
                         "path); it keeps its own profile/login — no --user-data-dir is injected")
    sp.add_argument("--wait", type=float, metavar="SECONDS",
                    help="how long to wait for the debug port (default 12; 45 with --app)")
    sp.add_argument("--copy-profile", action="store_true",
                    help="copy your real Chrome profile (logins/cookies) into --profile first")
    sp.add_argument("--from-profile", metavar="PATH",
                    help="copy from this profile dir instead of auto-detecting")
    sp.add_argument("--proxy", metavar="URL",
                    help="proxy for this instance: [scheme://][user:pass@]host:port "
                         "(http, https, socks4, socks5; default scheme http)")
    sp.add_argument("--proxy-auth", metavar="USER:PASS",
                    help="proxy credentials, instead of putting them in --proxy")
    sp.add_argument("--proxy-bypass", metavar="LIST",
                    help="hosts that skip the proxy, e.g. 'localhost,*.internal'")
    sp.add_argument("--proxy-pac", metavar="URL", help="use a PAC file instead of --proxy")
    sp.add_argument("--chrome-arg", action="append", metavar="FLAG",
                    help="extra Chrome flag, repeatable (also: put them after a bare `--`)")
    sp.add_argument("chrome_args", nargs="*", metavar="FLAG",
                    help="anything after a bare `--` is passed straight to Chrome")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("instances", aliases=["ps"], parents=[jsonopt],
                        help="list managed Chrome instances and their status")
    sp.add_argument("--prune", action="store_true", help="forget instances that are no longer up")
    sp.set_defaults(fn=cmd_instances)

    sp = sub.add_parser("adopt", parents=[jsonopt],
                        help="record an already-running browser/app on a debug port so -i NAME works")
    sp.add_argument("port_arg", nargs="?", metavar="PORT", help="debug port (default: --port)")
    sp.add_argument("--name", help="label this instance (default: from its user agent)")
    sp.add_argument("--force", action="store_true", help="replace an existing entry of that name")
    sp.set_defaults(fn=cmd_adopt)

    sp = sub.add_parser("stop", parents=[jsonopt],
                        help="stop a managed instance (by name/port) or --all")
    sp.add_argument("which", nargs="?", help="instance name or port")
    sp.add_argument("--all", action="store_true", help="stop every managed instance")
    sp.add_argument("--purge", action="store_true", help="also delete the instance's profile dir")
    sp.add_argument("--forget", action="store_true",
                    help="drop it from the registry without stopping the process")
    sp.add_argument("--force", action="store_true",
                    help="kill even an adopted instance (it is somebody's real app)")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("version", parents=[jsonopt], help="browser + protocol version"); sp.set_defaults(fn=cmd_version)

    sp = sub.add_parser("cheat", aliases=["commands"], parents=[jsonopt],
                        help="print the entire command surface in one call (agent-friendly)")
    sp.set_defaults(fn=cmd_cheat)

    sp = sub.add_parser("open", parents=[jsonopt], help="open a new tab at URL")
    sp.add_argument("url"); sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("close", parents=[jsonopt], help="close a tab")
    target_arg(sp, required=True); sp.set_defaults(fn=cmd_close)

    sp = sub.add_parser("goto", aliases=["nav"], parents=[jsonopt], help="navigate a tab to URL")
    target_arg(sp); sp.add_argument("url"); sp.add_argument("--timeout", type=float, default=15)
    sp.set_defaults(fn=cmd_goto)

    sp = sub.add_parser("eval", aliases=["js"], parents=[jsonopt], help="run JavaScript in a tab")
    target_arg(sp); sp.add_argument("js", nargs="+", help="JS expression")
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("html", parents=[jsonopt], help="dump a tab's HTML")
    target_arg(sp); sp.add_argument("--out"); sp.add_argument("--max", type=int, default=4000)
    sp.set_defaults(fn=cmd_html)

    sp = sub.add_parser("text", parents=[jsonopt], help="dump a tab's visible text")
    target_arg(sp); sp.set_defaults(fn=cmd_text)

    sp = sub.add_parser("cookies", parents=[jsonopt], help="list, set, delete or clear cookies")
    sp.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="set a cookie (repeatable)")
    sp.add_argument("--delete", action="append", default=[], metavar="NAME",
                    help="delete a cookie by name (repeatable)")
    sp.add_argument("--clear", action="store_true", help="clear ALL browser cookies")
    sp.add_argument("--url", help="URL the cookie belongs to (default: the tab's)")
    sp.add_argument("--domain", help="cookie domain (default: from --url)")
    target_arg(sp); sp.set_defaults(fn=cmd_cookies)

    sp = sub.add_parser("screenshot", aliases=["shot"], parents=[jsonopt], help="capture a screenshot")
    target_arg(sp); sp.add_argument("--out"); sp.add_argument("--full", action="store_true",
                                                              help="full-page (beyond viewport)")
    sp.set_defaults(fn=cmd_screenshot)

    sp = sub.add_parser("pdf", parents=[jsonopt], help="print a tab to PDF")
    target_arg(sp); sp.add_argument("--out"); sp.set_defaults(fn=cmd_pdf)

    def loc_args(sp):
        sp.add_argument("--ref", type=int, help="index from the last `snapshot`")
        sp.add_argument("--selector", help="CSS selector")
        sp.add_argument("--text", help="visible-text locator")
        sp.add_argument("--role", help="ARIA role (with optional --name)")
        sp.add_argument("--name", help="accessible name (used with --role)")
        sp.add_argument("--timeout", type=float, default=10000, help="ms (default 10000)")

    sp = sub.add_parser("read", parents=[jsonopt], help="extract main content as clean Markdown")
    target_arg(sp); sp.add_argument("--out", help="save Markdown to file")
    sp.add_argument("--max", type=int, default=6000, help="max chars to print (default 6000)")
    sp.set_defaults(fn=cmd_read)

    sp = sub.add_parser("extract", parents=[jsonopt], help="scrape fields to JSON: --field name=selector[@attr][]")
    target_arg(sp)
    sp.add_argument("--field", action="append", required=True, metavar="name=sel",
                    help="repeatable; @attr for an attribute, [] suffix for all matches")
    sp.set_defaults(fn=cmd_extract)

    sp = sub.add_parser("links", parents=[jsonopt], help="list page links (text + href)")
    target_arg(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--internal", action="store_true", help="only same-host links")
    g.add_argument("--external", action="store_true", help="only off-host links")
    sp.set_defaults(fn=cmd_links)

    sp = sub.add_parser("wait", parents=[jsonopt], help="wait for a selector/text/url/network-idle")
    target_arg(sp)
    sp.add_argument("--selector"); sp.add_argument("--text"); sp.add_argument("--url")
    sp.add_argument("--network-idle", dest="network_idle", action="store_true")
    sp.add_argument("--gone", action="store_true", help="wait for it to DISAPPEAR (selector/text)")
    sp.add_argument("--timeout", type=float, default=10000, help="ms (default 10000)")
    sp.set_defaults(fn=cmd_wait)

    sp = sub.add_parser("fill-form", parents=[jsonopt], help="fill multiple fields, optionally submit")
    target_arg(sp, required=True)
    sp.add_argument("--set", action="append", required=True, metavar="sel=value",
                    help="repeatable: CSS selector = value")
    sp.add_argument("--submit", metavar="SELECTOR", help="click this selector after filling")
    sp.add_argument("--enter", action="store_true", help="press Enter on the last field instead")
    sp.add_argument("--timeout", type=float, default=10000)
    sp.set_defaults(fn=cmd_fillform)

    sp = sub.add_parser("perf", help="measure Core Web Vitals (+ optional trace)")
    sp.add_argument("url", nargs="?", help="URL to open+measure (omit with --attach)")
    sp.add_argument("--attach", metavar="TARGET", help="measure an existing tab")
    sp.add_argument("--reload", action="store_true", help="with --attach: reload while measuring")
    sp.add_argument("--wait", type=float, default=3, help="seconds to settle after load (default 3)")
    sp.add_argument("--out", help="also save a raw trace (.json) loadable in DevTools Performance")
    sp.set_defaults(fn=cmd_perf)

    sp = sub.add_parser("lighthouse", aliases=["lh"], help="run a Lighthouse audit (needs `npm i -g lighthouse`)")
    sp.add_argument("url")
    sp.add_argument("--categories", help="comma list: performance,accessibility,best-practices,seo,pwa")
    sp.add_argument("--preset", choices=["desktop", "mobile"], default="mobile")
    sp.add_argument("--out", help="save the full JSON report")
    sp.set_defaults(fn=cmd_lighthouse)

    sp = sub.add_parser("snapshot", aliases=["snap"], parents=[jsonopt], help="list interactive elements (Playwright); saves refs")
    target_arg(sp); sp.add_argument("--out", help="write refs here (default ~/.chromectl/snaps/<host>-<port>.json)")
    sp.set_defaults(fn=cmd_snapshot)

    sp = sub.add_parser("click", parents=[jsonopt], help="click an element (Playwright auto-wait)")
    target_arg(sp); loc_args(sp); sp.set_defaults(fn=cmd_click)

    sp = sub.add_parser("fill", parents=[jsonopt], help="fill an input/textarea (Playwright)")
    target_arg(sp, required=True); sp.add_argument("value", nargs="+"); loc_args(sp)
    sp.add_argument("--enter", action="store_true", help="press Enter after")
    sp.set_defaults(fn=cmd_fill)

    sp = sub.add_parser("hover", parents=[jsonopt], help="hover an element (Playwright)")
    target_arg(sp); loc_args(sp); sp.set_defaults(fn=cmd_hover)

    sp = sub.add_parser("emulate", parents=[jsonopt], help="device/geo/network/color-scheme/UA emulation")
    target_arg(sp)
    sp.add_argument("--width", type=int); sp.add_argument("--height", type=int)
    sp.add_argument("--scale", type=float, default=1); sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--geo", metavar="LAT,LON", help="geolocation override")
    sp.add_argument("--throttle", choices=list(THROTTLE), help="network conditions preset")
    sp.add_argument("--color", choices=["light", "dark"], help="prefers-color-scheme")
    sp.add_argument("--ua", help="user-agent override")
    sp.add_argument("--shot", metavar="PATH", help="screenshot after applying (in-session)")
    sp.add_argument("--hold", action="store_true", help="keep session open so overrides persist")
    sp.add_argument("--clear", action="store_true", help="clear all overrides")
    sp.set_defaults(fn=cmd_emulate)

    sp = sub.add_parser("resize", parents=[jsonopt], help="set viewport size (device metrics)")
    target_arg(sp); sp.add_argument("width", type=int); sp.add_argument("height", type=int)
    sp.add_argument("--scale", type=float, default=1); sp.add_argument("--mobile", action="store_true")
    sp.add_argument("--shot", metavar="PATH"); sp.add_argument("--hold", action="store_true")
    sp.set_defaults(fn=cmd_resize)

    sp = sub.add_parser("press", parents=[jsonopt], help="press key(s): Enter, Tab, ArrowDown, a, …")
    target_arg(sp, required=True); sp.add_argument("keys", nargs="+")
    sp.add_argument("--selector", help="focus this CSS selector first")
    sp.set_defaults(fn=cmd_press)

    sp = sub.add_parser("type", parents=[jsonopt], help="type text into the focused (or --selector) element")
    target_arg(sp, required=True); sp.add_argument("text", nargs="+")
    sp.add_argument("--selector", help="focus this CSS selector first")
    sp.add_argument("--enter", action="store_true", help="press Enter after")
    sp.set_defaults(fn=cmd_type)

    sp = sub.add_parser("upload", parents=[jsonopt], help="set files on a file <input>")
    target_arg(sp, required=True); sp.add_argument("--selector", required=True, help="CSS selector of the file input")
    sp.add_argument("files", nargs="+", help="local file path(s)")
    sp.set_defaults(fn=cmd_upload)

    sp = sub.add_parser("dialog", help="auto-accept/dismiss JS dialogs (holds session)")
    target_arg(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--accept", action="store_true", default=True)
    g.add_argument("--dismiss", dest="accept", action="store_false")
    sp.add_argument("--text", help="promptText for prompt() dialogs")
    sp.add_argument("--max", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    sp.set_defaults(fn=cmd_dialog)

    sp = sub.add_parser("heapsnapshot", aliases=["heap"], parents=[jsonopt], help="capture a V8 heap snapshot")
    target_arg(sp); sp.add_argument("--out"); sp.set_defaults(fn=cmd_heapsnapshot)

    sp = sub.add_parser("run", parents=[jsonopt], help="run a sequence of steps in one process (script/batch)")
    sp.add_argument("file", nargs="?", help="steps file, or '-'/omitted = stdin")
    sp.add_argument("--step", action="append", metavar="CMD", help="a step (repeatable), instead of a file")
    sp.add_argument("--target", default="", help="default target for steps that omit one")
    sp.add_argument("--keep-going", action="store_true", help="continue after a failing step")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("back", parents=[jsonopt], help="go back in history")
    target_arg(sp); sp.set_defaults(fn=cmd_back)

    sp = sub.add_parser("forward", parents=[jsonopt], help="go forward in history")
    target_arg(sp); sp.set_defaults(fn=cmd_forward)

    sp = sub.add_parser("reload", parents=[jsonopt], help="reload a tab")
    target_arg(sp)
    sp.add_argument("--hard", action="store_true", help="bypass the cache")
    sp.add_argument("--timeout", type=float, default=15, help="seconds to await load")
    sp.set_defaults(fn=cmd_reload)

    sp = sub.add_parser("storage", parents=[jsonopt],
                        help="read/write localStorage or sessionStorage")
    target_arg(sp)
    sp.add_argument("--session", action="store_true", help="sessionStorage instead of localStorage")
    sp.add_argument("--get", metavar="KEY", help="read one key")
    sp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="set a key (repeatable)")
    sp.add_argument("--remove", action="append", default=[], metavar="KEY",
                    help="remove a key (repeatable)")
    sp.add_argument("--clear", action="store_true", help="clear the whole area")
    sp.set_defaults(fn=cmd_storage)

    sp = sub.add_parser("auth", parents=[jsonopt],
                        help="save/load a login (cookies + per-origin web storage)")
    sp.add_argument("action", choices=["save", "load"])
    sp.add_argument("file", help="storage-state JSON file")
    sp.add_argument("--origin", action="append", default=[], metavar="URL",
                    help="save: limit web storage to these origins (repeatable)")
    sp.add_argument("--keep-tabs", action="store_true",
                    help="load: leave the tabs opened to restore each origin")
    sp.set_defaults(fn=cmd_auth)

    sp = sub.add_parser("a11y", aliases=["ax"], parents=[jsonopt],
                        help="dump the accessibility tree (roles + names)")
    target_arg(sp)
    sp.add_argument("--all", action="store_true", help="include generic/presentational nodes")
    sp.add_argument("--max", type=int, default=0, help="cap the number of nodes")
    sp.set_defaults(fn=cmd_a11y)

    sp = sub.add_parser("intercept", parents=[jsonopt],
                        help="block, stub or rewrite requests (holds session)")
    target_arg(sp)
    sp.add_argument("--block", action="append", default=[], metavar="PATTERN",
                    help="fail requests matching this glob/substring (repeatable)")
    sp.add_argument("--stub", action="append", default=[], metavar="PATTERN=FILE",
                    help="serve FILE for requests matching PATTERN (repeatable)")
    sp.add_argument("--header", action="append", default=[], metavar="'Name: value'",
                    help="add a request header to everything (repeatable)")
    sp.add_argument("--status", type=int, default=200, help="status code for --stub")
    sp.add_argument("--content-type", default="application/json", help="content-type for --stub")
    sp.add_argument("--max", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    sp.set_defaults(fn=cmd_intercept)

    sp = sub.add_parser("download", parents=[jsonopt],
                        help="arm downloads to a directory and wait")
    target_arg(sp)
    sp.add_argument("--dir", default="./downloads", help="where files land (default ./downloads)")
    sp.add_argument("--url", help="navigate the tab here after arming")
    sp.add_argument("--wait", type=float, default=30, help="seconds to wait (default 30)")
    sp.add_argument("--all", action="store_true", help="keep waiting for more than one file")
    sp.set_defaults(fn=cmd_download)

    sp = sub.add_parser("skill", parents=[jsonopt],
                        help="print or install the agent skill for this CLI")
    sp.add_argument("action", nargs="?", default="print", choices=["print", "install"])
    sp.add_argument("--dir", help=f"install root (default {SKILL_DEFAULT_DIR})")
    sp.add_argument("--force", action="store_true", help="overwrite an existing skill")
    sp.set_defaults(fn=cmd_skill)

    sp = sub.add_parser("raw", aliases=["cmd"], help="send a raw CDP command")
    target_arg(sp); sp.add_argument("method"); sp.add_argument("params", nargs="?",
                                                               help="params as JSON")
    sp.set_defaults(fn=cmd_raw)

    sp = sub.add_parser("repl", help="interactive CDP prompt for a target")
    target_arg(sp); sp.set_defaults(fn=cmd_repl)

    sp = sub.add_parser("proto", help="look up protocol domains/commands/events")
    sp.add_argument("query", nargs="?", help="Domain | Domain.command | Domain.event")
    sp.set_defaults(fn=cmd_proto)

    sp = sub.add_parser("watch", parents=[jsonopt], help="live-tail network requests of a tab")
    target_arg(sp); sp.add_argument("--max", type=float, default=0,
                                    help="stop after N seconds (0 = until Ctrl-C)")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("console", aliases=["logs"], parents=[jsonopt], help="tail console messages + JS errors")
    target_arg(sp); sp.add_argument("--max", type=float, default=0,
                                    help="stop after N seconds (0 = until Ctrl-C)")
    sp.set_defaults(fn=cmd_console)

    sp = sub.add_parser("seo", parents=[jsonopt], help="on-page SEO audit of a tab or URL")
    sp.add_argument("target", nargs="?", default="",
                    help="a URL (opens+audits+closes) OR a target selector for a loaded tab")
    sp.set_defaults(fn=cmd_seo)

    sp = sub.add_parser("capture", parents=[jsonopt],
                        help="Burp-style full request/response capture")
    sp.add_argument("url", nargs="?", help="URL to open+capture (omit with --attach)")
    sp.add_argument("--attach", metavar="TARGET", help="capture an existing tab instead of opening one")
    sp.add_argument("--reload", action="store_true", help="with --attach: reload to capture from start")
    sp.add_argument("--type", help="filter by resource type substring (document/xhr/fetch/script/image…)")
    sp.add_argument("--print", type=int, default=3, help="how many full transactions to print (default 3)")
    sp.add_argument("--out", help="write ALL transactions raw to this file")
    sp.add_argument("--har", help="write a .har file (loadable in DevTools/Burp)")
    sp.add_argument("--no-bodies", action="store_true", help="skip fetching response bodies")
    sp.add_argument("--bodycap", type=int, default=1600, help="max body chars printed to console")
    sp.add_argument("--max", type=float, default=20, help="max capture seconds")
    sp.add_argument("--quiet", type=float, default=1.5, help="stop after this many idle seconds post-load")
    sp.set_defaults(fn=cmd_capture)
    return p


def die(args, kind, message, hint=""):
    """Report a fatal error the way the caller asked for it, then exit 1.

    With --json the error is a JSON object on stdout, so an agent parsing stdout
    gets a value either way instead of an empty string plus red prose on stderr.
    """
    if getattr(args, "json", False):
        out_json({"ok": False, "error": {"kind": kind, "message": str(message)}})
    else:
        err.print(f"{kind}: {message}" + (f"\n{hint}" if hint else ""))
    sys.exit(1)


def main():
    args = build_parser().parse_args()
    if getattr(args, "instance", None):        # -i NAME → that instance's host/port
        inst = _find_instance(args.instance)
        if not inst:
            die(args, "no-instance",
                f"no managed instance {args.instance!r} (see: chromectl instances)")
        args.host, args.port = inst.get("host", args.host), inst.get("port", args.port)
    if args.cmd == "capture" and not args.url and not args.attach:
        die(args, "bad-args", "capture needs a URL or --attach TARGET")
    try:
        args.fn(args)
    except UserError as e:
        die(args, e.kind, e)
    except (ConnectionError, OSError) as e:
        die(args, "connection", e,
            f"(is Chrome running with --remote-debugging-port={args.port}?)")
    except (CDPError, TimeoutError) as e:
        die(args, "cdp", e)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
