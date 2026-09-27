# chromectl — agent reference

A single CLI that drives Chrome over the DevTools Protocol. **This file is the complete interface — you do not need to run `--help` per command.** Re-fetch the surface as data anytime: `chromectl cheat --json`.

## Launch & manage Chrome instances

```bash
chromectl start                       # instance on 9222; profile persists at ~/.chromectl/profiles/chrome-9222
chromectl start --name work --auto-port   # a second instance on a free port, named 'work'
chromectl start --name work           # start 'work' again later -> SAME profile, logins persist
chromectl start --copy-profile        # launch with a COPY of your real profile (logins)
chromectl start --ephemeral           # throwaway /tmp profile (no persistence)
chromectl start --proxy user:pass@host:8080   # proxy (credentials handled for you)
chromectl start -- --lang=tr --window-size=1280,800   # any extra Chrome flags
chromectl start --app slack --name slack   # any Electron app (keeps ITS OWN profile/login)
chromectl adopt 9222 --name slack     # record a port someone else opened, so -i works
chromectl instances                   # list managed instances + up/down (--json, --prune)
chromectl stop work                   # stop by name/port; stop --all; stop work --purge (delete profile)
```

`--app PATH` launches any Electron-based application (a name on PATH or a path) with the
debug port open. We inject **only** `--remote-debugging-port` — never `--user-data-dir`,
because the point is to drive the user's real, signed-in app; pass `--profile`/`--ephemeral`
to opt into isolation. The app **must not already be running**: a single-instance lock hands
our flags to the first process and drops them, and `start` then fails with `launch-failed`
carrying the process's own output.

An Electron app has **no tab model**, so `open`/`close` fail with kind `cdp` and the app's own
message; use `list` + `goto`, `perf/capture --attach`, and give `seo` a target not a URL.
Windows may be `page` **or** `webview` targets. `stop` **refuses** to kill an instance you
adopted rather than started — `--forget` drops the record, `--force` insists.

Profiles **persist by default** per name — cookies/logins survive restarts. Target an instance with **`-i NAME`** (or `--port N`) before the subcommand:

```bash
chromectl -i work open https://site && chromectl -i work read --json
```

## Proxies and extra Chrome flags (`start` only)

```bash
chromectl start --proxy 10.0.0.1:8080                  # http proxy (scheme defaults to http)
chromectl start --proxy socks5://ada:secret@10.0.0.1:1080   # socks5 + credentials
chromectl start --proxy 10.0.0.1:8080 --proxy-auth 'ada:p@ss:word'  # creds kept out of the URL
chromectl start --proxy 10.0.0.1:8080 --proxy-bypass 'localhost,*.internal'
chromectl start --proxy-pac http://wpad/proxy.pac      # PAC file instead of --proxy
```

Chrome cannot take proxy credentials on the command line (it pops a login dialog, useless
headless), so when the proxy has a username/password chromectl runs a small local relay that
adds them upstream and points Chrome at that; `stop` shuts the relay down with the browser.
Credentials never appear in argv, and `instances` shows the password masked. Loopback is
exempt from the proxy by Chrome's own default — pass `--proxy-bypass '<-loopback>'` to send
127.0.0.1 traffic through it too.

Extra Chrome flags go after a bare `--`, or one at a time with `--chrome-arg`
(use `--chrome-arg=--flag` when the value starts with a dash). A flag you pass **overrides**
the same flag chromectl sets, so `-- --headless=old` or `-- --user-data-dir=/tmp/p` win:

```bash
chromectl start --name tr -- --lang=tr --disable-gpu --host-resolver-rules='MAP * 1.2.3.4'
chromectl start --chrome-arg=--disable-dev-shm-usage --chrome-arg=--window-size=640,480
```

## Prefer `run` for multi-step tasks

A sequence in one `run` executes in a single process over one persistent connection; state persists across steps and the tab you `open` becomes the implicit target for later steps. **Note:** step lines are shlex-split, so escape inner quotes in JS (e.g. `eval "localStorage.getItem('k')"` as a standalone command is easier than inside a step).

```bash
chromectl -i work run --step 'open https://site/login' \
  --step 'wait --selector #user' \
  --step 'fill-form --set #user=ada --set #pass=secret --submit #go' \
  --step 'wait --url /dashboard' --step 'read'
```

Add `--json` to the `run` itself (not to the steps) to get every step's result back
as one array — see below.

## Conventions (apply everywhere)

- **instance**: `-i NAME` / `--port N` selects which browser. `chromectl instances` lists them. Profiles persist per name.
- **target** (a tab): id-prefix, url/title substring, `browser`, or empty = first page. In `run`, omit to use the current tab. Prefer `list --json` + id-prefix.
- **`--json`** works on **every** command → one plain, pipeable JSON value on stdout (errors included). See below.
- Put **options after positionals**. Locate elements (click/fill/hover/wait) by `--selector`, `--text`, `--role`+`--name`, or `--ref N` (from last `snapshot`).
- **Emulation** (`emulate`/`resize`) reverts on exit — use `--shot`, `--hold`, or an `emulate` step in a `run`.
- Non-zero exit on failure. `lighthouse` needs `npm i -g lighthouse`.
- **instance kinds**: `instances --json` carries `kind` (`chrome`/`app`) and `managed`
  (did chromectl start it). `stop` refuses an unmanaged instance without `--force`.
- **Streaming commands need `--max N`**: `watch`, `console`, `intercept` otherwise run until Ctrl-C.


## Machine-readable output (`--json`)

**Every command takes `--json`.** Output becomes one JSON value on stdout. So do
failures: `{"ok": false, "error": {"kind": "...", "message": "..."}}`, still with a
non-zero exit — so parsing stdout always yields a value instead of an empty string.

Branch on `error.kind`: `bad-args`, `no-instance`, `no-target`, `not-found`, `exists`, `timeout`, `js-exception`, `no-snapshot`, `no-history`, `no-storage`, `close-failed`, `launch-failed`, `missing-dep`, `tool-failed`, `connection`, `cdp`.

`run --json` returns the whole batch at once — every step runs in its own `--json`
mode and its result is captured:

```json
{"ok": false, "steps": 3, "failed": 1,
 "results": [{"step": 1, "cmd": "open …", "ok": true, "result": {"id": "…", "url": "…"}},
             {"step": 2, "cmd": "wait …", "ok": false,
              "error": {"kind": "timeout", "message": "timeout waiting for selector '#x'"}}]}
```

`watch`, `console` and `intercept` stream until interrupted — **always pass `--max N`**
(seconds) so they terminate, and they return one array at the end under `--json`.

## Sessions: reuse a login without cloning a profile

```bash
chromectl auth save session.json          # cookies (browser-wide) + per-origin web storage
chromectl auth load session.json          # replay into any instance, incl. a fresh --ephemeral one
chromectl storage --json                  # localStorage; --session for sessionStorage
chromectl storage --set token=abc --get token --remove token --clear
chromectl cookies --set 'sid=abc' --delete sid --clear --json
```

`auth save` writes mode-600 and is a live login — treat the file like a password.
Prefer it over `start --copy-profile`, which clones the user's entire real profile.

## Changing traffic, not just reading it

`capture` reads; `intercept` rewrites:

```bash
chromectl intercept --block '*doubleclick*' --block '*analytics*' --max 10
chromectl intercept --stub '*/api/user=fixtures/user.json' --status 200 --max 10
chromectl intercept --header 'X-Test: 1' --max 10
```

Downloads need arming — headless Chrome discards them otherwise:

```bash
chromectl download --url https://site/report.pdf --dir ./out --wait 30 --json
```

## Replaying requests (for authorized testing)

`replay` re-sends a request — captured here or imported from Burp/curl/HAR —
optionally tampered, so you can probe one target you're allowed to test:

```bash
chromectl replay --burp req.txt                     # raw HTTP/1.1 file (Burp "Copy to file"); - reads stdin
chromectl replay --curl req.sh                       # a "Copy as cURL" command
chromectl replay --har cap.har --index 3             # an entry from any HAR (incl. `capture --har`)
chromectl replay --burp req.txt --set-header 'X-Role: admin' --method PUT   # tamper before sending
```

Two engines. **`raw`** (default without `--attach`) sends out-of-band with full
header control and does not follow redirects, so you see the 3xx — true
Burp-Repeater semantics. **`fetch`** (default with `--attach`) runs the request
inside a live tab via `fetch()`, carrying that tab's real session; the browser
strips forbidden headers (`Cookie`, `Host`, `Origin`, …) and can't read a
redirect, both of which `replay` reports rather than hiding.

Swap identities to test access control (raw engine). `--as` sends with cookies
from an `auth save` file; `--vs` sends a second time as another identity and
diffs the two responses — an identical body across identities is a
broken-access-control (IDOR/BOLA) signal:

```bash
chromectl replay --burp req.txt --as alice.json                 # send as one user
chromectl replay --burp req.txt --as alice.json --vs bob.json   # diff two users
chromectl replay --har cap.har --diff                           # diff replay vs the stored response
```

## Teaching another agent this CLI

```bash
chromectl skill install          # → ~/.claude/skills/chromectl/SKILL.md
chromectl skill install --dir .  # → ./chromectl/SKILL.md, for any agent harness
chromectl skill print            # stdout
```

The skill's command table is generated from the parser at install time, so it can
never drift from the real surface.

## Daemon (warm connections for many calls)

```bash
chromectl daemon start                 # resident process holding warm CDP connections
chromectl daemon status --json         # up? how many pooled connections, uptime
chromectl --daemon eval t "1+1"        # route ONE command through it (or CHROMECTL_DAEMON=1)
chromectl daemon stop
```

The daemon runs the same command dispatch as the CLI over one long-lived
process, keeping the connection pool warm between requests. **It is not a
speed-up for one-shot shell calls** — a `chromectl …` invocation still boots
Python (~100ms) before it can even talk to the daemon, so `--daemon` there is
marginally *slower*. The win is for a **resident client** that connects once and
issues many commands: it pays the Python boot exactly once, and each command is
then ~1ms vs ~100ms cold (~80× faster). Drive it from Python with the persistent
stream:

```python
from chromectl.daemon import Client
with Client() as c:                       # one connection, many commands
    c.call(["open", "https://target"])
    c.call(["replay", "--burp", "req.txt", "--as", "a.json", "--json"])
```

Streaming/interactive and process-management commands (`watch`, `console`,
`intercept`, `capture`, `dialog`, `repl`, `run`, `start`, `stop`, `download`,
`heapsnapshot`) always run locally, never through the daemon. The socket is a
user-only Unix socket at `~/.chromectl/daemon.sock` speaking
one-JSON-object-per-line — the same seam an agent holds open to drive Chrome as
a live stream.

### Capture buffer + `replay --last`

The daemon can hold an always-on tap on a target's traffic — a bounded ring
buffer — so you can replay recent requests without having armed a capture first
(these commands need `--daemon` and only work inside the daemon):

```bash
chromectl --daemon buffer start example        # tap the 'example' tab's network
chromectl --daemon buffer list                 # recent requests, numbered
chromectl --daemon buffer subscribe            # LIVE stream of requests as they happen
chromectl --daemon buffer subscribe --backlog 20 --max 30   # last 20 then live, for 30s
chromectl --daemon replay --last               # replay the most recent request
chromectl --daemon replay --last 3 --as b.json # 3rd-most-recent, as another identity
chromectl --daemon buffer stop
```

Records hold the full request (method/url/headers/body) — enough to replay and
tamper — but not response bodies, so memory stays bounded by the ring size.
`buffer subscribe` prints each request live (as a parseable line); with `--json`
it collects for `--max` seconds and returns one array. For a truly real-time
agent, hold the stream open in Python and react per request:

```python
from chromectl.daemon import subscribe, Client
with Client() as c:
    for ev in subscribe():                      # yields events as they happen
        tx = ev.get("tx")
        if tx and "/api/" in tx["url"]:
            c.call(["replay", "--last", "--as", "b.json", "--json"])
```

### Fast invocation with the `cx` thin client

Every `chromectl` call pays Python's ~100ms interpreter+import startup, even to
reach a warm daemon. `cx` is a tiny prebuilt Go binary that forwards your command
to the running daemon over its socket and prints the reply — near-instant — and
falls back to plain `chromectl` automatically when no daemon is up. Install it,
then use `cx <args>` anywhere you'd type `chromectl <args>`:

```bash
chromectl client install            # download the binary for this OS/arch → ~/.chromectl/bin/cx
chromectl client install --from dist/cx/   # or install from a local build dir / file
chromectl client install --build    # or build from source (needs `go`)
chromectl client status --json      # installed? on PATH?
chromectl client uninstall
cx -i work eval t "1+1"             # a drop-in for `chromectl` — uses the daemon when it's up
```

Add `~/.chromectl/bin` to your PATH (the installer prints the exact line) so a
bare `cx` resolves. Downloads are verified against the release SHA256SUMS.

## All commands

| command | usage | what |
|---|---|---|
| `list (ls)` | `--json` | list open targets (tabs) |
| `start` | `FLAG... --json --port PORT --host HOST --name NAME --auto-port --profile PROFILE --ephemeral --headful --binary BINARY --app PATH --wait SECONDS --copy-profile --from-profile PATH --proxy URL --proxy-auth USER:PASS --proxy-bypass LIST --proxy-pac URL --chrome-arg FLAG` | launch a browser (headless by default) or any Electron app |
| `instances (ps)` | `--json --prune` | list managed Chrome instances and their status |
| `adopt` | `[PORT] --json --name NAME --force` | record an already-running browser/app on a debug port so -i NAME works |
| `stop` | `[which] --json --all --purge --forget --force` | stop a managed instance (by name/port) or --all |
| `version` | `--json` | browser + protocol version |
| `cheat (commands)` | `--json` | print the entire command surface in one call (agent-friendly) |
| `open` | `<url> --json` | open a new tab at URL |
| `close` | `<target> --json` | close a tab |
| `goto (nav)` | `[target] <url> --json --timeout TIMEOUT` | navigate a tab to URL |
| `eval (js)` | `[target] js... --json` | run JavaScript in a tab |
| `html` | `[target] --json --out OUT --max MAX` | dump a tab's HTML |
| `text` | `[target] --json` | dump a tab's visible text |
| `cookies` | `[target] --json --set NAME=VALUE --delete NAME --clear --url URL --domain DOMAIN` | list, set, delete or clear cookies |
| `screenshot (shot)` | `[target] --json --out OUT --full` | capture a screenshot |
| `pdf` | `[target] --json --out OUT` | print a tab to PDF |
| `read` | `[target] --json --out OUT --max MAX` | extract main content as clean Markdown |
| `extract` | `[target] --json --field name=sel` | scrape fields to JSON: --field name=selector[@attr][] |
| `links` | `[target] --json --internal --external` | list page links (text + href) |
| `wait` | `[target] --json --selector SELECTOR --text TEXT --url URL --network-idle --gone --timeout TIMEOUT` | wait for a selector/text/url/network-idle |
| `fill-form` | `<target> --json --set sel=value --submit SELECTOR --enter --timeout TIMEOUT` | fill multiple fields, optionally submit |
| `perf` | `[url] --attach TARGET --reload --wait WAIT --out OUT` | measure Core Web Vitals (+ optional trace) |
| `lighthouse (lh)` | `<url> --categories CATEGORIES --preset {desktop,mobile} --out OUT` | run a Lighthouse audit (needs `npm i -g lighthouse`) |
| `snapshot (snap)` | `[target] --json --out OUT` | list interactive elements (Playwright); saves refs |
| `click` | `[target] --json --ref REF --selector SELECTOR --text TEXT --role ROLE --name NAME --timeout TIMEOUT` | click an element (Playwright auto-wait) |
| `fill` | `<target> value... --json --ref REF --selector SELECTOR --text TEXT --role ROLE --name NAME --timeout TIMEOUT --enter` | fill an input/textarea (Playwright) |
| `hover` | `[target] --json --ref REF --selector SELECTOR --text TEXT --role ROLE --name NAME --timeout TIMEOUT` | hover an element (Playwright) |
| `emulate` | `[target] --json --width WIDTH --height HEIGHT --scale SCALE --mobile --geo LAT,LON --throttle {offline,slow-3g,fast-3g,4g,none} --color {light,dark} --ua UA --shot PATH --hold --clear` | device/geo/network/color-scheme/UA emulation |
| `resize` | `[target] <width> <height> --json --scale SCALE --mobile --shot PATH --hold` | set viewport size (device metrics) |
| `press` | `<target> keys... --json --selector SELECTOR` | press key(s): Enter, Tab, ArrowDown, a, … |
| `type` | `<target> text... --json --selector SELECTOR --enter` | type text into the focused (or --selector) element |
| `upload` | `<target> files... --json --selector SELECTOR` | set files on a file <input> |
| `dialog` | `[target] --accept --dismiss --text TEXT --max MAX` | auto-accept/dismiss JS dialogs (holds session) |
| `heapsnapshot (heap)` | `[target] --json --out OUT` | capture a V8 heap snapshot |
| `run` | `[file] --json --step CMD --target TARGET --keep-going` | run a sequence of steps in one process (script/batch) |
| `back` | `[target] --json` | go back in history |
| `forward` | `[target] --json` | go forward in history |
| `reload` | `[target] --json --hard --timeout TIMEOUT` | reload a tab |
| `storage` | `[target] --json --session --get KEY --set KEY=VALUE --remove KEY --clear` | read/write localStorage or sessionStorage |
| `auth` | `<{save,load}> <file> --json --origin URL --keep-tabs` | save/load a login (cookies + per-origin web storage) |
| `a11y (ax)` | `[target] --json --all --max MAX` | dump the accessibility tree (roles + names) |
| `intercept` | `[target] --json --block PATTERN --stub PATTERN=FILE --header 'Name: value' --status STATUS --content-type CONTENT_TYPE --max MAX` | block, stub or rewrite requests (holds session) |
| `download` | `[target] --json --dir DIR --url URL --wait WAIT --all` | arm downloads to a directory and wait |
| `skill` | `[{print,install}] --json --dir DIR --force` | print or install the agent skill for this CLI |
| `raw (cmd)` | `[target] <method> [params]` | send a raw CDP command |
| `repl` | `[target]` | interactive CDP prompt for a target |
| `proto` | `[query]` | look up protocol domains/commands/events |
| `watch` | `[target] --json --max MAX` | live-tail network requests of a tab |
| `console (logs)` | `[target] --json --max MAX` | tail console messages + JS errors |
| `seo` | `[target] --json` | on-page SEO audit of a tab or URL |
| `capture` | `[url] --json --attach TARGET --reload --type TYPE --print PRINT --out OUT --har HAR --no-bodies --bodycap BODYCAP --max MAX --quiet QUIET` | Burp-style full request/response capture |
| `replay` | `[target] --json --burp FILE --curl FILE --har FILE --last N --index INDEX --scheme {http,https} --method METHOD --url URL --set-header 'Name: value' --remove-header NAME --body BODY --body-file FILE --as SESSION.json --vs SESSION.json --diff --engine {auto,raw,fetch} --attach TARGET --timeout TIMEOUT --out FILE --bodycap BODYCAP` | re-send a captured/imported request (Burp/curl/HAR), tampered, through the live session or out-of-band |
| `daemon` | `[{start,stop,status}] --json --foreground` | run a resident process holding warm CDP connections |
| `buffer` | `[{start,stop,list,subscribe}] [target] --json --maxlen MAXLEN --backlog BACKLOG --max MAX` | daemon-resident tap on live network traffic: start/stop/list, subscribe to a live stream, feed replay --last (needs --daemon) |
| `client` | `[{install,status,uninstall}] --json --from PATH --build` | install the prebuilt `cx` thin client (fast daemon drop-in) |
