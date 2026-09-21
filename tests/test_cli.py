"""Integration tests: a real headless Chrome, driven through the CLI.

Every assertion goes through `--json`, because that is the contract an agent
depends on — if a command's JSON shape breaks, these fail.
"""
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

from .conftest import CHROME_BINARIES, free_port


# --- basics ---------------------------------------------------------------
def test_version_json(jcli):
    got, rc = jcli("version")
    assert rc == 0
    assert "Browser" in got and "webSocketDebuggerUrl" in got


def test_open_read_close_roundtrip(jcli, page):
    with page("/index.html") as tid:
        listed, _ = jcli("list")
        assert any(t["id"] == tid for t in listed)
        got, rc = jcli("read", tid)
        assert rc == 0
        assert "Hello fixture" in got["markdown"]
        assert got["chars"] > 0


def test_eval_returns_a_typed_value(jcli, page):
    with page("/index.html") as tid:
        assert jcli("eval", tid, "document.title")[0] == "chromectl fixture"
        assert jcli("eval", tid, "1 + 1")[0] == 2
        assert jcli("eval", tid, "[1,2,3]")[0] == [1, 2, 3]


def test_text_and_html(jcli, page):
    with page("/index.html") as tid:
        txt, rc = jcli("text", tid)
        assert rc == 0 and "Hello fixture" in txt["text"]
        html, rc = jcli("html", tid)
        assert rc == 0 and "<h1>" in html["html"]
        assert html["chars"] == len(html["html"])


def test_extract_fields(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("extract", tid, "--field", "title=h1",
                       "--field", "prices=.price[]", "--field", "skus=.price[]@data-sku")
        assert rc == 0
        assert got["title"] == "Hello fixture"
        assert got["prices"] == ["9.99", "19.99"]
        assert got["skus"] == ["a1", "b2"]


def test_links_filters(jcli, page, server):
    with page("/index.html") as tid:
        ext, rc = jcli("links", tid, "--external")
        assert rc == 0
        assert any("example.com" in link["href"] for link in ext)
        assert not any(server in link["href"] for link in ext)


# --- the error contract ---------------------------------------------------
def test_failure_is_json_on_stdout_with_a_kind(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("eval", tid, "throw new Error('boom')")
        assert rc == 1
        assert got["ok"] is False
        assert got["error"]["kind"] == "js-exception"
        assert "boom" in got["error"]["message"]


def test_unknown_target_reports_no_target(jcli):
    got, rc = jcli("read", "definitely-not-a-real-tab-xyz")
    assert rc == 1
    assert got["error"]["kind"] == "no-target"


def test_timeout_has_its_own_kind(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("wait", tid, "--selector", "#never-appears", "--timeout", "800")
        assert rc == 1
        assert got["error"]["kind"] == "timeout"


def test_bad_args_are_reported_as_json(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("storage", tid, "--set", "no-equals-sign")
        assert rc == 1
        assert got["error"]["kind"] == "bad-args"


# --- navigation -----------------------------------------------------------
def test_back_forward_reload(jcli, page, server):
    with page("/index.html") as tid:
        assert jcli("goto", tid, server + "/page2.html")[0]["ok"]
        back, rc = jcli("back", tid)
        assert rc == 0 and back["url"].endswith("/index.html")
        fwd, rc = jcli("forward", tid)
        assert rc == 0 and fwd["url"].endswith("/page2.html")
        rel, rc = jcli("reload", tid, "--hard")
        assert rc == 0 and rel["ok"]


def test_back_at_the_start_of_history_fails_cleanly(jcli, page):
    with page("/index.html") as tid:
        jcli("back", tid)                      # burn the one entry we have
        got, rc = jcli("back", tid)
        assert rc == 1
        assert got["error"]["kind"] == "no-history"


# --- interaction ----------------------------------------------------------
def test_snapshot_returns_refs_and_click_uses_them(jcli, page):
    with page("/index.html") as tid:
        snap, rc = jcli("snapshot", tid)
        assert rc == 0 and snap["count"] > 0
        assert not os.path.exists(".chromectl-snap.json"), "snap file must not land in the CWD"
        assert snap["refs"].startswith(os.path.expanduser("~/.chromectl"))
        button = next(e for e in snap["elements"] if e["name"] == "Go")
        clicked, rc = jcli("click", tid, "--ref", button["ref"])
        assert rc == 0 and clicked["ok"]
        assert jcli("eval", tid, "document.getElementById('res').textContent")[0] == "clicked"


def test_click_by_role_and_name(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("click", tid, "--role", "button", "--name", "Go")
        assert rc == 0 and got["ok"]


def test_fill_form_and_submit(jcli, page):
    with page("/form.html") as tid:
        got, rc = jcli("fill-form", tid, "--set", "#user=ada", "--set", "#pass=secret",
                       "--submit", "#submit")
        assert rc == 0
        assert jcli("eval", tid, "document.getElementById('who').textContent")[0] == "ada"


def test_a11y_tree(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("a11y", tid)
        assert rc == 0 and got["count"] > 0
        roles = {n["role"] for n in got["nodes"]}
        assert "heading" in roles and "button" in roles
        heading = next(n for n in got["nodes"] if n["role"] == "heading")
        assert heading["name"] == "Hello fixture"


# --- storage, cookies, auth ----------------------------------------------
def test_storage_read_write_clear(jcli, page):
    with page("/index.html") as tid:
        assert jcli("storage", tid)[0]["items"]["seeded"] == "yes"
        got, rc = jcli("storage", tid, "--set", "token=abc")
        assert rc == 0 and got["items"]["token"] == "abc"
        assert jcli("storage", tid, "--get", "token")[0]["value"] == "abc"
        assert jcli("storage", tid, "--remove", "token")[0]["items"].get("token") is None
        assert jcli("storage", tid, "--clear")[0]["cleared"] is True
        assert jcli("storage", tid)[0]["items"] == {}


def test_session_storage_is_a_separate_area(jcli, page):
    with page("/index.html") as tid:
        jcli("storage", tid, "--session", "--set", "s=1")
        assert jcli("storage", tid, "--session")[0]["items"]["s"] == "1"
        assert "s" not in jcli("storage", tid)[0]["items"]


def test_cookie_set_and_delete(jcli, page):
    with page("/index.html") as tid:
        cookies, rc = jcli("cookies", tid, "--set", "sid=abc123")
        assert rc == 0
        assert any(c["name"] == "sid" and c["value"] == "abc123" for c in cookies)
        after, rc = jcli("cookies", tid, "--delete", "sid")
        assert rc == 0
        assert not any(c["name"] == "sid" for c in after)


def test_auth_save_round_trips_cookies_and_storage(jcli, page, tmp_path, server):
    state = tmp_path / "state.json"
    with page("/index.html") as tid:
        jcli("cookies", tid, "--set", "sid=round-trip")
        jcli("storage", tid, "--set", "tok=xyz")
        saved, rc = jcli("auth", "save", str(state))
        assert rc == 0 and saved["cookies"] >= 1
        assert server.replace("http://", "http://") in " ".join(saved["origins"])
    body = json.loads(state.read_text())
    assert body["version"] == 1
    assert any(c["name"] == "sid" for c in body["cookies"])
    origin = next(o for o in body["origins"] if o["localStorage"])
    assert origin["localStorage"]["tok"] == "xyz"
    assert oct(state.stat().st_mode)[-3:] == "600", "a saved login must not be world-readable"


def test_auth_load_restores_into_the_browser(jcli, page, tmp_path, cli):
    state = tmp_path / "state.json"
    with page("/index.html") as tid:
        jcli("cookies", tid, "--set", "restored=yes")
        jcli("storage", tid, "--set", "fromfile=1")
        jcli("auth", "save", str(state))
        jcli("cookies", tid, "--clear")
        jcli("storage", tid, "--clear")
        assert not any(c["name"] == "restored" for c in jcli("cookies", tid)[0])
        got, rc = jcli("auth", "load", str(state))
        assert rc == 0 and got["cookies"] >= 1
        assert any(c["name"] == "restored" for c in jcli("cookies", tid)[0])
        jcli("reload", tid)
        assert jcli("storage", tid)[0]["items"].get("fromfile") == "1"


def test_auth_load_of_a_missing_file_fails_cleanly(jcli):
    got, rc = jcli("auth", "load", "/nope/not/here.json")
    assert rc == 1 and got["error"]["kind"] == "bad-args"


# --- network --------------------------------------------------------------
def test_watch_is_bounded_and_returns_requests(jcli, page, cli, server):
    with page("/index.html") as tid:
        started = time.time()
        got, rc = jcli("watch", tid, "--max", "2", timeout=30)
        assert rc == 0
        assert time.time() - started < 20, "watch --max must actually stop"
        assert "requests" in got


def test_console_collects_logs_and_uncaught_errors(jcli, page):
    with page("/noisy.html") as tid:
        got, rc = jcli("console", tid, "--max", "2", timeout=30)
        assert rc == 0
        texts = " ".join(m["text"] for m in got["messages"])
        assert "hello from the page" in texts


def test_capture_summarises_transactions(jcli, server, tmp_path):
    har = tmp_path / "out.har"
    got, rc = jcli("capture", server + "/fetcher.html", "--max", "10",
                   "--har", str(har), timeout=60)
    assert rc == 0
    urls = [t["url"] for t in got["transactions"]]
    assert any(u.endswith("/fetcher.html") for u in urls)
    assert any(u.endswith("/data.json") for u in urls)
    assert har.exists()
    assert json.loads(har.read_text())["log"]["entries"]


def _spawn(port, *args):
    return subprocess.Popen(
        [sys.executable, "-m", "chromectl", "--port", str(port)] + [str(a) for a in args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_intercept_stubs_a_response(jcli, page, chrome, server):
    stub = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "stub.json")
    with page("/fetcher.html") as tid:
        assert jcli("eval", tid, "document.getElementById('out').textContent")[0] == '{"real":true}'
        proc = _spawn(chrome, "intercept", tid, "--stub", f"*data.json={stub}",
                      "--max", "8", "--json")
        time.sleep(1.5)                       # let Fetch.enable land before reloading
        jcli("reload", tid)
        time.sleep(2)
        out = jcli("eval", tid, "document.getElementById('out').textContent")[0]
        stdout, _stderr = proc.communicate(timeout=30)
        assert out == '{"stubbed":true}'
        report = json.loads(stdout)
        assert report["counts"].get("stub") == 1


def test_intercept_blocks_a_request(jcli, page, chrome):
    with page("/fetcher.html") as tid:
        proc = _spawn(chrome, "intercept", tid, "--block", "*data.json*", "--max", "8", "--json")
        time.sleep(1.5)
        jcli("reload", tid)
        time.sleep(2)
        out = jcli("eval", tid, "document.getElementById('out').textContent")[0]
        stdout, _stderr = proc.communicate(timeout=30)
        assert out.startswith("ERR")
        assert json.loads(stdout)["counts"].get("block") == 1


def test_intercept_needs_something_to_do(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("intercept", tid, "--max", "1")
        assert rc == 1 and got["error"]["kind"] == "bad-args"


def test_download_lands_a_file_under_its_real_name(jcli, page, tmp_path, server):
    with page("/index.html") as tid:
        got, rc = jcli("download", tid, "--url", server + "/payload.bin",
                       "--dir", str(tmp_path / "dl"), "--wait", "20", timeout=60)
        assert rc == 0, got
        assert got["count"] == 1
        entry = got["downloads"][0]
        assert entry["name"] == "payload.bin"
        assert entry["state"] == "completed"
        assert os.path.exists(entry["path"])
        assert os.path.getsize(entry["path"]) == 2048


# --- run ------------------------------------------------------------------
def test_run_json_reports_every_step(jcli, server):
    got, rc = jcli("run",
                   "--step", f"open {server}/form.html",
                   "--step", "wait --selector #user",
                   "--step", "fill-form --set #user=ada --set #pass=pw --submit #submit",
                   "--step", "read", timeout=120)
    assert rc == 0, got
    assert got["ok"] is True and got["failed"] == 0
    assert [r["step"] for r in got["results"]] == [1, 2, 3, 4]
    assert all(r["ok"] for r in got["results"])
    assert got["results"][0]["result"]["id"]
    assert "Sign in" in got["results"][3]["result"]["markdown"]


def test_run_carries_the_opened_tab_to_later_steps(jcli, server):
    got, rc = jcli("run",
                   "--step", f"open {server}/index.html",
                   "--step", "eval document.title", timeout=90)
    assert rc == 0
    assert got["results"][1]["result"] == "chromectl fixture"


def test_run_json_reports_a_failing_step_without_losing_the_others(jcli, server):
    got, rc = jcli("run", "--keep-going",
                   "--step", f"open {server}/index.html",
                   "--step", "wait --selector #nope --timeout 700",
                   "--step", "eval document.title", timeout=120)
    assert rc == 1
    assert got["ok"] is False and got["failed"] == 1
    assert got["results"][1]["error"]["kind"] == "timeout"
    assert got["results"][2]["ok"] is True, "--keep-going must run the remaining steps"


def test_run_stops_at_the_first_failure_by_default(jcli, server):
    got, rc = jcli("run",
                   "--step", f"open {server}/index.html",
                   "--step", "wait --selector #nope --timeout 700",
                   "--step", "eval document.title", timeout=120)
    assert rc == 1
    assert len(got["results"]) == 2, "without --keep-going the batch stops"


def test_run_needs_steps(jcli):
    got, rc = jcli("run", "--step", "  ")
    assert rc == 1 and got["error"]["kind"] == "bad-args"


# --- audits ---------------------------------------------------------------
def test_seo_audit(jcli, page):
    with page("/index.html") as tid:
        got, rc = jcli("seo", tid)
        assert rc == 0
        assert got["title"] == "chromectl fixture"
        assert got["lang"] == "en"
        assert got["h1"] == ["Hello fixture"]


def test_screenshot_writes_a_png(jcli, page, tmp_path):
    shot = tmp_path / "shot.png"
    with page("/index.html") as tid:
        got, rc = jcli("screenshot", tid, "--out", str(shot), "--full")
        assert rc == 0 and got["ok"]
        assert shot.exists() and shot.read_bytes()[:4] == b"\x89PNG"


# --- the skill ------------------------------------------------------------
def test_skill_install_and_refusal_to_clobber(cli, tmp_path):
    p = cli("skill", "install", "--dir", str(tmp_path), "--json")
    assert p.returncode == 0
    dest = tmp_path / "chromectl" / "SKILL.md"
    assert dest.exists()
    assert dest.read_text().startswith("---\nname: chromectl")

    again = cli("skill", "install", "--dir", str(tmp_path), "--json")
    assert again.returncode == 1
    assert json.loads(again.stdout)["error"]["kind"] == "exists"

    forced = cli("skill", "install", "--dir", str(tmp_path), "--force", "--json")
    assert forced.returncode == 0


# --- launching something that is not Chrome, and adopting what we didn't start ---
# No Electron app exists on a CI box, so the `--app` code path is exercised with
# Chrome standing in as the "app", and the refusal path with a binary that is not a
# browser at all. The one behaviour that genuinely needs Electron — Target.createTarget
# answering "Not supported" — is covered in test_unit.py against a fake CDP instead.

def test_app_mode_injects_only_the_debug_port(manage, tmp_path):
    """`--app` must not dictate headless or a profile: an app keeps its own session.
    Chrome stands in for the app here; what's asserted is the flags we chose."""
    chrome_bin = next((shutil.which(b) for b in CHROME_BINARIES if shutil.which(b)), None)
    if not chrome_bin:
        pytest.skip("no Chrome/Chromium on PATH")
    port = free_port()
    name = f"pytest-app-{port}"
    # Chrome 136+ refuses the debug port on a default profile, so pass one explicitly —
    # which also covers the "app with opt-in isolation" branch.
    p = manage("start", "--app", chrome_bin, "--name", name, "--port", port,
               "--profile", str(tmp_path / "prof"), "--chrome-arg=--headless=new", "--json")
    try:
        assert p.returncode == 0, p.stdout + p.stderr
        got = json.loads(p.stdout)
        assert got["ok"] is True and got["port"] == port
        # it sniffs what actually answered rather than echoing how we launched it
        assert got["kind"] == "chrome", "Chrome is not Electron, and must not claim to be"
        listed = manage("--port", port, "list", "--json")
        assert listed.returncode == 0, listed.stderr
    finally:
        manage("stop", name, "--force")


def test_a_binary_that_never_opens_the_port_is_a_typed_error(manage):
    """The 'you pointed me at something that isn't Electron' case."""
    if not os.path.exists("/bin/true"):
        pytest.skip("no /bin/true")
    port = free_port()
    p = manage("start", "--app", "/bin/true", "--name", f"pytest-dead-{port}",
               "--port", port, "--wait", "3", "--json")
    assert p.returncode != 0
    got = json.loads(p.stdout)
    assert got["ok"] is False
    assert got["error"]["kind"] == "launch-failed"
    assert "code 0" in got["error"]["message"]


@pytest.fixture
def foreign_chrome(tmp_path):
    """A Chrome this suite launches by hand, NOT through chromectl.

    Adopting is only honestly tested against a process chromectl knows nothing
    about — adopting one it started would exercise the relabel path instead.
    """
    chrome_bin = next((shutil.which(b) for b in CHROME_BINARIES if shutil.which(b)), None)
    if not chrome_bin:
        pytest.skip("no Chrome/Chromium on PATH")
    port = free_port()
    proc = subprocess.Popen(
        [chrome_bin, "--headless=new", f"--remote-debugging-port={port}",
         f"--user-data-dir={tmp_path / 'foreign'}", "--no-first-run"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", port), 0.3):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.skip("hand-launched Chrome never opened its port")
        yield port
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=20)


def test_adopt_records_a_browser_we_did_not_start(manage, foreign_chrome):
    name = f"pytest-adopt-{foreign_chrome}"
    p = manage("adopt", foreign_chrome, "--name", name, "--json")
    assert p.returncode == 0, p.stdout + p.stderr
    got = json.loads(p.stdout)
    assert got["managed"] is False, "chromectl did not start this one"
    assert got["pid"] is None
    assert got["kind"] == "chrome"
    assert got["browser"], "should record what answered /json/version"
    assert manage("--port", foreign_chrome, "list", "--json").returncode == 0


def test_stop_refuses_to_kill_an_adopted_browser(manage, foreign_chrome):
    """The pkill fallback matches on the debug port. Without the guard, cleaning up
    an adopted entry would quit the user's real, signed-in application."""
    name = f"pytest-keep-{foreign_chrome}"
    assert manage("adopt", foreign_chrome, "--name", name, "--json").returncode == 0
    refused = manage("stop", name)
    assert refused.returncode != 0
    assert "--force" in refused.stderr and "--forget" in refused.stderr
    # the browser is untouched...
    assert manage("--port", foreign_chrome, "version", "--json").returncode == 0, \
        "a refused stop must not have killed it"
    # ...and the entry is still there, because refusing is not forgetting
    assert any(i["name"] == name for i in json.loads(manage("instances", "--json").stdout))
    # --forget drops the record and still leaves the process alone
    assert manage("stop", name, "--forget").returncode == 0
    assert manage("--port", foreign_chrome, "version", "--json").returncode == 0
    assert not any(i["name"] == name for i in json.loads(manage("instances", "--json").stdout))


def test_adopting_a_port_we_manage_keeps_its_provenance(manage):
    """Relabelling must not launder away the fact that we started it: the pid would
    be lost and `stop` would then refuse to clean up a browser that is ours, leaving
    it running with nothing left that knows how to reach it."""
    chrome_bin = next((shutil.which(b) for b in CHROME_BINARIES if shutil.which(b)), None)
    if not chrome_bin:
        pytest.skip("no Chrome/Chromium on PATH")
    port = free_port()
    started = f"pytest-mine-{port}"
    p = manage("start", "--name", started, "--port", port, "--ephemeral", "--json")
    if p.returncode != 0:
        pytest.skip(f"could not start Chrome: {p.stderr[-300:]}")
    relabelled = f"pytest-relabel-{port}"
    try:
        got = json.loads(manage("adopt", port, "--name", relabelled, "--force", "--json").stdout)
        assert got["managed"] is True, "we did start this one"
        assert got["pid"], "the pid must survive a relabel"
        # and because it survived, stop can still clean it up
        assert manage("stop", relabelled, "--purge").returncode == 0
        assert manage("--port", port, "version", "--json").returncode != 0, "should be gone"
    finally:
        manage("stop", relabelled, "--purge", "--force")
        manage("stop", started, "--purge", "--force")


def test_open_returns_a_loaded_tab_not_a_blank_one(jcli, cli, server):
    """Target.createTarget returns before the tab has navigated. If `open` hands that
    back, its --json reports url:"" and the very next step reads the blank page —
    which is exactly the `run --step 'open URL' --step 'read'` pattern the docs push."""
    got, rc = jcli("open", server + "/index.html")
    try:
        assert rc == 0, got
        assert got["url"].endswith("/index.html"), f"url not settled: {got['url']!r}"
        assert got["title"] == "chromectl fixture", f"title not settled: {got['title']!r}"
        # and it is immediately readable, with no wait in between
        title, rc2 = jcli("eval", got["id"], "document.title")
        assert rc2 == 0 and title == "chromectl fixture"
    finally:
        cli("close", got["id"])
