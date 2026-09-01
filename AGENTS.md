# AGENTS.md

Working notes for agents and future sessions. Records how this project is
actually built, not how it might ideally be built.

## What this is

A local bridge that serves the **OpenAI Chat Completions API** and the
**Anthropic Messages API** on `127.0.0.1`, and answers them by driving a real,
logged-in `claude.ai` session in a Camoufox browser profile. No official
Anthropic, OpenAI or xAI API key is involved anywhere.

The point is that a coding client (OpenClaude, or the official Claude Code)
keeps its own agent loop and host tools, while the bridge carries the
conversation and the tool schemas to Claude in the browser and streams the
answer back. Tool calls come from Claude's own native `tool_use` stream — the
bridge never guesses a tool call from response text.

## Layout

```
src/claude_web_api/
  app.py            FastAPI app, lifespan, static mount, health, legacy /chat
  runtime.py        process-wide state: session, control, telemetry, registry
  completions.py    one Claude Web turn: tools, retries, rotation, journal
  paths.py          every filesystem anchor, derived from the repo root
  sanitize.py       redaction applied to anything that leaves the process
  api/              one module per wire protocol
    openai.py       /v1/chat/completions, /v1/models
    anthropic.py    /v1/messages, /v1/messages/count_tokens
    control.py      /api/control/* for the local panel
  protocol/         pure translation, no I/O, no routing
    openai.py       OpenAI <-> internal history and native tools
    anthropic.py    Messages blocks <-> internal history
    openai_usage.py provider token counts -> OpenAI usage shape
  providers/        contracts, registry, claude_web, grok_web
  session/claude.py the browser driver (largest file; see below)
  control/config.py control_config.json store and persona compilation
  telemetry/        runtime journal + SQLite store
  enrollment/       adding an account through a visible browser
web/                control panel (plain HTML/CSS/JS, no build step)
tests/              unittest; 229 tests, no browser required
scripts/            PowerShell entry points (Windows is the primary host)
```

## Commands

```powershell
.\scripts\setup.ps1          # portable Python + deps + Camoufox browser
.\scripts\start.ps1          # supervisor + server on 127.0.0.1:8765
.\scripts\open-control.ps1   # open the control panel
```

Checks, from the repository root:

```powershell
.\.python\python.exe -m unittest discover -s tests -t tests
.\.python\python.exe -m ruff check .
```

`unittest discover` needs both `-s tests` and `-t tests`; the suite does not
import as a package. The tests never launch a browser — everything below the
provider boundary is faked.

## Rules that keep this working

**Reach shared state through its module.** Routes call `runtime.session`,
`runtime.control`, `completions.run_native_with_limits`. Importing those names
by value breaks every test that patches them, and the failure looks like the
patch silently not applying.

**The bridge does not invent model output.** No canned answers, no tool calls
parsed out of text, no fabricated usage numbers. When something cannot be
delivered, it fails visibly: unsupported content blocks return 400, the Grok
provider reports `streaming=false` rather than pretending, and absent token
counts are reported as zero rather than estimated. The one documented
exception is `POST /v1/messages/count_tokens`, which returns a character-length
estimate because claude.ai exposes no tokenizer — it is labelled as an estimate
in the code, the README and the docs.

**Project Instructions are a stable contract.** `resources/project_instructions.txt`
is synced into each account's `OpenClaude IDE` project prompt and verified
before a turn is sent. Per-request context (working directory, model, persona)
is request-scoped and never written into the project prompt. If you change that
file, understand `_sync_trusted_project` in `session/claude.py` first — a
mismatch stops sending rather than overwriting someone's edit.

**Redact before anything leaves.** Errors, journal entries and panel responses
go through `sanitize.py`. Account UUIDs, cookies, tokens and key material must
not reach a response body or the SQLite journal.

**Endpoint changes need `tests/test_routes.py` updated.** It asserts the served
path set from the OpenAPI document. That test exists because a refactor once
left a decorator behind and silently re-bound `/api/control/state` to `main()`.

## Things that will bite you

- `session/claude.py` is ~4900 lines with a 90-method `ClaudeSession`. It embeds
  browser-side JavaScript in string literals — that is why the file is exempt
  from `E501` in `pyproject.toml`. Reflowing those literals changes the code
  that runs in the page.
- FastAPI's `include_router` mounts a router as a single node, so
  `len(app.routes)` does not grow by the number of endpoints. Read
  `/openapi.json` to see what is actually served.
- Account-scoped files (`control_config.json`, `claude_project.json`,
  `project_prompt_leases.json`) are gitignored and recreated from defaults on
  first run. `*.example.json` files document their shape.
- `mypy` currently reports 7 errors in inherited code (`session/claude.py`,
  `enrollment/manager.py`, `telemetry/store.py`, `control/config.py`,
  `protocol/openai_usage.py`). It is configured in `pyproject.toml` but kept
  out of CI rather than being suppressed with a passing exit code. Fixing those
  is welcome; masking them is not.
- The Grok provider is deliberately fail-closed: xAI blocks automated browsers,
  so it reports no capabilities and refuses activation. Do not "fix" it by
  making it claim support.

## What not to do here

- Do not add an official API-key path. The project exists to avoid one.
- Do not write account state into the repository, and do not remove entries
  from `.gitignore` to make something easier to debug.
- Do not put fallback answers, retries-with-different-wording, or "helpful"
  reconstructions in front of a failed turn. A failure is reported.
- Do not add dependencies casually: the runtime is a portable Python tree that
  users install with one script.
