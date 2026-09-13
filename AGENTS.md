# chromectl — agent reference

A single CLI that drives Chrome over the DevTools Protocol. **This file is the complete interface — you do not need to run `--help` per command.** Re-fetch the surface as data anytime: `chromectl cheat --json`.

## Launch & manage Chrome instances

```bash
chromectl start                       # instance on 9222; profile persists at ~/.chromectl/profiles/chrome-9222
chromectl start --name work --auto-port   # a second instance on a free port, named 'work'
chromectl start --name work           # start 'work' again later -> SAME profile, logins persist
chromectl start --copy-profile        # launch with a COPY of your real profile (logins)
chromectl start --ephemeral           # throwaway /tmp profile (no persistence)
chromectl instances                   # list managed instances + up/down (--json, --prune)
chromectl stop work                   # stop by name/port; stop --all; stop work --purge (delete profile)
```

Profiles **persist by default** per name — cookies/logins survive restarts. Target an instance with **`-i NAME`** (or `--port N`) before the subcommand:

```bash
chromectl -i work open https://site && chromectl -i work read --json
```

## Prefer `run` for multi-step tasks

A sequence in one `run` executes in a single process over one persistent connection; state persists across steps and the tab you `open` becomes the implicit target for later steps. **Note:** step lines are shlex-split, so escape inner quotes in JS (e.g. `eval "localStorage.getItem('k')"` as a standalone command is easier than inside a step).

```bash
chromectl -i work run --step 'open https://site/login' \
  --step 'wait --selector #user' \
  --step 'fill-form --set #user=ada --set #pass=secret --submit #go' \
  --step 'wait --url /dashboard' --step 'read --json'
```

## Conventions (apply everywhere)

- **instance**: `-i NAME` / `--port N` selects which browser. `chromectl instances` lists them. Profiles persist per name.
- **target** (a tab): id-prefix, url/title substring, `browser`, or empty = first page. In `run`, omit to use the current tab. Prefer `list --json` + id-prefix.
- **`--json`** on read-only commands (`list`,`read`,`extract`,`links`,`cookies`,`seo`,`wait`,`fill-form`,`instances`,`cheat`) → plain pipeable JSON.
- Put **options after positionals**. Locate elements (click/fill/hover/wait) by `--selector`, `--text`, `--role`+`--name`, or `--ref N` (from last `snapshot`).
- **Emulation** (`emulate`/`resize`) reverts on exit — use `--shot`, `--hold`, or an `emulate` step in a `run`.
- Non-zero exit on failure. `lighthouse` needs `npm i -g lighthouse`.


## All commands

| command | usage | what |
|---|---|---|
| `list (ls)` | `--json` | list open targets (tabs) |
| `start` | `--name NAME --auto-port --profile PROFILE --ephemeral --headful --binary BINARY --copy-profile --from-profile PATH` | launch a Chrome instance (headless by default) |
| `instances (ps)` | `--json --prune` | list managed Chrome instances and their status |
| `stop` | `[which] --all --purge` | stop a managed instance (by name/port) or --all |
| `version` | `` | browser + protocol version |
| `cheat (commands)` | `--json` | print the entire command surface in one call (agent-friendly) |
| `open` | `<url>` | open a new tab at URL |
| `close` | `<target>` | close a tab |
| `goto (nav)` | `[target] <url> --timeout TIMEOUT` | navigate a tab to URL |
| `eval (js)` | `[target] js...` | run JavaScript in a tab |
| `html` | `[target] --out OUT --max MAX` | dump a tab's HTML |
| `text` | `[target]` | dump a tab's visible text |
| `cookies` | `[target] --json` | list a tab's cookies |
| `screenshot (shot)` | `[target] --out OUT --full` | capture a screenshot |
| `pdf` | `[target] --out OUT` | print a tab to PDF |
| `read` | `[target] --json --out OUT --max MAX` | extract main content as clean Markdown |
| `extract` | `[target] --json --field name=sel` | scrape fields to JSON: --field name=selector[@attr][] |
| `links` | `[target] --json --internal --external` | list page links (text + href) |
| `wait` | `[target] --json --selector SELECTOR --text TEXT --url URL --network-idle --gone --timeout TIMEOUT` | wait for a selector/text/url/network-idle |
| `fill-form` | `<target> --json --set sel=value --submit SELECTOR --enter --timeout TIMEOUT` | fill multiple fields, optionally submit |
| `perf` | `[url] --attach TARGET --reload --wait WAIT --out OUT` | measure Core Web Vitals (+ optional trace) |
| `lighthouse (lh)` | `<url> --categories CATEGORIES --preset {desktop,mobile} --out OUT` | run a Lighthouse audit (needs `npm i -g lighthouse`) |
| `snapshot (snap)` | `[target] --out OUT` | list interactive elements (Playwright); saves refs |
| `click` | `[target] --ref REF --selector SELECTOR --text TEXT --role ROLE --name NAME --timeout TIMEOUT` | click an element (Playwright auto-wait) |
| `fill` | `<target> value... --ref REF --selector SELECTOR --text TEXT --role ROLE --name NAME --timeout TIMEOUT --enter` | fill an input/textarea (Playwright) |
| `hover` | `[target] --ref REF --selector SELECTOR --text TEXT --role ROLE --name NAME --timeout TIMEOUT` | hover an element (Playwright) |
| `emulate` | `[target] --width WIDTH --height HEIGHT --scale SCALE --mobile --geo LAT,LON --throttle {offline,slow-3g,fast-3g,4g,none} --color {light,dark} --ua UA --shot PATH --hold --clear` | device/geo/network/color-scheme/UA emulation |
| `resize` | `[target] <width> <height> --scale SCALE --mobile --shot PATH --hold` | set viewport size (device metrics) |
| `press` | `<target> keys... --selector SELECTOR` | press key(s): Enter, Tab, ArrowDown, a, … |
| `type` | `<target> text... --selector SELECTOR --enter` | type text into the focused (or --selector) element |
| `upload` | `<target> files... --selector SELECTOR` | set files on a file <input> |
| `dialog` | `[target] --accept --dismiss --text TEXT --max MAX` | auto-accept/dismiss JS dialogs (holds session) |
| `heapsnapshot (heap)` | `[target] --out OUT` | capture a V8 heap snapshot |
| `run` | `[file] --step CMD --target TARGET --keep-going` | run a sequence of steps in one process (script/batch) |
| `raw (cmd)` | `[target] <method> [params]` | send a raw CDP command |
| `repl` | `[target]` | interactive CDP prompt for a target |
| `proto` | `[query]` | look up protocol domains/commands/events |
| `watch` | `[target]` | live-tail network requests of a tab |
| `console (logs)` | `[target] --max MAX` | tail console messages + JS errors |
| `seo` | `[target] --json` | on-page SEO audit of a tab or URL |
| `capture` | `[url] --attach TARGET --reload --type TYPE --print PRINT --out OUT --har HAR --no-bodies --bodycap BODYCAP --max MAX --quiet QUIET` | Burp-style full request/response capture |
