# preship

**A pre-launch reliability check for FastAPI.** Point it at a running app — it pokes
every documented endpoint with unexpected inputs and tells you what breaks: crashes,
undocumented status codes, and responses that don't match their declared schema.
Then it hands you a copy-paste prompt to fix each one with any AI.

> **preship is not a security scanner.** It finds reliability bugs (the 500s and contract
> breaks your users would hit), not vulnerabilities. See [What it does NOT do](#what-it-does-not-do).
> Saying this up front is the point: it does its one job well and is honest about the rest.

No API key needed to find issues and get fix prompts. (An optional key unlocks AI-written patches.)

---

## What it does

- **Finds unhandled 500 crashes** — the inputs that make a handler throw instead of
  returning a clean error. Missing validation, empty-list math, nested-body bugs,
  async handlers, null dereferences. (Verified across unrelated real-world apps.)
- **Flags undocumented status codes** — if an endpoint returns a code that isn't in its
  OpenAPI spec, you'll know. Documented codes are correctly left alone.
- **Catches response-schema mismatches** — when the body doesn't match the
  `response_model` you declared (missing field, wrong type, null where it shouldn't be).
- **Tells intended 5xx apart from real crashes** — a `503` you documented for maintenance
  is shown as low-severity, not flagged as a crash.
- **Gives you a fix** — one copy-paste prompt per problem, for ChatGPT, Claude, Gemini, anything.

## What it does NOT do

Being clear about the edges is how you can trust the rest:

- **No security testing.** No auth-bypass, injection, IDOR, or data-leak detection. Even
  an extra undeclared field leaking in a response isn't flagged — that's a security tool's job.
- **Misses crashes that only fire on one magic value** (e.g. a bug that only triggers when
  `code == 1337`). Black-box fuzzing can't guess arbitrary constants.
- **Doesn't reach endpoints behind a login.** Anything behind `401/403` is out of scope
  (passing a token with `--header` may help, but isn't guaranteed).
- **Needs an app that actually starts** and serves `/openapi.json`. If your app won't boot
  without a database, secrets, or model files, preship can't scan it.
- **Assumes a clean staging instance.** If your endpoints mutate stored state, scan against
  a disposable instance so results stay consistent.

---

## Install

You need **Python 3.10 or newer**. Check what you have:

```bash
python --version
```

If that prints `Python 3.10` or higher, install preship:

```bash
pip install preship
```

Then confirm it's ready:

```bash
preship --help
```

If you see the help text, you're done — skip to [Quick start](#quick-start-about-60-seconds).

### If something went wrong

Installation is where most people get stuck, so here are the usual fixes:

- **`python: command not found`** — try `python3` and `pip3` instead of `python` and `pip`.
- **`pip: command not found`** — run `python -m pip install preship` instead.
- **Your Python is older than 3.10** — install a newer Python from
  [python.org/downloads](https://www.python.org/downloads/), then try again.
- **`preship: command not found` after install succeeded** — your install location isn't on
  your PATH. Run it via Python directly: `python -m ohs_preflight.cli --help`.
- **Permission errors** — don't use `sudo`. Install just for you: `pip install --user preship`,
  or better, use a virtual environment (below).

### The clean way (recommended): a virtual environment

A virtual environment keeps preship and its dependencies separate from the rest of your
system, which avoids most of the problems above:

```bash
python -m venv .venv
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install preship
preship --help
```

### Still stuck? Let an AI walk you through it

If none of that worked, copy the prompt below, fill in the two blanks, and paste it into
any AI chat (ChatGPT, Claude, Gemini). It will give you step-by-step commands for your exact
setup — no need to understand any of this yourself:

```
I'm trying to install a Python command-line tool called "preship" (PyPI package name:
preship) and I'm stuck. Please give me exact, copy-paste terminal commands to fix it,
explained simply for a beginner.

My operating system is: __________  (e.g. Windows 11 / macOS / Ubuntu Linux)
When I run `pip install preship` I get this error / this happens:
__________  (paste the full error message, or describe what you see)

Assume I'm new to Python. Walk me through it one command at a time, and tell me how to
check whether each step worked.
```

Whatever the AI tells you, the commands stay on your own machine — you're just installing a
package, nothing is sent anywhere.

---

## Quick start (about 60 seconds)

preship only scans apps **you own** — so it can't be aimed at someone else's server. You
prove ownership once by adding a tiny route to your app. Here's the whole flow:

**1. Run a scan.** The first time, it stops and prints your token + the exact code to add:

```bash
preship scan https://your-staging.example.com
```

**2. Add the verification route it shows you.** Paste the token it printed:

```python
# anywhere below  app = FastAPI()
from fastapi.responses import PlainTextResponse

@app.get("/.well-known/preflight-verify")
def preflight_verify():
    return PlainTextResponse("the-token-preship-printed")
```

Redeploy your staging app so the route is live.

**3. Run the scan again.** This time it scans and reports:

```bash
preship scan https://your-staging.example.com
```

That's it. The token is fixed per URL, so you only add the route once.

> Behind auth? Pass headers through to every request:
> ```bash
> preship scan https://your-staging.example.com --header "Authorization: Bearer <token>"
> ```

---

## What the output looks like

```
3 defect(s) / 2 pattern(s) / 2 endpoint(s) (severity: high first):

[HIGH] not_a_server_error — 1 endpoint(s)
  Affected endpoints:
    - GET /items/{item_id}
  Example: GET /items/-1300  →  500 Internal Server Error

[LOW] status_code_conformance — 2 endpoint(s)
  Affected endpoints:
    - GET /items/{item_id}
    - POST /orders
```

Each block is one **pattern** (a kind of problem), listing every endpoint it affects.

---

## Understanding the results

| check | severity | what it means |
|-------|----------|---------------|
| `not_a_server_error` | **HIGH** | An undocumented 500 — a real, unhandled crash on some input. Fix these first. |
| `documented_5xx_response` | LOW | A 5xx you *did* document (e.g. maintenance). Shown for review; likely intended. |
| `status_code_conformance` | LOW | Returned a status code that isn't in your OpenAPI spec. Document it or change it. |
| `response_schema_conformance` | LOW | The response body doesn't match the `response_model` you declared. |

**HIGH** = something crashes; fix before launch. **LOW** = a contract gap; worth tidying.

---

## Fixing what it finds (no experience needed)

You don't have to figure out the fix yourself. After the report, preship prints a
**ready-made prompt** for each problem. You:

1. Copy the whole prompt block for a problem.
2. Paste it into any AI chat — ChatGPT, Claude, Gemini, whatever you use.
3. It explains the cause and gives you the corrected FastAPI code.

The prompt already contains the failing request, the response, and what to do — so the AI
has everything it needs. No security or testing knowledge required on your part.

*(If you set an `ANTHROPIC_API_KEY`, preship can also generate the patch for you directly.
Optional — the copy-paste prompts work with no key at all.)*

---

## Using it in CI (for the gate-keepers)

preship's exit codes let you block a release when there are defects:

| exit code | meaning | typical CI action |
|-----------|---------|-------------------|
| `0` | scan ran, no defects | pass |
| `1` | scan ran, defects found | **fail the build** (this is the gate) |
| `2` | scan couldn't run (target unreachable, schemathesis missing) | surface as an error, distinct from defects |
| `3` | ownership unverified — **no external request was sent** | fix verification, then retry |

The ownership check (exit `3`) runs *first*: if the URL isn't proven yours, preship sends
no scan, no `/openapi.json` fetch, no AI call. Safe by default.

```bash
# fail the pipeline if preship finds anything
preship scan "$STAGING_URL" || exit 1
```

---

## How it works (under the hood)

preship fetches your `/openapi.json`, then runs property-based fuzzing
([schemathesis](https://schemathesis.readthedocs.io/)) against every documented endpoint
with `--mode positive` (valid-shaped inputs that probe edge cases). It reads the results,
classifies each failure, and formats the report. It judges behavior over HTTP only — it
never reads your source code.

---

## License

preship is released under the **Business Source License 1.1 (BUSL-1.1)**.

In plain terms: you can **use, copy, and modify** it freely for your own development and
internal purposes, including commercial use. The one restriction is that you can't offer
preship itself as a competing hosted/commercial product. On the **Change Date**, it converts
to **Apache 2.0** (fully open source).

See [LICENSE](LICENSE) for the exact terms — that text governs, not this summary.
