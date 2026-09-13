# chromectl — a friendly CLI for the Chrome DevTools Protocol

Drive a Chrome/Chromium instance from the shell over its remote debugging port.
Everything is the Chrome DevTools Protocol (CDP) under the hood; this wraps it in
ergonomic subcommands with nice output. Not just network — JS execution, console
logs/errors, screenshots, PDFs, cookies, SEO audits, page-reading, form flows,
Core Web Vitals / Lighthouse, and a raw escape hatch.

## Install

```bash
pipx install git+https://github.com/USER/chromectl.git   # install straight from the repo
# or, from a local clone:  pipx install .
pipx upgrade chromectl                                    # pull later changes
npm i -g lighthouse                                       # optional — only for `lighthouse`
```

pipx puts `chromectl` on your PATH in an isolated env and pulls all Python deps
(`websocket-client`, `rich`, `playwright`, `readability-lxml`, `markdownify`).
**No `playwright install` needed** — the interaction commands *attach* to your Chrome
over CDP rather than launching their own browser. Playwright and the read/markdown
libs are imported lazily, so the core commands stay fast.

## 1. Launch Chrome

```bash
chromectl start                                   # headless, port 9222, throwaway profile
chromectl start --headful --port 9223             # visible window, custom port
chromectl start --profile ~/.cache/chromectl      # custom profile dir
chromectl start --copy-profile                    # copy your REAL Chrome profile (logins!) then launch
chromectl start --from-profile /path/to/profile   # copy from a specific profile dir
```

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
- A **non-default profile is mandatory** since Chrome 136 — the real default profile
  refuses the debug port (anti-cookie-theft). `start` uses a throwaway profile; to reuse
  your logins, copy your real one first: `cp -r ~/.config/google-chrome /tmp/prof` then
  `chromectl start --profile /tmp/prof`.
- `start` sets `--remote-allow-origins=*` so clients (incl. Playwright) can connect.
- Anyone who can reach the port has **full, unauthenticated control** of that browser
  (read cookies/sessions, run JS, read traffic). Keep it on localhost; never forward it.

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

# Most read-only commands accept --json for scripting/agents:
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
be stable before acting) instead of hand-rolled timing. `snapshot` writes `.cdp-snap.json`
so `--ref N` works in a later, separate command (the stateless equivalent of the MCP's
element uids). Locate an element by any of: `--ref`, `--selector`, `--text`, or
`--role`+`--name`.

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

## Layout
- `chromectl/cli.py` — the CLI.
- `chromectl/protocol.json` — bundled CDP schema used by `chromectl proto`/`cheat` (falls back to live).
- `pyproject.toml` — packaging; `chromectl` console entry point.
- `AGENTS.md` — the agent-facing interface reference (auto-read by coding agents).

## For agents
Read `AGENTS.md` (or run `chromectl cheat --json` once) for the full command surface —
no need to call `--help` per command.
