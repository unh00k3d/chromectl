---
name: chromectl
description: Drive a real Chrome browser from the shell over the DevTools Protocol — open pages, read them as Markdown, click and fill forms, capture network traffic, block or stub requests, audit performance/SEO/accessibility, and save logins. Use when a task needs a real browser: scraping a JS-rendered page, reproducing a UI bug, checking what a site actually sends, testing a login flow, or measuring Core Web Vitals. Requires the `chromectl` CLI on PATH.
---

# chromectl

One CLI over the Chrome DevTools Protocol. Everything below is the whole
interface — you never need `--help` per command.

## The three rules

1. **Add `--json` to every command.** Output becomes one JSON value on stdout.
   Failures become `{"ok": false, "error": {"kind": "...", "message": "..."}}` on
   stdout too, with a non-zero exit — so parsing stdout always yields a value.
   Branch on `error.kind`: <!-- ERROR-KINDS -->.
2. **Batch with `run`.** A sequence in one `run` shares one process and one
   connection, and the tab you `open` becomes the implicit target for later
   steps. Six steps in one `run --json` is one command, not six round trips.
3. **Start a browser first.** `chromectl start` (headless, port 9222, profile
   persists). Target another instance with `-i NAME` *before* the subcommand.
   For an Electron app: `chromectl start --app /path/to/app`, or
   `chromectl adopt PORT` to take over one that is already listening.

```bash
chromectl start --name work
chromectl -i work run --json \
  --step 'open https://site/login' \
  --step 'wait --selector #user' \
  --step 'fill-form --set #user=ada --set #pass=secret --submit #go' \
  --step 'wait --url /dashboard' \
  --step 'read'
```

`run --json` returns `{"ok", "steps", "failed", "results": [{step, cmd, ok, result|error}]}`.
Add `--keep-going` to continue past a failing step.

## Reading a page

Prefer these over `html` — they cost a fraction of the tokens:

- `read --json` → `{url, title, markdown, chars}`, article text only.
- `extract --json --field "title=h1" --field "prices=.price[]" --field "img=img@src"` →
  scrape to a JSON object. `[]` = all matches, `@attr` = an attribute.
- `links --json --external` → every link as `{text, href}`.
- `snapshot --json` → interactive elements with stable `ref` numbers, then act with
  `click --ref 3`. This is the cheap way to "see" a page; screenshots rarely are.
- `a11y --json` → the accessibility tree (roles + names), as a screen reader sees it.

## Acting on a page

Locate an element by **`--ref N`** (from `snapshot`), `--selector CSS`,
`--text STR`, or `--role ROLE --name STR`. `click`/`fill`/`hover` go through
Playwright attached over CDP, so they auto-wait for the element to be visible and
actionable — no manual sleeps.

Make waiting explicit rather than sleeping: `wait --selector X`, `--text X`,
`--url X`, `--gone`, or `--network-idle`.

## Sessions and state

```bash
chromectl auth save session.json          # cookies + per-origin web storage
chromectl auth load session.json          # replay into a fresh instance
chromectl storage --json                  # localStorage (--session for sessionStorage)
chromectl storage --set token=abc --json
chromectl cookies --json                  # --set n=v, --delete NAME, --clear
```

`auth save` is the right way to reuse a login. Prefer it over `start --copy-profile`,
which clones the user's entire real Chrome profile.

## Network

- `capture URL --json` — full request/response transactions, Burp-style. `--har out.har`
  to open in DevTools. Filter with `--type xhr`.
- `watch --json --max 5` — a bounded one-line-per-request tail. **Always pass `--max`**
  or it runs until interrupted.
- `intercept --block '*doubleclick*' --max 10` — block requests. Also
  `--stub 'api/user=fake.json'` to serve a fixed body, and `--header 'X-Test: 1'`.
- `console --json --max 5` — console messages and uncaught errors.

## Audits

`perf URL` (Core Web Vitals), `lighthouse URL --preset desktop` (needs
`npm i -g lighthouse`), `seo --json`, `a11y --json`.

## Electron apps (Slack, Discord, VS Code, Obsidian, …)

An Electron app is Chromium, so everything above works against it once it is
listening — `read`, `eval`, `capture --attach`, `console`, `intercept`, `snapshot`,
`click`, `a11y`, `storage`, `cookies`.

```bash
chromectl start --app slack --name slack   # or: slack --remote-debugging-port=9222
chromectl adopt 9222 --name slack          #      then adopt it
chromectl -i slack list                    # windows are 'page' or 'webview' targets
chromectl -i slack read --json
```

Three differences from a browser:

- **No tab model.** `open` and `close` fail with kind `cdp`, carrying the app's own
  words ("Not supported"). There is nothing to open a tab *in*. Use `list` to find a
  window and `goto` to navigate one; use `--attach TARGET` for `perf` and `capture`,
  and pass `seo` a target instead of a URL.
- **It must not already be running.** A single-instance lock hands our flags to the
  first process and drops them. Quit the app fully, then start it.
- **`auth load` can partially apply** — cookies land, but an origin with no window
  open on it cannot get web storage. Check `skipped` in the payload; `ok` is false
  when it is non-empty.

`stop` refuses to kill an instance you adopted rather than started — it is somebody's
real, signed-in application. Use `--forget` to drop the record, `--force` to insist.

## Gotchas

- **Emulation reverts** when the connection closes. `emulate`/`resize` overrides only
  persist within one process — use `--shot` to capture in the same session, `--hold`
  to keep it open, or put the `emulate` in a `run`.
- **`run` steps are shlex-split.** Escaping quotes inside JS in a step is painful;
  run `eval` as its own command instead.
- **Put options after positionals**: `chromectl read example --json`, not `--json example`.
- **Downloads need arming**: `chromectl download --url URL --dir ./out` — headless
  Chrome otherwise discards them.
- Anyone who can reach the debug port has full control of that browser. Keep it on
  localhost.

## Commands

<!-- COMMANDS -->
