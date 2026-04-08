import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import StreamEvent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
BOOT_ID = str(uuid.uuid4())
BOOT_TIME = time.time()
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
SESSION_HEADER_CANONICAL = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
MAX_WS_MESSAGE_BYTES = 32 * 1024
EVENT_STREAM_CONTENT_TYPE = "text/event-stream"
PROBE_REQUEST_FIELD = "__agentcore_probe__"
DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "CLAUDE_AGENT_SYSTEM_PROMPT",
    (
        "You are Claude Code running inside Amazon Bedrock AgentCore. "
        "Answer directly and keep responses concise unless the user asks for depth. "
        "Use tools only when they are necessary. "
        "For research tasks, use WebFetch to gather current information and cite sources when helpful."
    ),
)
DEFAULT_ALLOWED_TOOLS = "Read,Glob,Grep,WebFetch,Bash"
ALLOWED_TOOLS = [
    tool.strip()
    for tool in os.environ.get("CLAUDE_AGENT_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS).split(",")
    if tool.strip()
]
PERMISSION_MODE = os.environ.get("CLAUDE_AGENT_PERMISSION_MODE", "bypassPermissions")
MODEL = os.environ.get("ANTHROPIC_MODEL")
LOG_LEVEL = os.environ.get("AGENTCORE_LOG_LEVEL", "INFO").upper()
SESSION_STATES: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SANDBOX_SETTINGS = {
    "enabled": _env_bool("CLAUDE_AGENT_SANDBOX_ENABLED", True),
    "autoAllowBashIfSandboxed": _env_bool("CLAUDE_AGENT_SANDBOX_AUTO_ALLOW_BASH", True),
    "allowUnsandboxedCommands": _env_bool("CLAUDE_AGENT_SANDBOX_ALLOW_UNSANDBOXED", False),
    "enableWeakerNestedSandbox": _env_bool("CLAUDE_AGENT_SANDBOX_WEAKER_NESTED", True),
}
CLAUDE_SUBPROCESS_ENV = {
    "IS_SANDBOX": os.environ.get("IS_SANDBOX", "1"),
}


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
    format="%(message)s",
)
logger = logging.getLogger("agentcore-runtime")


def _log(event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "boot_id": BOOT_ID,
        "pid": os.getpid(),
        **fields,
    }
    logger.info(json.dumps(payload, default=str, separators=(",", ":")))


def _agent_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        allowed_tools=ALLOWED_TOOLS,
        permission_mode=PERMISSION_MODE,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        model=MODEL,
        cwd="/app",
        env=CLAUDE_SUBPROCESS_ENV,
        include_partial_messages=True,
        sandbox=SANDBOX_SETTINGS,
    )


def _sticky_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_count": state["request_count"],
        "prompts": state["prompts"],
        "boot_id": BOOT_ID,
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - BOOT_TIME, 3),
        "session_age_seconds": round(time.time() - state["created_at"], 3),
    }


def _probe_response(runtime_session_id: str) -> dict[str, Any]:
    return {
        "status": "Healthy",
        "probe": True,
        "runtime_session_id": runtime_session_id,
        "boot_id": BOOT_ID,
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - BOOT_TIME, 3),
        "http_streaming_content_type": EVENT_STREAM_CONTENT_TYPE,
        "websocket_path": "/ws",
    }


async def _get_or_create_session(runtime_session_id: str) -> dict[str, Any]:
    state = SESSION_STATES.get(runtime_session_id)
    if state is not None:
        _log(
            "session_reused",
            runtime_session_id=runtime_session_id,
            request_count=state["request_count"],
        )
        return state

    client = ClaudeSDKClient(options=_agent_options())
    await client.connect()
    state = {
        "client": client,
        "lock": asyncio.Lock(),
        "request_count": 0,
        "prompts": [],
        "created_at": time.time(),
    }
    SESSION_STATES[runtime_session_id] = state
    _log("session_created", runtime_session_id=runtime_session_id)
    return state


async def _disconnect_all_sessions() -> None:
    _log("shutdown_started", open_session_count=len(SESSION_STATES))
    for state in list(SESSION_STATES.values()):
        try:
            await state["client"].disconnect()
        except Exception:
            pass
    SESSION_STATES.clear()
    _log("shutdown_completed")


def _extract_prompt(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("prompt", "inputText", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return ""


def _is_probe_request(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get(PROBE_REQUEST_FIELD) is True


def _session_id_from_request(request: Request) -> str:
    return (
        request.headers.get(SESSION_HEADER_CANONICAL)
        or request.headers.get(SESSION_HEADER)
        or request.query_params.get("session_id")
        or request.query_params.get(SESSION_HEADER_CANONICAL)
        or request.query_params.get(SESSION_HEADER)
        or "missing-session-id"
    )


def _session_id_from_websocket(websocket: WebSocket) -> str:
    return (
        websocket.headers.get(SESSION_HEADER_CANONICAL)
        or websocket.headers.get(SESSION_HEADER)
        or websocket.query_params.get("session_id")
        or websocket.query_params.get(SESSION_HEADER_CANONICAL)
        or websocket.query_params.get(SESSION_HEADER)
        or "missing-session-id"
    )


def _result_payload(message: ResultMessage) -> dict[str, Any]:
    return {
        "subtype": message.subtype,
        "duration_ms": message.duration_ms,
        "duration_api_ms": message.duration_api_ms,
        "is_error": message.is_error,
        "num_turns": message.num_turns,
        "session_id": message.session_id,
        "total_cost_usd": message.total_cost_usd,
        "usage": message.usage,
    }


def _http_stream_requested(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if EVENT_STREAM_CONTENT_TYPE in accept.lower():
        return True

    stream_param = request.query_params.get("stream", "").strip().lower()
    return stream_param in {"1", "true", "yes"}


def _encode_sse_event(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode("utf-8")


def _assistant_texts(message: AssistantMessage) -> list[str]:
    texts = []
    for block in message.content:
        text = getattr(block, "text", None)
        if text:
            cleaned = text.strip()
            if cleaned:
                texts.append(cleaned)
    return texts


async def _run_turn(
    runtime_session_id: str,
    prompt: str,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    state = await _get_or_create_session(runtime_session_id)
    client = state["client"]
    assistant_messages: list[str] = []
    streamed_text_chunks: list[str] = []
    result_message: dict[str, Any] | None = None
    current_tool_use: dict[str, Any] | None = None

    # Each sticky runtime session shares one Claude client, so turns must run serially.
    async with state["lock"]:
        state["request_count"] += 1
        state["prompts"].append(prompt)
        _log(
            "turn_started",
            runtime_session_id=runtime_session_id,
            request_count=state["request_count"],
            prompt_preview=prompt[:200],
        )

        await client.query(prompt)

        async for message in client.receive_response():
            if isinstance(message, StreamEvent):
                event = message.event
                event_type = event.get("type")

                if event_type == "content_block_start":
                    content_block = event.get("content_block", {})
                    if content_block.get("type") == "tool_use":
                        current_tool_use = {
                            "id": content_block.get("id"),
                            "name": content_block.get("name"),
                            "input": "",
                        }
                        if on_event is not None:
                            await on_event(
                                {
                                    "type": "tool_call_start",
                                    "tool_name": current_tool_use["name"],
                                    "tool_use_id": current_tool_use["id"],
                                    "runtime_session_id": runtime_session_id,
                                }
                            )
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            streamed_text_chunks.append(text)
                            if on_event is not None:
                                await on_event(
                                    {
                                        "type": "assistant_delta",
                                        "text": text,
                                        "runtime_session_id": runtime_session_id,
                                        "parent_tool_use_id": message.parent_tool_use_id,
                                    }
                                )
                    elif delta_type == "input_json_delta" and current_tool_use is not None:
                        partial_json = delta.get("partial_json", "")
                        if partial_json:
                            current_tool_use["input"] += partial_json
                            if on_event is not None:
                                await on_event(
                                    {
                                        "type": "tool_call_delta",
                                        "tool_name": current_tool_use["name"],
                                        "tool_use_id": current_tool_use["id"],
                                        "partial_json": partial_json,
                                        "runtime_session_id": runtime_session_id,
                                    }
                                )
                elif event_type == "content_block_stop" and current_tool_use is not None:
                    if on_event is not None:
                        await on_event(
                            {
                                "type": "tool_call_stop",
                                "tool_name": current_tool_use["name"],
                                "tool_use_id": current_tool_use["id"],
                                "input": current_tool_use["input"],
                                "runtime_session_id": runtime_session_id,
                            }
                        )
                    current_tool_use = None
            elif isinstance(message, AssistantMessage):
                texts = _assistant_texts(message)
                if texts:
                    assistant_messages.extend(texts)
                if texts and on_event is not None:
                    await on_event(
                        {
                            "type": "assistant_message",
                            "texts": texts,
                            "model": message.model,
                            "runtime_session_id": runtime_session_id,
                            "sticky_state": _sticky_state(state),
                        }
                    )
            elif isinstance(message, SystemMessage) and on_event is not None:
                await on_event(
                    {
                        "type": "system_message",
                        "subtype": message.subtype,
                        "data": message.data,
                        "runtime_session_id": runtime_session_id,
                    }
                )
            elif isinstance(message, ResultMessage):
                result_message = _result_payload(message)
                if on_event is not None:
                    await on_event(
                        {
                            "type": "result",
                            "result": result_message,
                            "runtime_session_id": runtime_session_id,
                        }
                    )

    response_text = "\n".join(assistant_messages)
    if not response_text and streamed_text_chunks:
        response_text = "".join(streamed_text_chunks).strip()
    if not response_text:
        response_text = "(no assistant text returned)"
    payload = {
        "status": "success",
        "response": response_text,
        "runtime_session_id": runtime_session_id,
        "sticky_state": _sticky_state(state),
        "result": result_message,
    }
    _log(
        "turn_completed",
        runtime_session_id=runtime_session_id,
        request_count=state["request_count"],
        response_preview=response_text[:200],
        result=result_message,
    )

    if on_event is not None:
        await on_event({"type": "turn_complete", **payload})

    return payload


async def _stream_turn_over_http(
    runtime_session_id: str,
    prompt: str,
    request_payload: Any,
):
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def on_event(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            await queue.put(
                {
                    "type": "session_started",
                    "runtime_session_id": runtime_session_id,
                    "boot_id": BOOT_ID,
                }
            )
            await _run_turn(runtime_session_id, prompt, on_event=on_event)
        except Exception as exc:
            await queue.put(
                {
                    "type": "error",
                    "error": str(exc),
                    "runtime_session_id": runtime_session_id,
                    "request": request_payload,
                }
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _encode_sse_event(event)
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def ping(_: Request) -> JSONResponse:
    _log("ping")
    return JSONResponse(
        {
            "status": "Healthy",
            "boot_id": BOOT_ID,
            "claude_code_use_bedrock": os.environ.get("CLAUDE_CODE_USE_BEDROCK"),
            "anthropic_model": os.environ.get("ANTHROPIC_MODEL"),
            "http_streaming_content_type": EVENT_STREAM_CONTENT_TYPE,
            "websocket_path": "/ws",
        }
    )


async def invocations(request: Request) -> JSONResponse:
    runtime_session_id = _session_id_from_request(request)
    raw = await request.body()

    try:
        request_payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        request_payload = {"raw": raw.decode("utf-8", errors="replace")}

    if _is_probe_request(request_payload):
        _log("invocation_probe", runtime_session_id=runtime_session_id)
        return JSONResponse(_probe_response(runtime_session_id))

    prompt = _extract_prompt(request_payload)
    if not prompt and isinstance(request_payload, dict):
        prompt = request_payload.get("raw", "")

    if _http_stream_requested(request):
        _log(
            "http_stream_requested",
            runtime_session_id=runtime_session_id,
            request_payload=request_payload,
        )
        return StreamingResponse(
            _stream_turn_over_http(runtime_session_id, prompt, request_payload),
            media_type=EVENT_STREAM_CONTENT_TYPE,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        payload = await _run_turn(runtime_session_id, prompt)
        payload["request"] = request_payload
        return JSONResponse(payload)
    except Exception as exc:
        _log(
            "http_invocation_error",
            runtime_session_id=runtime_session_id,
            error=str(exc),
            request_payload=request_payload,
        )
        return JSONResponse(
            {
                "status": "error",
                "error": str(exc),
                "runtime_session_id": runtime_session_id,
                "request": request_payload,
            },
            status_code=500,
        )


async def websocket_endpoint(websocket: WebSocket) -> None:
    runtime_session_id = _session_id_from_websocket(websocket)
    await websocket.accept()
    _log("websocket_connected", runtime_session_id=runtime_session_id)
    await websocket.send_json(
        {
            "type": "session_started",
            "runtime_session_id": runtime_session_id,
            "boot_id": BOOT_ID,
        }
    )

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                await websocket.close(code=1003, reason="Binary frames are not supported")
                return

            raw_text = message.get("text") or ""
            if len(raw_text.encode("utf-8")) > MAX_WS_MESSAGE_BYTES:
                await websocket.close(code=1009, reason="Message frame too large")
                return

            try:
                request_payload = json.loads(raw_text) if raw_text else {}
            except json.JSONDecodeError:
                request_payload = {"prompt": raw_text}

            prompt = _extract_prompt(request_payload)
            if not prompt:
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": "Expected a text frame or JSON payload with prompt, inputText, text, or message.",
                        "runtime_session_id": runtime_session_id,
                    }
                )
                continue

            try:
                await _run_turn(runtime_session_id, prompt, on_event=websocket.send_json)
            except Exception as exc:
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": str(exc),
                        "runtime_session_id": runtime_session_id,
                    }
                )
    except WebSocketDisconnect:
        _log("websocket_disconnected", runtime_session_id=runtime_session_id)
        return
    finally:
        _log("websocket_closed", runtime_session_id=runtime_session_id)


@asynccontextmanager
async def lifespan(_: Starlette):
    _log(
        "startup",
        model=MODEL,
        allowed_tools=ALLOWED_TOOLS,
        permission_mode=PERMISSION_MODE,
        sandbox=SANDBOX_SETTINGS,
        subprocess_env=CLAUDE_SUBPROCESS_ENV,
    )
    yield
    await _disconnect_all_sessions()


app = Starlette(
    debug=False,
    routes=[
        Route("/ping", ping, methods=["GET"]),
        Route("/invocations", invocations, methods=["POST"]),
        WebSocketRoute("/ws", websocket_endpoint),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
