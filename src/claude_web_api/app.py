"""Local OpenAI-compatible API backed by an authenticated claude.ai session.

OpenAI functions are mapped to claude.ai native tools. Claude selects actions,
OpenClaude executes them, and their results return through Claude's real
``/tool_result`` side-channel while the original SSE remains open.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from claude_web_api import __version__, completions, runtime
from claude_web_api.api import anthropic as anthropic_api
from claude_web_api.api import control as control_api
from claude_web_api.api import openai as openai_api
from claude_web_api.paths import (
    WEB_ROOT,
)
from claude_web_api.sanitize import public_error_message
from claude_web_api.session.claude import (
    ClaudeBrowserUnavailableError,
    ClaudeTurnOutcomeUnknownError,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    await runtime.session.start()
    await runtime.session.start_watchdog()
    telemetry_settings = runtime.control.telemetry_settings()
    try:
        # Recovery is only safe after session.start() has acquired the
        # profile/runtime lease. A second worker must not interrupt the
        # first worker's in-flight rows.
        await runtime.telemetry.store_call_async("recover_interrupted")
        if not bool(telemetry_settings.get("store_content")):
            await runtime.telemetry.store_call_async("scrub_content")
        await runtime.telemetry.store_call_async(
            "prune",
            retention_days=int(
                telemetry_settings.get("retention_days") or 30
            ),
            max_requests=int(
                telemetry_settings.get("max_requests") or 5_000
            ),
        )
    except Exception:
        # Telemetry is auxiliary; its health is exposed in the control
        # panel, while the API remains available.
        pass
    telemetry_task = asyncio.create_task(
        runtime.telemetry_maintenance(),
        name="telemetry-maintenance",
    )
    runtime.persist_runtime_identity()
    runtime.telemetry.log("INFO", "API", "Сервер и Camoufox запущены")
    try:
        yield
    finally:
        telemetry_task.cancel()
        await asyncio.gather(telemetry_task, return_exceptions=True)
        await runtime.enrollment.stop()
        await runtime.session.stop()
        await runtime.telemetry.close_store_executor()


app = FastAPI(
    title="Claude Web API",
    version=__version__,
    lifespan=lifespan,
)
app.include_router(anthropic_api.router)

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Clients of the Messages API expect Anthropic's error envelope.

    FastAPI's default body would reach Claude Code as an unparseable shape, so
    the Messages paths answer in their own protocol; everything else keeps the
    framework default.
    """
    if request.url.path.startswith("/v1/messages"):
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ())[1:])}: "
            f"{error.get('msg', 'invalid value')}"
            for error in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content=anthropic_api.error_payload(
                400, detail or "invalid request body"
            ),
        )
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


app.include_router(control_api.router)
app.include_router(openai_api.router)

if WEB_ROOT.exists():
    app.mount(
        "/control/assets",
        StaticFiles(directory=str(WEB_ROOT)),
        name="control-assets",
    )


class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    new_chat: bool = False
    timeout: float = Field(default=300.0, ge=5.0, le=600.0)


class ChatOut(BaseModel):
    response: str








@app.get("/")
async def root():
    return RedirectResponse("/control/")


@app.get("/control/")
async def control_index():
    index = WEB_ROOT / "index.html"
    if not index.exists():
        raise HTTPException(404, "control panel has not been installed")
    return FileResponse(index)


@app.get("/health")
async def health():
    return runtime.session.health_snapshot()


@app.get("/health/live")
async def health_live():
    """Event-loop liveness only; never waits for Playwright or its global lock."""
    if not runtime.session.watchdog_healthy():
        raise HTTPException(503, "Camoufox watchdog is unhealthy")
    return {"ok": True, "watchdog": True, "time": time.time()}


@app.get("/health/ready")
async def health_ready():
    """Non-blocking Camoufox readiness snapshot."""
    snapshot = runtime.session.health_snapshot()
    if not snapshot["ok"]:
        return JSONResponse(status_code=503, content=snapshot)
    return snapshot


@app.post("/new")
async def new_chat():
    try:
        await runtime.session.new_chat()
        return {"ok": True}
    except ClaudeBrowserUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn):
    request_id = f"legacy-{uuid.uuid4().hex[:12]}"
    completions.begin_request_telemetry(
        request_id,
        "claude-web",
        None,
        body.message,
        streaming=False,
    )
    try:
        text = await runtime.session.chat(
            body.message,
            timeout=body.timeout,
            new_chat=body.new_chat,
        )
        completions.finish_request_telemetry(
            request_id,
            status="completed",
            assistant_text=text,
            resolved_model="claude-web",
        )
        return ChatOut(response=text)
    except ClaudeTurnOutcomeUnknownError as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(409, str(exc)) from exc
    except ClaudeBrowserUnavailableError as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        completions.finish_request_telemetry(
            request_id,
            status="error",
            error=public_error_message(exc),
        )
        raise HTTPException(500, str(exc)) from exc






























































def main() -> None:
    """Run the bridge on the loopback interface."""
    import uvicorn

    uvicorn.run(
        "claude_web_api.app:app",
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8765")),
        reload=False,
    )


if __name__ == "__main__":
    main()
