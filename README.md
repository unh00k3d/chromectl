# chromectl — a friendly CLI for the Chrome DevTools Protocol

Drive a Chrome/Chromium instance from the shell over its remote debugging port.
Everything is the Chrome DevTools Protocol (CDP) under the hood; this wraps it in
ergonomic subcommands with nice output. Not just network — JS execution, console
logs/errors, screenshots, PDFs, cookies, SEO audits, page-reading, form flows,
Core Web Vitals / Lighthouse, and a raw escape hatch.

## Install

Install straight from the repo with pipx:

```bash
pipx install git+https://github.com/unh00k3d/chromectl.git
# or, from a local clone:  pipx install .
npm i -g lighthouse                                       # optional — only for `lighthouse`
```

pipx puts the CLI on your PATH in an isolated env and pulls all Python deps
(`websocket-client`, `rich`, `playwright`, `readability-lxml`, `markdownify`).
**No `playwright install` needed** — the interaction commands *attach* to your Chrome
over CDP rather than launching their own browser. Playwright and the read/markdown
libs are imported lazily, so the core commands stay fast.

### Fast by default

The install ships **two** commands. `chromectl-py` is the Python CLI. `chromectl`
is a tiny native front-end (compiled from `client/cx` at install time when a Go
toolchain is present) that forwards each command to a running daemon over a
socket — **~5ms vs ~100ms of Python startup, ~20× faster** — and falls back to
`chromectl-py` automatically when no daemon is up or a command can't be routed.
You always type `chromectl`; the speed is transparent. Turn it on for a session
by starting the daemon once:

```bash
chromectl daemon start              # resident process holding warm connections
chromectl eval t "1+1" --json       # now served by the daemon; ~5ms
```

**Install Go first** (e.g. `apt install golang` / `brew install go`) if you want
the fast `chromectl` — without it, only `chromectl-py` is installed and you can
add the front-end later with `chromectl-py client install --build`, then put
`~/.chromectl/bin` first on PATH.

## 1. Launch a browser

```bash
chromectl start                                   # headless, port 9222, PERSISTENT profile
chromectl start --headful --port 9223             # visible window, custom port
chromectl start --profile ~/.cache/chromectl      # custom profile dir
chromectl start --ephemeral                       # throwaway profile in /tmp (no persistence)
chromectl start --copy-profile                    # copy your REAL Chrome profile (logins!) then launch
chromectl start --from-profile /path/to/profile   # copy from a specific profile dir
chromectl start --proxy user:pass@10.0.0.1:8080   # behind a proxy (credentials handled)
chromectl start -- --lang=tr --disable-gpu        # pass any extra Chrome flags
chromectl start --app slack                      # any Electron app (see below)
```

### Extra Chrome flags

Anything after a bare `--` goes straight to Chrome, or pass them one at a time with
`--chrome-arg` (write `--chrome-arg=--flag` when the value starts with a dash):

```bash
chromectl start --name tr -- --lang=tr --window-size=1280,800 --host-resolver-rules='MAP * 1.2.3.4'
chromectl start --chrome-arg=--disable-dev-shm-usage --chrome-arg=--blink-settings=imagesEnabled=false
```

Your flags **override** the ones chromectl sets, so `-- --headless=old` or
`-- --user-data-dir=/tmp/p` win — and the registry records the values Chrome actually got.
`instances --json` lists them under `chrome_args`.

### Proxies

```bash
chromectl start --proxy 10.0.0.1:8080                        # http proxy (default scheme)
chromectl start --proxy socks5://10.0.0.1:1080               # socks5 (also https://, socks4://)
chromectl start --proxy socks5://ada:secret@10.0.0.1:1080    # with credentials
chromectl start --proxy 10.0.0.1:8080 --proxy-auth 'ada:p@ss:word'   # creds outside the URL
chromectl start --proxy 10.0.0.1:8080 --proxy-bypass 'localhost,*.internal'
chromectl start --proxy-pac http://wpad/proxy.pac            # PAC file instead
```

Chrome has no way to accept proxy **credentials** on the command line — it opens a login
dialog, which is no help headless. So when your proxy needs a username/password, chromectl
starts a tiny local relay (`chromectl.proxyrelay`), points Chrome at it, and the relay adds
the credentials on the way upstream — HTTP `CONNECT` tunnels and SOCKS5 alike. The relay
belongs to the instance: `stop` shuts it down with the browser, and `instances` shows it.

- Credentials are handed to the relay through the environment, never argv (argv is visible
  to every user via `ps`), and `instances` masks the password.
- The relay listens on 127.0.0.1 only, and speaks to one upstream proxy.
- SOCKS4 has no password auth — use `socks5://` or an HTTP proxy if you need credentials.
- Chrome **bypasses proxies for loopback** by default. To route `127.0.0.1` traffic through
  the proxy too (useful when testing), add `--proxy-bypass '<-loopback>'`.
- Nothing else changes: chromectl still talks CDP to the browser directly on localhost.

`--copy-profile` auto-detects your default Chrome user-data-dir (per-OS), copies it into
`--profile` (skipping caches), and launches from the copy — so you debug **with your real
logins** without touching/using the original profile. Close Chrome using that profile first
so its files aren't mid-write. Security: the copy holds your live cookies/sessions — anyone
who reaches the debug port can act as you, so keep it local and delete it when done.

### Multiple instances
Run and manage several browsers at once, each on its own port, addressed by name:

```bash
chromectl start --name work                 # instance on 9222
chromectl start --name scratch --auto-port  # a second, on the next free port
chromectl instances                         # list them + up/down status (--json, --prune)

chromectl -i scratch open https://example.com   # target by name (or --port 9223)
chromectl -i work read --json

chromectl stop scratch                      # stop one by name/port
chromectl stop --all                        # stop every managed instance
```

`-i NAME` (or `--port N`) selects which browser every command talks to. The registry lives
at `~/.chromectl/instances.json`.

Notes:
- **Profiles persist by default.** Each instance gets a stable dir at
  `~/.chromectl/profiles/<name>` that's **reused every time you start that name**, so
  cookies/logins survive restarts. Use `--profile PATH` for a custom location,
  `--ephemeral` for a throwaway `/tmp` profile, or `stop <name> --purge` to delete it.
- A **non-default profile is mandatory** since Chrome 136 — the real default profile
  refuses the debug port (anti-cookie-theft). `start`'s profile is non-default; to reuse
  your logins, copy your real one first: `cp -r ~/.config/google-chrome /tmp/prof` then
  `chromectl start --profile /tmp/prof`.
- `start` sets `--remote-allow-origins=*` so clients (incl. Playwright) can connect.
- Anyone who can reach the port has **full, unauthenticated control** of that browser
  (read cookies/sessions, run JS, read traffic). Keep it on localhost; never forward it.

### Electron apps (any of them)

Slack, Discord, VS Code, Obsidian, Signal, Telegram Desktop — an Electron app *is*
Chromium, so once it is listening the whole CLI works against it. There is no list of
supported apps in the code: you point `--app` at any executable.

```bash
chromectl start --app slack --name slack       # a name on PATH, or a full path
chromectl start --app /opt/Discord/Discord --name discord --auto-port
chromectl -i slack list                        # its windows
chromectl -i slack read --json                 # same commands as any browser
chromectl -i slack capture --attach slack --max 20   # what it sends over the wire
```

Already launched it yourself? Record the port instead, and `-i NAME` works the same:

```bash
slack --remote-debugging-port=9222 &
chromectl adopt 9222 --name slack
```

Four things to know:

- **It keeps its own profile.** chromectl passes an app **only**
  `--remote-debugging-port` — never `--user-data-dir`, which would hand you a
  signed-out app and defeat the point. Pass `--profile DIR` or `--ephemeral` to opt
  into an isolated one.
- **Quit it first.** Electron's single-instance lock forwards a second launch to the
  process already running and silently drops our flags. `start` notices, fails with
  `launch-failed`, and prints the app's own output so you can see what it said.
- **No tab model.** `Target.createTarget` is unimplemented in Electron, so `open` and
  `close` fail — with the app's own words ("Not supported"), not ours. Everything that
  drives an *existing* window is fine: `goto`, `eval`, `read`, `console`, `intercept`,
  `snapshot`/`click`/`fill`, `a11y`, `storage`, `cookies`, plus `perf --attach`,
  `capture --attach`, and `seo <target>` instead of `seo <url>`. App windows show up as
  `page` **or** `webview` targets; `chromectl list` shows both.
- **This is your real session.** A debug port on Slack is far more dangerous than one on
  a throwaway browser — anyone who reaches it can read every message and act as you.
  Keep it on localhost, and stop the instance when you're done. For the same reason
  `chromectl stop` **refuses** to kill an instance it did not start: use
  `stop NAME --forget` to drop the record, or `--force` to insist.

If a WebSocket connection is refused, add `--chrome-arg --remote-allow-origins='*'`.

**Not in scope:** Microsoft Teams and other [WebView2](https://learn.microsoft.com/en-us/microsoft-edge/webview2/how-to/remote-debugging-desktop)
apps are Edge-embedded rather than Electron, and need a Windows env var or registry key
rather than a flag. On Linux, Teams is a PWA that the ordinary Chrome path already covers.

## 2. Use it

```bash
chromectl list                      # open tabs/targets
chromectl version                   # browser + protocol info
chromectl open https://example.com  # open a tab
chromectl close example             # close a tab (by url/title substring or id-prefix)

chromectl goto example https://news.ycombinator.com   # navigate
chromectl eval example "document.title"               # run JS, get the value back
chromectl eval example "({t: document.title, links: document.links.length})"
chromectl html example --out page.html                # dump HTML
chromectl text example                                # visible text
chromectl cookies example                             # cookie table (--json for raw)
chromectl screenshot example --full --out shot.png    # full-page screenshot
chromectl pdf example --out page.pdf                  # print to PDF

chromectl console example                             # tail console.* + JS errors (Ctrl-C)
chromectl watch example                               # live one-line-per-request network tail

chromectl capture https://github.com --print 3        # Burp-style full req/resp capture
chromectl capture https://api.github.com/ --type xhr --har out.har   # filter + HAR export
chromectl capture --attach example --reload           # capture an existing tab from reload

chromectl seo https://example.com                     # on-page SEO audit (open+audit+close)
chromectl seo example                                 # audit an already-loaded tab

# --- run a whole flow in ONE process (script/batch) ---
chromectl run steps.txt                    # one command per line (# comments ok)
chromectl run --step "open https://site/login" \
          --step "wait --selector #user" \
          --step "fill-form --set #user=ada --set #pass=pw --submit #go" \
          --step "wait --url /dashboard" \
          --step "read --json"
# opened tab auto-becomes the target for later steps; one connection; state persists; --keep-going to continue on error

# --- read & extract (turn pages into data) ---
chromectl read example                                    # main content as clean Markdown
chromectl read example --json                             # {url,title,markdown,chars}
chromectl extract example --field "title=h1" --field "prices=.price[]" --field "img=img@src"
chromectl links example --external --json                 # list links (filter internal/external)

# --- reliable flows ---
chromectl wait example --selector "#results" --timeout 8000   # wait until it's visible
chromectl wait example --network-idle                          # or: --text "Done" / --url "/checkout" / --gone
chromectl fill-form login --set "#user=ada" --set "#pass=secret" --submit "#go"

# EVERY command accepts --json — one plain JSON value on stdout:
chromectl list --json ; chromectl cookies example --json ; chromectl seo example --json

# --- performance (Tier 3) ---
chromectl perf https://example.com                        # Core Web Vitals (LCP/CLS/INP/FCP/TTFB)
chromectl perf https://example.com --out trace.json       # + raw trace (DevTools ▸ Performance ▸ Load)
chromectl perf --attach example --reload                  # measure an existing tab
chromectl lighthouse https://example.com --preset desktop # full Lighthouse audit (needs the CLI)
chromectl lighthouse https://example.com --categories performance,seo --out report.json

# --- robust interaction via Playwright-over-CDP (Tier 2) ---
chromectl snapshot example                                # list interactive elements + save refs
chromectl click example --role button --name "Sign in"    # click by ARIA role + name
chromectl click example --ref 3                           # click element #3 from last snapshot
chromectl click example --text "Add to cart"              # click by visible text
chromectl fill example "ada@x.com" --selector "#email" --enter   # fill + submit (auto-waits)
chromectl hover example --selector ".menu"                # hover (reveals submenus)

# --- interaction / emulation (Tier 1) ---
chromectl resize example 390 844 --mobile --shot m.png   # viewport + full-page screenshot
chromectl emulate example --color dark --shot dark.png   # dark mode
chromectl emulate example --geo 48.85,2.35 --throttle slow-3g --hold   # geo + network (held)
chromectl type example "hello" --selector "#search" --enter   # type into a field
chromectl press example Enter                              # press key(s): Enter/Tab/ArrowDown/a…
chromectl upload example ./photo.png --selector "#file"   # set a file <input>
chromectl dialog example --accept --text "Ada"            # auto-answer alert/confirm/prompt
chromectl heap example --out heap.heapsnapshot            # V8 heap snapshot (DevTools ▸ Memory)

# --- navigation & state ---
chromectl back example ; chromectl forward example ; chromectl reload example --hard
chromectl storage example --json                          # localStorage (--session for sessionStorage)
chromectl storage example --set token=abc --json          # …and write it
chromectl cookies example --set 'sid=abc' --json          # cookies are writable too (--delete, --clear)
chromectl auth save session.json                          # cookies + per-origin web storage
chromectl auth load session.json                          # replay that login into any instance

# --- accessibility & interception ---
chromectl a11y example --json                             # the tree a screen reader sees
chromectl intercept example --block '*doubleclick*' --max 10      # block requests
chromectl intercept example --stub '*/api/me=fake.json' --max 10  # serve a fixed body
chromectl intercept example --header 'X-Test: 1' --max 10         # add a request header
chromectl download example --url https://site/a.pdf --dir ./out   # headless downloads need arming

# --- electron apps ---
chromectl start --app slack --name slack              # launch any Electron app
chromectl adopt 9222 --name slack                     # or adopt one already listening
chromectl -i slack goto slack https://app.slack.com/  # `open` has no meaning: no tabs
chromectl stop slack --forget                         # drop the record, leave it running

chromectl skill install                               # teach a coding agent this CLI
chromectl raw browser Browser.getVersion              # raw CDP command (browser target)
chromectl raw example Runtime.evaluate '{"expression":"1+1","returnByValue":true}'
chromectl proto Network                               # protocol lookup: a domain…
chromectl proto Network.getResponseBody               # …a command's params/returns
chromectl repl example                                # interactive CDP prompt
```

### Target selection
Anywhere a command takes a target you can pass:
- an **id-prefix** (`B1B0`), a **url/title substring** (`example`, `github`),
- `browser` for the browser-level target (for `raw`/`repl`),
- or **nothing** to use the first open page.

### Emulation caveat
CDP overrides (`emulate`/`resize` viewport, geo, throttle, color-scheme, UA) live on
the **CDP connection** and revert when it closes. Since each `chromectl` command connects,
acts, and disconnects, use `--shot` to capture in the same session, or `--hold` to keep
the connection open (Ctrl-C to release) so the override persists while you do other work.

### Interaction (Tier 2) — how it works
`snapshot`/`click`/`fill`/`hover` use **Playwright attached to your Chrome over CDP**
(`connect_over_cdp`) — no separate browser is launched. You get Playwright's
auto-waiting and actionability checks (waits for the element to exist, be visible, and
be stable before acting) instead of hand-rolled timing. `snapshot` saves its refs to
`~/.chromectl/snaps/<host>-<port>.json` so `--ref N` works in a later, separate command
(the stateless equivalent of the MCP's element uids) — per instance, so two browsers
never clobber each other's refs, and never in your working directory. `snapshot --json`
returns the same elements inline. Locate an element by any of: `--ref`, `--selector`,
`--text`, or `--role`+`--name`.

### `--json` everywhere (the agent contract)

Every command takes `--json` and prints exactly one JSON value on stdout. **Failures
do too** — `{"ok": false, "error": {"kind": "timeout", "message": "…"}}`, still with a
non-zero exit — so a script that parses stdout always gets a value instead of an empty
string plus red prose on stderr. `kind` is one of `bad-args`, `no-instance`, `no-target`, `not-found`, `exists`, `timeout`, `js-exception`, `no-snapshot`, `no-history`, `no-storage`, `close-failed`, `launch-failed`, `missing-dep`, `tool-failed`, `connection`, `cdp`.

`run --json` runs every step in its own JSON mode and returns the whole batch:

```console
$ chromectl run --json --step 'open https://site' --step 'wait --selector #nope --timeout 800'
{
  "ok": false, "steps": 2, "failed": 1,
  "results": [
    {"step": 1, "cmd": "open https://site", "ok": true,
     "result": {"ok": true, "id": "AE21…", "url": "https://site"}},
    {"step": 2, "cmd": "wait --selector #nope --timeout 800", "ok": false,
     "error": {"kind": "timeout", "message": "timeout waiting for selector '#nope'"}}
  ]
}
```

`watch`, `console` and `intercept` stream until interrupted — pass `--max SECONDS` to
bound them, and under `--json` they hand back one array at the end.

### Reusing a login (`auth`)

```bash
chromectl auth save session.json    # cookies (browser-wide) + per-origin localStorage/sessionStorage
chromectl start --name fresh --ephemeral
chromectl -i fresh auth load session.json
```

Log in once, replay everywhere — including into a throwaway instance. The file is
written mode 600 and **is a live session**: treat it like a password. This is the
narrow, safer alternative to `--copy-profile`, which clones your whole real profile.

### For coding agents (`skill`)

`AGENTS.md` only helps inside this repo, but chromectl is installed globally — so the
docs ship with the binary:

```bash
chromectl skill install           # → ~/.claude/skills/chromectl/SKILL.md
chromectl skill install --dir .   # → ./chromectl/SKILL.md, for any agent harness
chromectl skill print             # to stdout
```

The command table inside the skill is generated from the argument parser at install
time, so it cannot drift from the real surface.

### Handy options
- `--host` / `--port` (or env `CDP_HOST` / `CDP_PORT`) — default `localhost:9222`.
- `capture`: `--print N`, `--out FILE`, `--har FILE`, `--type TYPE`,
  `--no-bodies`, `--bodycap N`, `--max SECONDS`, `--quiet SECONDS`,
  `--attach TARGET`, `--reload`.

## How it works (the workflow)

`HTTP /json/version` → open ONE WebSocket to the browser (or a per-tab socket) →
send CDP commands (`{id, method, params}`) and receive replies + unsolicited
events → for capture: `Network.enable` **before** navigating, correlate events by
`requestId`, fetch each body on `loadingFinished` (before Chrome evicts it), merge
the `*ExtraInfo` events for the real cookies/headers, and reconstruct raw HTTP.

A background thread reads frames into a queue so `call()` (request/response) and
the capture/console/watch loops (event streams) share one connection cleanly.

**Gotcha baked in:** `websocket-client` sends an `Origin` header by default, which
modern Chrome rejects on the debug port. `chromectl` sets `suppress_origin=True` so
connections are accepted without relaunching Chrome.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest                 # unit tests + browser-driven integration tests
.venv/bin/pytest tests/test_unit.py   # just the ones that need no browser
```

The integration tests start and stop their own headless Chrome on a free port with a
throwaway profile, and serve fixtures from `tests/fixtures` over a local HTTP server —
they never touch your own instances. They skip cleanly when no Chrome is on PATH.

## Layout
- `chromectl/cli.py` — the CLI.
- `chromectl/SKILL.md` — the agent skill, installed by `chromectl skill install`.
- `chromectl/protocol.json` — bundled CDP schema used by `chromectl proto`/`cheat` (falls back to live).
- `chromectl/proxyrelay.py` — the local relay that adds proxy credentials upstream.
- `tests/` — pytest suite (`test_unit.py` needs no browser; `test_cli.py` drives a real one).
- `pyproject.toml` — packaging; `chromectl` console entry point.
- `AGENTS.md` — the agent-facing interface reference (auto-read by coding agents).

## For agents
Run `chromectl skill install` once — that drops a skill where coding agents look, so
they learn this CLI in any project, not just this repo. Inside this repo, `AGENTS.md`
is the same reference; `chromectl cheat --json` returns the surface as data.
