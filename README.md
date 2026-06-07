# Preship

**Hammer your FastAPI staging app before launch — find the inputs that crash it, and get a fix for each.**

Give it one staging URL. Preship fuzzes every endpoint and tells you:

- **What breaks** — unhandled 500s (the inputs that crash it), undocumented responses, and schema mismatches, grouped by pattern.
- **How to fix it** — one copy-paste **fix prompt** per pattern, ready to drop into whichever AI you use (ChatGPT, Claude, Gemini, anything).

No API key required. Finding the issues and getting the fix prompts works entirely without one.

---

## 1. Install

Requires Python 3.10+.

```bash
pip install preship
```

This adds the `preship` command:

```bash
preship --help
```

## 2. Point it at a staging URL + add an ownership-check route

Preship only scans URLs **you own**, so it can't be pointed at someone else's server. Before the
first scan you prove the URL is yours — once — by adding a single verification route to your app.

Run a scan once and Preship prints **your token** and **the exact route code to paste**. Drop the
token into the snippet below, add it to your app, and redeploy.

```python
# Add this one route to your FastAPI app (anywhere below app = FastAPI())
from fastapi.responses import PlainTextResponse

@app.get("/.well-known/preflight-verify")
def preflight_verify():
    return PlainTextResponse("paste the token that scan printed here")
```

> The first scan tells you the exact token and prints the route code too — no guessing, just copy
> what it shows. The token is fixed per URL, so you only add this once.

## 3. Run the scan

```bash
preship scan https://your-staging.example.com
```

If your staging is behind auth, pass headers along:

```bash
preship scan https://your-staging.example.com --header "Authorization: Bearer <token>"
```

## 4. Reading the results

The output has two parts.

**(1) Findings report** — same-kind problems are grouped into patterns and tagged by severity:
`[HIGH]` (the server crashes with a 500) or `[LOW]` (response-contract mismatch). Each pattern
lists the affected endpoints and the actual request → response Preship observed.

**(2) AI fix prompts** — one prompt block per pattern. **Copy a block whole and paste it into
whichever AI you use**, and you get a likely cause, a FastAPI fix, and an explanation. The prompts
contain only what the scanner observed (never your source code) and aren't tied to any specific AI.

> **Beta:** the AI fix prompts are free during beta. Features and policy may change at general availability.

> Apply a fix and run `preship scan` again to confirm the pattern is gone.

---

## Advanced (optional): auto-generated patch drafts

Want auto-generated patch drafts too? Put **your own** Anthropic API key in **your own**
environment and scan. Without it, the fix prompts above still work — the key is entirely optional.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # your key. The default beta path needs no key — the prompts alone are enough.
preship scan https://your-staging.example.com
```

---

## Try it on the included demo

This repo ships a deliberately buggy FastAPI app at `fixtures/demo_app_buggy.py` — seven endpoints,
six with planted defects and one clean control. Run it locally and point Preship at it to see a real
findings report without touching your own API:

```bash
pip install fastapi "uvicorn[standard]"
uvicorn fixtures.demo_app_buggy:app --port 8000
# in another terminal:
preship scan http://127.0.0.1:8000
```

---

## License

Preship is released under the **Business Source License 1.1 (BUSL-1.1)**.

- You may use Preship freely — including in **commercial, staging, and live environments** — to test,
  diagnose, or improve the reliability of your own APIs.
- The only restriction: you may **not** repackage Preship as a competing hosted or managed
  reliability/fuzz-testing service.
- On **2030-06-07**, this version automatically converts to the **Apache License 2.0**.

See the [LICENSE](./LICENSE) file for the full text.
