"""Tests that need no browser: parsing, flag merging, the surface, the skill."""
import argparse
import json
import os
import platform

import pytest

from chromectl import cli


# --- the command surface --------------------------------------------------
def test_parser_builds_and_every_command_has_a_handler():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert len(sub.choices) > 40
    for name, sp in sub.choices.items():
        assert sp.get_default("fn") is not None, f"{name} has no fn"


def test_surface_is_introspected_not_hand_written():
    rows = {r["command"] for r in cli._surface()}
    for expected in ("open", "read", "run", "auth", "a11y", "intercept", "download", "skill"):
        assert expected in rows
    assert all(r["help"] for r in cli._surface()), "every command needs a help string"


@pytest.mark.parametrize("command", [
    "list", "read", "extract", "links", "wait", "fill-form", "seo", "cookies",
    "snapshot", "eval", "run", "watch", "console", "a11y", "storage", "auth",
    "capture", "intercept", "download", "version", "open", "goto", "click",
])
def test_read_and_action_commands_accept_json(command):
    """--json is the agent contract: it has to exist everywhere, uniformly."""
    row = next(r for r in cli._surface() if r["command"] == command)
    assert "--json" in row["options"], f"{command} is missing --json"


def test_watch_is_bounded():
    """An unbounded tail hangs an agent; --max is what makes it callable."""
    row = next(r for r in cli._surface() if r["command"] == "watch")
    assert any(o.startswith("--max") for o in row["options"])


# --- errors ---------------------------------------------------------------
def test_user_error_carries_a_machine_readable_kind():
    e = cli.UserError("nope", "timeout")
    assert e.kind == "timeout"
    assert str(e) == "nope"


def test_emit_prints_json_when_asked(capsys):
    a = argparse.Namespace(json=True)
    cli.emit(a, {"ok": True, "n": 1}, lambda: print("human"))
    assert json.loads(capsys.readouterr().out) == {"ok": True, "n": 1}


def test_emit_renders_for_humans_otherwise(capsys):
    a = argparse.Namespace(json=False)
    cli.emit(a, {"ok": True}, lambda: print("human"))
    assert capsys.readouterr().out.strip() == "human"


def test_die_emits_a_json_envelope(capsys):
    a = argparse.Namespace(json=True)
    with pytest.raises(SystemExit) as exc:
        cli.die(a, "timeout", "waited too long")
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": {"kind": "timeout", "message": "waited too long"}}


# --- field specs ----------------------------------------------------------
@pytest.mark.parametrize("spec,expected", [
    ("title=h1", {"name": "title", "sel": "h1", "attr": None, "all": False}),
    ("prices=.price[]", {"name": "prices", "sel": ".price", "attr": None, "all": True}),
    ("img=img@src", {"name": "img", "sel": "img", "attr": "src", "all": False}),
    ("skus=.price[]@data-sku", {"name": "skus", "sel": ".price", "attr": "data-sku", "all": True}),
])
def test_parse_field(spec, expected):
    assert cli._parse_field(spec) == expected


def test_parse_field_rejects_a_missing_equals():
    with pytest.raises(cli.CDPError):
        cli._parse_field("justaselector")


# --- chrome flag merging --------------------------------------------------
def test_user_flags_override_ours():
    merged = cli._merge_chrome_flags(
        ["--headless=new", "--user-data-dir=/a", "--no-first-run"],
        ["--user-data-dir=/b"])
    assert "--user-data-dir=/b" in merged
    assert "--user-data-dir=/a" not in merged
    assert "--no-first-run" in merged


def test_switch_helpers():
    assert cli._switch_name("--lang=tr") == "--lang"
    assert cli._switch_name("--headful") == "--headful"
    assert cli._switch_value(["--port=9222", "--lang=tr"], "--lang") == "tr"
    assert cli._switch_value(["--lang=tr"], "--nope") is None


# --- origins / auth state -------------------------------------------------
@pytest.mark.parametrize("url,origin", [
    ("https://example.com/a/b?c=1", "https://example.com"),
    ("http://localhost:8099/page.html", "http://localhost:8099"),
    ("about:blank", ""),
    ("file:///tmp/x.html", ""),
])
def test_origin_of(url, origin):
    assert cli._origin_of(url) == origin


# --- run step payloads ----------------------------------------------------
def test_step_payload_prefers_parsed_json():
    assert cli._step_payload('{"ok": true}', None) == {"ok": True}


def test_step_payload_falls_back_to_text():
    assert cli._step_payload("not json", None) == {"output": "not json"}


def test_step_payload_uses_the_return_value_when_nothing_printed():
    assert cli._step_payload("", {"id": "abc"}) == {"id": "abc"}


# --- the skill ------------------------------------------------------------
def test_skill_has_frontmatter_with_a_description():
    text = cli._skill_text()
    assert text.startswith("---\n")
    head = text.split("---")[1]
    assert "name: chromectl" in head
    assert "description:" in head


def test_skill_command_table_is_generated_from_the_parser():
    """The table must come from the parser, or the docs drift the first time
    someone adds a command."""
    text = cli._skill_text()
    assert "<!-- COMMANDS -->" not in text
    for command in ("`intercept", "`auth", "`a11y", "`download"):
        assert command in text


def test_skill_documents_the_error_kinds_it_promises():
    text = cli._skill_text()
    for kind in ("no-target", "timeout", "js-exception", "bad-args"):
        assert kind in text


# --- HAR ------------------------------------------------------------------
def test_build_har_is_well_formed():
    records = [{
        "req": {"method": "GET", "url": "https://x.test/a", "headers": {"accept": "*/*"}},
        "resp": {"status": 200, "statusText": "OK", "headers": {"content-type": "text/html"},
                 "mimeType": "text/html"},
        "body": "<html></html>", "type": "Document",
    }]
    har = cli._build_har(records, "https://x.test/a")
    assert har["log"]["version"]
    entry = har["log"]["entries"][0]
    assert entry["request"]["method"] == "GET"
    assert entry["response"]["status"] == 200
    json.dumps(har)     # must be serialisable


# --- attaching to a browser that has no tab model (Electron apps) ----------
def _t(tid, type_="page", url="", title="", ws="ws://x/1"):
    t = {"id": tid, "type": type_, "url": url, "title": title}
    if ws:
        t["webSocketDebuggerUrl"] = ws
    return t


def test_attachable_prefers_a_page():
    got = cli._attachable([_t("A", "webview"), _t("B", "page"), _t("C", "other")])
    assert [t["id"] for t in got] == ["B"]


def test_attachable_falls_back_when_there_are_no_pages():
    """An Electron window can come back as 'webview' — insisting on 'page' would
    report an empty browser at an app full of windows."""
    got = cli._attachable([_t("A", "webview"), _t("C", "other")])
    assert [t["id"] for t in got] == ["A", "C"]


def test_attachable_skips_targets_we_cannot_drive():
    assert cli._attachable([_t("A", "page", ws=None), _t("B", "browser")]) == []


def test_resolve_picks_a_webview_when_that_is_all_there_is(monkeypatch):
    monkeypatch.setattr(cli, "list_targets",
                        lambda h, p: [_t("W1", "webview", "file:///app.html", "Slack")])
    assert cli.resolve("h", 1, "")["id"] == "W1"


def test_resolve_error_names_the_types_it_actually_found(monkeypatch):
    monkeypatch.setattr(cli, "list_targets", lambda h, p: [_t("S", "service_worker", ws=None)])
    with pytest.raises(cli.UserError) as exc:
        cli.resolve("h", 1, "")
    assert exc.value.kind == "no-target"
    assert "service_worker" in str(exc.value)
    assert "chromectl open" not in str(exc.value), "that hint is Chrome-only advice"


def test_target_ws_reports_instead_of_raising_keyerror():
    """This used to be a bare subscript, so an undrivable target was a traceback."""
    with pytest.raises(cli.UserError) as exc:
        cli._target_ws(_t("ABC123", "other", ws=None))
    assert exc.value.kind == "no-target"
    assert "ABC123" in str(exc.value)


def test_new_target_passes_the_browsers_own_refusal_through(monkeypatch):
    """Electron answers Target.createTarget with 'Not supported'. The user should
    read that, not a generic failure — this is the behaviour under test without Electron."""
    class FakeCDP:
        def __init__(self, ws): pass
        def call(self, method, params=None): raise cli.CDPError("Not supported")
        def close(self): pass
    monkeypatch.setattr(cli, "browser_ws", lambda h, p: "ws://x/browser")
    monkeypatch.setattr(cli, "CDP", FakeCDP)
    with pytest.raises(cli.UserError) as exc:
        cli.new_target("h", 1, "about:blank")
    assert exc.value.kind == "cdp"
    assert "Not supported" in str(exc.value)


# --- launch flags ---------------------------------------------------------
def _launch_args(**kw):
    base = dict(app=None, headful=False, chrome_arg=None, chrome_args=[])
    base.update(kw)
    return argparse.Namespace(**base)


def test_chrome_launch_flags_are_unchanged():
    flags = cli._launch_flags(_launch_args(), 9222, "/tmp/p")
    assert "--headless=new" in flags
    assert "--user-data-dir=/tmp/p" in flags
    assert "--remote-allow-origins=*" in flags


def test_app_launch_injects_only_the_debug_port():
    """An app keeps its own profile — injecting --user-data-dir would hand back a
    signed-out application, which defeats the point of attaching to it."""
    flags = cli._launch_flags(_launch_args(app="/usr/bin/slack"), 9333, None)
    assert flags == ["--remote-debugging-port=9333"]


def test_app_launch_honours_an_explicit_profile():
    flags = cli._launch_flags(_launch_args(app="/usr/bin/slack"), 9333, "/tmp/iso")
    assert flags == ["--remote-debugging-port=9333", "--user-data-dir=/tmp/iso"]


def test_user_flags_still_win_for_an_app():
    flags = cli._launch_flags(
        _launch_args(app="/usr/bin/slack", chrome_arg=["--user-data-dir=/tmp/x"]), 9333, None)
    assert "--user-data-dir=/tmp/x" in flags


# --- the instance registry ------------------------------------------------
def test_registry_roundtrip_and_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "STATE_FILE", str(tmp_path / "instances.json"))
    cli._save_instances([{"name": "work", "host": "localhost", "port": 9222}])
    assert cli._find_instance("work")["port"] == 9222
    assert cli._find_instance("9222")["name"] == "work"
    assert cli._find_instance("nope") is None


def test_legacy_entries_default_to_a_managed_chrome():
    """Entries written before --app existed carry neither field."""
    old = {"name": "work", "port": 9222}
    assert cli._inst_kind(old) == "chrome"
    assert cli._inst_managed(old) is True


def test_adopted_entries_are_not_ours_to_kill():
    adopted = {"name": "slack", "port": 9222, "kind": "app", "managed": False}
    assert cli._inst_kind(adopted) == "app"
    assert cli._inst_managed(adopted) is False


def test_stop_refuses_to_kill_what_it_did_not_start(monkeypatch, tmp_path):
    """The pkill fallback matches on the debug port, so without this guard
    `stop` would quit the user's real, signed-in application."""
    monkeypatch.setattr(cli, "STATE_FILE", str(tmp_path / "instances.json"))
    cli._save_instances([{"name": "slack", "host": "localhost", "port": 9222,
                          "pid": 4242, "kind": "app", "managed": False}])
    monkeypatch.setattr(cli.os, "kill", lambda *a: pytest.fail("killed an adopted process"))
    monkeypatch.setattr(cli.os, "system", lambda *a: pytest.fail("pkill'd an adopted process"))
    args = argparse.Namespace(which="slack", all=False, purge=False, forget=False,
                              force=False, json=False, host="localhost")
    with pytest.raises(cli.UserError) as exc:
        cli.cmd_stop(args)
    assert "--force" in str(exc.value) and "--forget" in str(exc.value)
    assert cli._find_instance("slack") is not None, "a refused stop must not drop the entry"


def test_ua_product_names_the_app_not_the_engine():
    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Slack/4.33.84 Chrome/114.0.5735.289 Electron/25.3.1 Safari/537.36")
    assert cli._ua_product(ua) == "slack"


# --- the error-kind vocabulary --------------------------------------------
def test_every_error_kind_used_in_the_source_is_documented():
    """Three hand-written doc lists had already drifted apart; ERROR_KINDS is now
    the single source and this keeps new kinds from slipping in unlisted."""
    import inspect
    import re
    src = inspect.getsource(cli)
    used = set(re.findall(r'UserError\([^)]*?,\s*"([a-z-]+)"\s*\)', src, re.S))
    used |= set(re.findall(r'\bdie\(\w+,\s*"([a-z-]+)"', src))
    undocumented = used - set(cli.ERROR_KINDS)
    assert not undocumented, f"add to ERROR_KINDS: {sorted(undocumented)}"


def test_agents_md_command_table_matches_the_parser():
    """AGENTS.md pastes the generated table by hand, and it had already drifted once.
    Regenerate with: python -c 'from chromectl.cli import _command_table; print(_command_table())'"""
    import pathlib
    doc = pathlib.Path(__file__).resolve().parent.parent / "AGENTS.md"
    if not doc.exists():
        pytest.skip("AGENTS.md is not part of the installed package")
    _, sep, table = doc.read_text().partition("## All commands\n\n")
    assert sep, "AGENTS.md lost its '## All commands' heading"
    assert table.strip() == cli._command_table().strip(), "AGENTS.md table is stale"


def test_skill_has_no_unfilled_placeholders():
    text = cli._skill_text()
    assert "<!--" not in text, "a generated placeholder was left unsubstituted"
    assert "`launch-failed`" in text, "the kind list should come from ERROR_KINDS"


# --- daemon (no browser needed: exercises protocol + dispatch capture) -----
def test_daemon_lifecycle_protocol_and_routing(tmp_path, monkeypatch):
    """Start the server in-process, then ping/status/route/shutdown over the socket.

    No Chrome: routing a command that can't reach a browser must come back as the
    same JSON error envelope the CLI produces, proving dispatch runs identically
    inside the daemon."""
    import contextlib
    import io
    import threading
    import time

    from chromectl import daemon as d

    monkeypatch.setattr(d, "_DIR", str(tmp_path))
    monkeypatch.setattr(d, "SOCK", str(tmp_path / "daemon.sock"))
    monkeypatch.setattr(d, "PIDFILE", str(tmp_path / "daemon.pid"))

    assert d.is_running() is False
    threading.Thread(target=d.serve, daemon=True).start()
    for _ in range(60):
        if d.is_running():
            break
        time.sleep(0.05)
    assert d.is_running(), "daemon never came up"

    st = d.status()
    assert st["running"] and st["pooled"] == 0 and "uptime" in st

    # a command that reaches no browser -> connection error, exit 1, JSON on stdout
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = d.route(["--port", "1", "version", "--json"])
    assert code == 1
    obj = json.loads(buf.getvalue())
    assert obj["ok"] is False and obj["error"]["kind"] == "connection"

    # a routable command that needs no browser succeeds through the daemon
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = d.route(["cheat", "--json"])
    assert code == 0 and "replay" in buf.getvalue()

    # a persistent Client issues many requests over ONE connection (the stream)
    with d.Client() as client:
        r1 = client.call(["cheat", "--json"])
        r2 = client.call(["--port", "1", "version", "--json"])
        assert r1["ok"] is True and r1["code"] == 0 and "replay" in r1["stdout"]
        assert r2["ok"] is False and r2["code"] == 1
        assert json.loads(r2["stdout"])["error"]["kind"] == "connection"

    assert d.stop() is True
    for _ in range(60):
        if not d.is_running():
            break
        time.sleep(0.05)
    assert d.is_running() is False
    assert cli._RUN_ACTIVE is False, "daemon must not leak pooling after shutdown"


def test_streaming_commands_are_not_routable():
    from chromectl import daemon as d
    for cmd in ("watch", "console", "intercept", "capture", "dialog", "repl", "run", "start"):
        assert cmd in d.NON_ROUTABLE


def test_buffer_and_replay_last_refuse_outside_the_daemon():
    """The capture buffer is daemon-resident; asking for it in a bare process
    must fail with a clear bad-args, not silently operate on an empty buffer."""
    from chromectl import daemon as d
    assert d._IN_DAEMON is False
    p = cli.build_parser()
    with pytest.raises(cli.UserError) as e1:
        cli.cmd_buffer(p.parse_args(["buffer", "list"]))
    assert e1.value.kind == "bad-args"
    with pytest.raises(cli.UserError) as e2:
        cli.cmd_replay(p.parse_args(["replay", "--last"]))
    assert e2.value.kind == "bad-args"


def test_daemon_subscribe_streams_events_concurrently(tmp_path, monkeypatch):
    """A subscriber receives the confirmation then live events, and command
    requests still work while it streams (per-connection threads)."""
    import threading
    import time

    from chromectl import daemon as d

    monkeypatch.setattr(d, "_DIR", str(tmp_path))
    monkeypatch.setattr(d, "SOCK", str(tmp_path / "daemon.sock"))
    monkeypatch.setattr(d, "PIDFILE", str(tmp_path / "daemon.pid"))

    threading.Thread(target=d.serve, daemon=True).start()
    for _ in range(60):
        if d.is_running():
            break
        time.sleep(0.05)
    assert d.is_running()

    got = []
    listener = threading.Thread(target=lambda: got.extend(d.subscribe(max_seconds=2)))
    listener.start()
    time.sleep(0.3)                          # let the subscription register

    # a command still runs while a subscriber is streaming (concurrency)
    with d.Client() as c:
        assert c.call(["cheat", "--json"])["code"] == 0

    d._publish({"method": "GET", "url": "https://t/x", "status": 200, "type": "xhr"})
    listener.join(timeout=3)

    assert got and got[0]["event"] == "subscribed"
    assert any(e.get("event") == "request" and e["tx"]["url"] == "https://t/x" for e in got)
    assert d.stop() is True


def test_buffer_records_and_indexing(monkeypatch):
    """buffer_list summarizes and buffer_get indexes the ring (newest = -1)."""
    import collections

    from chromectl import daemon as d
    monkeypatch.setattr(d, "_IN_DAEMON", True)
    buf = collections.deque(maxlen=10)
    for i in range(3):
        rec = {"req": {"method": "GET", "url": f"https://t/{i}",
                       "headers": {"Cookie": "x"}, "postData": None},
               "type": "xhr", "status": 200, "mime": "application/json", "ts": i}
        buf.append(d._compact(f"r{i}", rec))
    monkeypatch.setattr(d, "_BUFFER", buf)
    listing = d.buffer_list(50)
    assert listing["count"] == 3
    assert listing["transactions"][-1]["url"] == "https://t/2"
    assert d.buffer_get(-1)["url"] == "https://t/2"        # most recent
    assert d.buffer_get(-3)["url"] == "https://t/0"
    assert d.buffer_get(-99) is None                       # out of range


# --- client: the prebuilt `cx` thin-client installer -----------------------
@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", "cx-linux-amd64"),
    ("Linux", "aarch64", "cx-linux-arm64"),
    ("Darwin", "arm64", "cx-darwin-arm64"),
    ("Darwin", "x86_64", "cx-darwin-amd64"),
    ("Windows", "AMD64", "cx-windows-amd64.exe"),
    ("Windows", "arm64", "cx-windows-arm64.exe"),
])
def test_client_asset_name_maps_platform_to_the_build_contract(system, machine, expected):
    assert cli._client_asset_name(system, machine) == expected


def test_client_asset_name_rejects_an_unknown_arch():
    with pytest.raises(cli.UserError) as ei:
        cli._client_asset_name("Linux", "sparc64")
    assert ei.value.kind == "not-found"


def test_client_install_from_file_roundtrip(tmp_path, monkeypatch):
    """--from a local file lands an executable at BIN_DIR/cx; status/uninstall agree.

    Uses the local path only — the download path can't be exercised without a
    real GitHub release, so it is intentionally not tested here.
    """
    bindir = tmp_path / "bin"
    monkeypatch.setattr(cli, "BIN_DIR", str(bindir))
    # keep the platform deterministic so the binary name is 'cx' (not cx.exe)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    # ensure the PATH check has something to say and never crashes
    monkeypatch.setenv("PATH", "/usr/bin")

    fake = tmp_path / "cx-src"
    fake.write_bytes(b"#!/bin/sh\necho cx\n")

    parser = cli.build_parser()
    result = cli.cmd_client(parser.parse_args(
        ["client", "install", "--from", str(fake), "--json"]))
    dest = bindir / "cx"
    assert result["ok"] and result["source"] == "local"
    assert result["installed"] == str(dest)
    assert dest.exists() and os.access(dest, os.X_OK)

    status = cli.cmd_client(parser.parse_args(["client", "status", "--json"]))
    assert status["installed"] is True and status["path"] == str(dest)

    removed = cli.cmd_client(parser.parse_args(["client", "uninstall", "--json"]))
    assert removed["removed"] is True
    assert not dest.exists()

    status = cli.cmd_client(parser.parse_args(["client", "status", "--json"]))
    assert status["installed"] is False and status["path"] is None


def test_client_install_from_dir_finds_the_platform_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "cx-linux-amd64").write_bytes(b"bin")
    parser = cli.build_parser()
    result = cli.cmd_client(parser.parse_args(
        ["client", "install", "--from", str(dist), "--json"]))
    assert result["ok"] and (tmp_path / "bin" / "cx").exists()
