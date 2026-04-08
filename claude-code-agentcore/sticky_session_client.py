import argparse
import asyncio
import json
import os
import ssl
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode

import boto3
from botocore.auth import SigV4Auth
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import get_cert_path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        value = os.path.expandvars(value)
        os.environ.setdefault(key, value)


load_env_file(Path(__file__).resolve().with_name(".env"))


def default_runtime_arn() -> str:
    explicit = os.environ.get("AGENT_RUNTIME_ARN", "").strip()
    if explicit:
        return explicit

    region = os.environ.get("AWS_REGION", "").strip()
    account_id = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    runtime_id = os.environ.get("AGENT_RUNTIME_ID", "").strip()
    if region and account_id and runtime_id:
        return (
            f"arn:aws:bedrock-agentcore:{region}:{account_id}:"
            f"runtime/{runtime_id}"
        )
    return ""


DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_PROFILE = os.environ.get("AWS_PROFILE", "default")
DEFAULT_RUNTIME_ARN = default_runtime_arn()
DEFAULT_RUNTIME_QUALIFIER = os.environ.get("RUNTIME_QUALIFIER", "DEFAULT")
MIN_SESSION_ID_LENGTH = 33
DEFAULT_WS_URL_EXPIRES = 300
EVENT_STREAM_CONTENT_TYPE = "text/event-stream"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
PROBE_REQUEST_FIELD = "__agentcore_probe__"


def normalize_session_id(value: str) -> str:
    if len(value) >= MIN_SESSION_ID_LENGTH:
        return value
    suffix = str(uuid.uuid4())
    normalized = f"{value}-{suffix}"
    return normalized[: max(MIN_SESSION_ID_LENGTH, len(normalized))]


def require_runtime_arn(
    runtime_arn: str,
    *,
    parser: argparse.ArgumentParser,
    endpoint_url: str | None = None,
    ws_url: str | None = None,
) -> None:
    if runtime_arn or endpoint_url or ws_url:
        return
    parser.error(
        "runtime ARN required. Set AGENT_RUNTIME_ARN in .env or pass --runtime-arn."
    )


def encode_runtime_identifier(value: str) -> str:
    return quote(value, safe="")


def probe_payload() -> bytes:
    return json.dumps({PROBE_REQUEST_FIELD: True}).encode("utf-8")


def invoke(
    client,
    runtime_arn: str,
    session_id: str,
    prompt: str,
    http_stream: bool = False,
    on_stream_event=None,
    collect_events: bool = True,
) -> dict:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept=EVENT_STREAM_CONTENT_TYPE if http_stream else "application/json",
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
    )
    content_type = response["contentType"]
    runtime_session_id = response["runtimeSessionId"]
    status_code = response["statusCode"]

    if EVENT_STREAM_CONTENT_TYPE in content_type:
        events = [] if collect_events else None
        final_body = None
        for line in response["response"].iter_lines(chunk_size=1):
            if not line:
                continue

            decoded = line.decode("utf-8")
            if not decoded.startswith("data: "):
                continue

            payload = decoded[6:]
            event = json.loads(payload)
            if events is not None:
                events.append(event)
            if on_stream_event is not None:
                on_stream_event(event)

            if event.get("type") in {"turn_complete", "error"}:
                final_body = event

        return {
            "runtime_session_id": runtime_session_id,
            "status_code": status_code,
            "content_type": content_type,
            "events": events or [],
            "body": final_body,
        }

    body = response["response"].read().decode("utf-8")
    parsed = json.loads(body)
    return {
        "runtime_session_id": runtime_session_id,
        "status_code": status_code,
        "content_type": content_type,
        "body": parsed,
    }


def ping_runtime(
    boto_session: boto3.Session,
    runtime_arn: str,
    region: str,
    session_id: str,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    ssl_context = create_https_ssl_context(boto_session)
    headers = {
        "Accept": "application/json",
        SESSION_HEADER: session_id,
    }

    if endpoint_url:
        url = f"{endpoint_url.rstrip('/')}/ping"
        request = Request(url=url, headers=headers, method="GET")
    else:
        credentials = boto_session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available for ping request signing.")

        frozen_credentials = credentials.get_frozen_credentials()
        encoded_runtime_arn = encode_runtime_identifier(runtime_arn)
        url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/"
            f"runtimes/{encoded_runtime_arn}/ping"
        )
        aws_request = AWSRequest(method="GET", url=url, headers=headers)
        SigV4Auth(frozen_credentials, "bedrock-agentcore", region).add_auth(aws_request)
        request = Request(
            url=aws_request.url,
            headers=dict(aws_request.headers.items()),
            method="GET",
        )

    try:
        with urlopen(request, context=ssl_context) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return {
                "status_code": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "body": parsed,
                "runtime_session_id": session_id,
                "url": url,
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return {
            "status_code": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
            "body": parsed,
            "runtime_session_id": session_id,
            "url": url,
        }


def probe_runtime(
    boto_session: boto3.Session,
    runtime_arn: str,
    region: str,
    session_id: str,
    qualifier: str,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    ssl_context = create_https_ssl_context(boto_session)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        SESSION_HEADER: session_id,
    }
    payload = probe_payload()

    if endpoint_url:
        url = f"{endpoint_url.rstrip('/')}/invocations"
        request = Request(url=url, data=payload, headers=headers, method="POST")
    else:
        credentials = boto_session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available for probe request signing.")

        frozen_credentials = credentials.get_frozen_credentials()
        encoded_runtime_arn = encode_runtime_identifier(runtime_arn)
        url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/"
            f"runtimes/{encoded_runtime_arn}/invocations?qualifier={quote(qualifier, safe='')}"
        )
        aws_request = AWSRequest(method="POST", url=url, data=payload, headers=headers)
        SigV4Auth(frozen_credentials, "bedrock-agentcore", region).add_auth(aws_request)
        request = Request(
            url=aws_request.url,
            data=payload,
            headers=dict(aws_request.headers.items()),
            method="POST",
        )

    try:
        with urlopen(request, context=ssl_context) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return {
                "status_code": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "body": parsed,
                "runtime_session_id": session_id,
                "url": url,
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return {
            "status_code": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
            "body": parsed,
            "runtime_session_id": session_id,
            "url": url,
        }


def generate_presigned_websocket_url(
    session: boto3.Session,
    runtime_arn: str,
    region: str,
    expires: int,
    qualifier: str,
) -> str:
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials available for WebSocket signing.")

    frozen_credentials = credentials.get_frozen_credentials()
    encoded_runtime_arn = encode_runtime_identifier(runtime_arn)
    query = urlencode({"qualifier": qualifier})
    request = AWSRequest(
        method="GET",
        url=(
            f"https://bedrock-agentcore.{region}.amazonaws.com/"
            f"runtimes/{encoded_runtime_arn}/ws?{query}"
        ),
    )
    SigV4QueryAuth(
        frozen_credentials,
        "bedrock-agentcore",
        region,
        expires=expires,
    ).add_auth(request)
    return request.url.replace("https://", "wss://", 1)


def create_websocket_ssl_context(boto_session: boto3.Session) -> ssl.SSLContext:
    ca_bundle = boto_session._session.get_config_variable("ca_bundle")
    ssl_context = ssl.create_default_context(cafile=get_cert_path(ca_bundle or True))
    return ssl_context


def websocket_headers(session_id: str) -> dict[str, str]:
    return {SESSION_HEADER: session_id}


def create_https_ssl_context(boto_session: boto3.Session) -> ssl.SSLContext:
    ca_bundle = boto_session._session.get_config_variable("ca_bundle")
    ssl_context = ssl.create_default_context(cafile=get_cert_path(ca_bundle or True))
    return ssl_context


def _ensure_stream_line_closed(render_state: dict[str, Any]) -> None:
    if render_state.get("assistant_open"):
        print()
        render_state["assistant_open"] = False


def print_http_result(result: dict, show_json: bool) -> None:
    if EVENT_STREAM_CONTENT_TYPE in result.get("content_type", ""):
        render_state = {"assistant_open": False, "saw_assistant_delta": False}
        for event in result.get("events", []):
            print_runtime_event(event, show_json=show_json, render_state=render_state)
        _ensure_stream_line_closed(render_state)
        return

    body = result["body"]
    sticky_state = body.get("sticky_state", {})

    print(f"assistant> {body.get('response', '')}")
    print(
        "session> "
        f"id={result['runtime_session_id']} "
        f"count={sticky_state.get('request_count', '?')} "
        f"boot_id={sticky_state.get('boot_id', '?')}"
    )

    if show_json:
        print(json.dumps(result, indent=2))


def print_ping_result(result: dict[str, Any], show_json: bool) -> None:
    if show_json:
        print(json.dumps(result, indent=2))
        return

    body = result.get("body", {})
    print(
        "ping> "
        f"http_status={result.get('status_code', '?')} "
        f"status={body.get('status', '?')}"
    )

    if "time_of_last_update" in body:
        print(f"ping> time_of_last_update={body['time_of_last_update']}")

    print(f"session> id={result.get('runtime_session_id', '?')}")


def print_probe_result(result: dict[str, Any], show_json: bool) -> None:
    if show_json:
        print(json.dumps(result, indent=2))
        return

    body = result.get("body", {})
    print(
        "probe> "
        f"http_status={result.get('status_code', '?')} "
        f"status={body.get('status', '?')} "
        f"probe={body.get('probe', '?')}"
    )
    print(
        "session> "
        f"id={body.get('runtime_session_id', result.get('runtime_session_id', '?'))} "
        f"boot_id={body.get('boot_id', '?')}"
    )


def print_help(transport: str) -> None:
    print("Commands:")
    print("  /help     Show this help")
    if transport == "http":
        print("  /ping     Call GET /ping for the current session")
        print("  /probe    Call a lightweight POST /invocations probe for the current session")
    print("  /session  Show the current runtime session ID")
    print("  /new      Start using a fresh runtime session ID")
    print("  /quit     Exit")
    if transport == "ws":
        print("  /reconnect  Reconnect the current WebSocket session")


def print_runtime_event(
    event: dict[str, Any],
    show_json: bool,
    render_state: dict[str, Any] | None = None,
) -> None:
    render_state = render_state or {}
    event_type = event.get("type", "unknown")

    if show_json:
        print(json.dumps(event, indent=2))
        return

    if event_type == "session_started":
        _ensure_stream_line_closed(render_state)
        print(
            "session> "
            f"id={event.get('runtime_session_id', '?')} "
            f"boot_id={event.get('boot_id', '?')}"
        )
        return

    if event_type == "assistant_delta":
        text = event.get("text", "")
        if not text:
            return
        if not render_state.get("assistant_open"):
            print("assistant> ", end="", flush=True)
            render_state["assistant_open"] = True
        print(text, end="", flush=True)
        render_state["saw_assistant_delta"] = True
        return

    if event_type == "assistant_message":
        if render_state.get("saw_assistant_delta"):
            return
        for text in event.get("texts", []):
            print(f"assistant> {text}")
        return

    if event_type == "system_message":
        _ensure_stream_line_closed(render_state)
        print(f"system> subtype={event.get('subtype', '?')}")
        return

    if event_type == "tool_call_start":
        _ensure_stream_line_closed(render_state)
        print(f"tool> using {event.get('tool_name', '?')}")
        return

    if event_type == "result":
        _ensure_stream_line_closed(render_state)
        result = event.get("result", {})
        print(
            "result> "
            f"session_id={result.get('session_id', '?')} "
            f"turns={result.get('num_turns', '?')} "
            f"error={result.get('is_error', '?')}"
        )
        return

    if event_type == "turn_complete":
        _ensure_stream_line_closed(render_state)
        sticky_state = event.get("sticky_state", {})
        print(
            "session> "
            f"id={event.get('runtime_session_id', '?')} "
            f"count={sticky_state.get('request_count', '?')} "
            f"boot_id={sticky_state.get('boot_id', '?')}"
        )
        render_state["saw_assistant_delta"] = False
        return

    if event_type == "error":
        _ensure_stream_line_closed(render_state)
        print(f"error> {event.get('error', 'unknown error')}")
        render_state["saw_assistant_delta"] = False
        return

    if event_type in {"tool_call_delta", "tool_call_stop"}:
        return

    _ensure_stream_line_closed(render_state)
    print(f"event> {json.dumps(event)}")


async def prompt_async(prompt_text: str) -> str:
    # Keep stdin reads off the event loop so WebSocket keepalives continue.
    return await asyncio.to_thread(input, prompt_text)


def is_websocket_closed_error(exc: Exception) -> bool:
    return exc.__class__.__name__ in {
        "ConnectionClosed",
        "ConnectionClosedError",
        "ConnectionClosedOK",
    }


async def receive_websocket_turn(ws, show_json: bool) -> None:
    render_state = {"assistant_open": False, "saw_assistant_delta": False}
    while True:
        raw_message = await ws.recv()
        event = json.loads(raw_message)
        print_runtime_event(event, show_json=show_json, render_state=render_state)
        if event.get("type") in {"turn_complete", "error"}:
            _ensure_stream_line_closed(render_state)
            return


async def maybe_receive_websocket_event(
    ws,
    show_json: bool,
    timeout_seconds: float = 1.0,
) -> None:
    try:
        raw_message = await asyncio.wait_for(ws.recv(), timeout=timeout_seconds)
    except TimeoutError:
        return

    event = json.loads(raw_message)
    print_runtime_event(event, show_json=show_json)


async def connect_websocket(
    boto_session: boto3.Session,
    runtime_arn: str,
    region: str,
    session_id: str,
    expires: int,
    qualifier: str,
    ws_url: str | None = None,
):
    try:
        import websockets
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "WebSocket mode requires the 'websockets' package. "
            "Use the rebuilt runtime image or install websockets locally."
        ) from exc

    connection_url = ws_url or generate_presigned_websocket_url(
        session=boto_session,
        runtime_arn=runtime_arn,
        region=region,
        expires=expires,
        qualifier=qualifier,
    )
    headers = websocket_headers(session_id)
    if connection_url.startswith("wss://"):
        ssl_context = create_websocket_ssl_context(boto_session)
        return websockets.connect(
            connection_url,
            ssl=ssl_context,
            additional_headers=headers,
        )
    return websockets.connect(connection_url, additional_headers=headers)


async def websocket_batch_session(
    boto_session: boto3.Session,
    runtime_arn: str,
    region: str,
    session_id: str,
    prompts: list[str],
    show_json: bool,
    compare_new_session: bool,
    ws_url_expires: int,
    runtime_qualifier: str,
    ws_url: str | None,
) -> None:
    if ws_url:
        print(f"ws_url={ws_url}")
    else:
        print(f"runtime_arn={runtime_arn}")
        print(f"runtime_qualifier={runtime_qualifier}")
    print(f"sticky_session_id={session_id}")

    async with await connect_websocket(
        boto_session,
        runtime_arn,
        region,
        session_id,
        ws_url_expires,
        runtime_qualifier,
        ws_url=ws_url,
    ) as ws:
        await maybe_receive_websocket_event(ws, show_json=show_json)

        for index, prompt in enumerate(prompts, start=1):
            print(f"\n=== streaming call {index} ===")
            await ws.send(json.dumps({"prompt": prompt}))
            await receive_websocket_turn(ws, show_json=show_json)

    if compare_new_session:
        comparison_session_id = normalize_session_id(f"sticky-{uuid.uuid4()}")
        print(f"\n=== new session comparison ===")
        print(f"comparison_session_id={comparison_session_id}")
        async with await connect_websocket(
            boto_session,
            runtime_arn,
            region,
            comparison_session_id,
            ws_url_expires,
            runtime_qualifier,
            ws_url=ws_url,
        ) as ws:
            await maybe_receive_websocket_event(ws, show_json=show_json)
            await ws.send(
                json.dumps({"prompt": "comparison call with a new session"})
            )
            await receive_websocket_turn(ws, show_json=show_json)


async def interactive_websocket_session(
    boto_session: boto3.Session,
    runtime_arn: str,
    region: str,
    session_id: str,
    show_json: bool,
    ws_url_expires: int,
    runtime_qualifier: str,
    ws_url: str | None,
) -> None:
    current_session_id = session_id

    if ws_url:
        print(f"ws_url={ws_url}")
    else:
        print(f"runtime_arn={runtime_arn}")
        print(f"runtime_qualifier={runtime_qualifier}")
    print(f"sticky_session_id={current_session_id}")
    print("Interactive AgentCore WebSocket session. Type /help for commands.")

    while True:
        async with await connect_websocket(
            boto_session,
            runtime_arn,
            region,
            current_session_id,
            ws_url_expires,
            runtime_qualifier,
            ws_url=ws_url,
        ) as ws:
            await maybe_receive_websocket_event(ws, show_json=show_json)

            while True:
                try:
                    prompt = (await prompt_async("\nyou> ")).strip()
                except EOFError:
                    print("\nExiting.")
                    return
                except KeyboardInterrupt:
                    print("\nExiting.")
                    return

                if not prompt:
                    continue
                if prompt == "/quit":
                    return
                if prompt == "/help":
                    print_help("ws")
                    continue
                if prompt == "/session":
                    print(f"sticky_session_id={current_session_id}")
                    continue
                if prompt == "/new":
                    current_session_id = normalize_session_id(f"sticky-{uuid.uuid4()}")
                    print(f"sticky_session_id={current_session_id}")
                    break
                if prompt == "/reconnect":
                    print("session> reconnecting")
                    break
                if prompt.startswith("/"):
                    print("Unknown command. Type /help for commands.")
                    continue

                try:
                    await ws.send(json.dumps({"prompt": prompt}))
                    await receive_websocket_turn(ws, show_json=show_json)
                except Exception as exc:
                    if not is_websocket_closed_error(exc):
                        raise
                    print(f"session> websocket closed: {exc}")
                    print("session> reconnecting")
                    break


def interactive_http_session(
    boto_session: boto3.Session,
    client,
    runtime_arn: str,
    region: str,
    session_id: str,
    runtime_qualifier: str,
    show_json: bool,
    http_stream: bool,
    endpoint_url: str | None,
) -> None:
    current_session_id = session_id

    print(f"runtime_arn={runtime_arn}")
    print(f"sticky_session_id={current_session_id}")
    print("Interactive AgentCore HTTP session. Type /help for commands.")

    while True:
        try:
            prompt = input("\nyou> ").strip()
        except EOFError:
            print("\nExiting.")
            return
        except KeyboardInterrupt:
            print("\nExiting.")
            return

        if not prompt:
            continue
        if prompt == "/quit":
            return
        if prompt == "/help":
            print_help("http")
            continue
        if prompt == "/ping":
            result = ping_runtime(
                boto_session=boto_session,
                runtime_arn=runtime_arn,
                region=region,
                session_id=current_session_id,
                endpoint_url=endpoint_url,
            )
            print_ping_result(result, show_json=show_json)
            continue
        if prompt == "/probe":
            result = probe_runtime(
                boto_session=boto_session,
                runtime_arn=runtime_arn,
                region=region,
                session_id=current_session_id,
                qualifier=runtime_qualifier,
                endpoint_url=endpoint_url,
            )
            print_probe_result(result, show_json=show_json)
            continue
        if prompt == "/session":
            print(f"sticky_session_id={current_session_id}")
            continue
        if prompt == "/new":
            current_session_id = normalize_session_id(f"sticky-{uuid.uuid4()}")
            print(f"sticky_session_id={current_session_id}")
            continue
        if prompt.startswith("/"):
            print("Unknown command. Type /help for commands.")
            continue

        result = invoke(
            client,
            runtime_arn,
            current_session_id,
            prompt,
            http_stream=http_stream,
            on_stream_event=(
                lambda event: print_runtime_event(
                    event,
                    show_json=show_json,
                    render_state=interactive_http_session.render_state,
                )
            )
            if http_stream
            else None,
            collect_events=show_json,
        )
        if http_stream:
            _ensure_stream_line_closed(interactive_http_session.render_state)
            if show_json:
                print(json.dumps(result, indent=2))
            continue
        print_http_result(result, show_json=show_json)


interactive_http_session.render_state = {
    "assistant_open": False,
    "saw_assistant_delta": False,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Batch or interactive client for an AgentCore runtime session over "
            "HTTP or WebSocket."
        )
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--runtime-arn", default=DEFAULT_RUNTIME_ARN)
    parser.add_argument(
        "--runtime-qualifier",
        default=DEFAULT_RUNTIME_QUALIFIER,
        help="Runtime qualifier appended to presigned WebSocket URLs.",
    )
    parser.add_argument("--session-id", default=f"sticky-{uuid.uuid4()}")
    parser.add_argument(
        "--operation",
        choices=("invoke", "ping", "probe"),
        default="invoke",
        help="Choose agent invocation, GET /ping health check, or lightweight POST /invocations probe.",
    )
    parser.add_argument(
        "--transport",
        choices=("http", "ws"),
        default="http",
        help="Choose HTTP invoke mode or bidirectional WebSocket streaming mode.",
    )
    parser.add_argument(
        "--endpoint-url",
        help="Optional direct base URL for the runtime, for example http://127.0.0.1:8080.",
    )
    parser.add_argument(
        "--ws-url",
        help="Optional direct WebSocket URL, for example ws://127.0.0.1:8080/ws.",
    )
    parser.add_argument(
        "--prompts",
        nargs="+",
        help="Run in batch mode with the provided prompts. If omitted, starts an interactive session.",
    )
    parser.add_argument(
        "--compare-new-session",
        action="store_true",
        help="Make one additional call with a fresh session ID for comparison.",
    )
    parser.add_argument(
        "--show-json",
        action="store_true",
        help="Print the full JSON result after each response.",
    )
    parser.add_argument(
        "--http-stream",
        action="store_true",
        help="When using --transport http, request and render a text/event-stream response.",
    )
    parser.add_argument(
        "--ws-url-expires",
        type=int,
        default=DEFAULT_WS_URL_EXPIRES,
        help="Expiry in seconds for the presigned AgentCore WebSocket URL.",
    )
    args = parser.parse_args()
    args.session_id = normalize_session_id(args.session_id)

    require_runtime_arn(
        args.runtime_arn,
        parser=parser,
        endpoint_url=args.endpoint_url,
        ws_url=args.ws_url,
    )

    boto_session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = boto_session.client("bedrock-agentcore")

    if args.operation in {"ping", "probe"}:
        if args.transport != "http":
            parser.error(f"--operation {args.operation} only supports --transport http.")

        if args.endpoint_url:
            print(f"endpoint_url={args.endpoint_url}")
        else:
            print(f"runtime_arn={args.runtime_arn}")
            if args.operation == "probe":
                print(f"runtime_qualifier={args.runtime_qualifier}")
        print(f"sticky_session_id={args.session_id}")

        if args.operation == "ping":
            result = ping_runtime(
                boto_session=boto_session,
                runtime_arn=args.runtime_arn,
                region=args.region,
                session_id=args.session_id,
                endpoint_url=args.endpoint_url,
            )
            print_ping_result(result, show_json=args.show_json)
        else:
            result = probe_runtime(
                boto_session=boto_session,
                runtime_arn=args.runtime_arn,
                region=args.region,
                session_id=args.session_id,
                qualifier=args.runtime_qualifier,
                endpoint_url=args.endpoint_url,
            )
            print_probe_result(result, show_json=args.show_json)
        return

    if args.transport == "ws":
        if not args.prompts:
            asyncio.run(
                interactive_websocket_session(
                    boto_session=boto_session,
                    runtime_arn=args.runtime_arn,
                    region=args.region,
                    session_id=args.session_id,
                    show_json=args.show_json,
                    ws_url_expires=args.ws_url_expires,
                    runtime_qualifier=args.runtime_qualifier,
                    ws_url=args.ws_url,
                )
            )
            return

        asyncio.run(
            websocket_batch_session(
                boto_session=boto_session,
                runtime_arn=args.runtime_arn,
                region=args.region,
                session_id=args.session_id,
                prompts=args.prompts,
                show_json=args.show_json,
                compare_new_session=args.compare_new_session,
                ws_url_expires=args.ws_url_expires,
                runtime_qualifier=args.runtime_qualifier,
                ws_url=args.ws_url,
            )
        )
        return

    if not args.prompts:
        interactive_http_session(
            boto_session,
            client,
            runtime_arn=args.runtime_arn,
            region=args.region,
            session_id=args.session_id,
            runtime_qualifier=args.runtime_qualifier,
            show_json=args.show_json,
            http_stream=args.http_stream,
            endpoint_url=args.endpoint_url,
        )
        return

    print(f"runtime_arn={args.runtime_arn}")
    print(f"sticky_session_id={args.session_id}")

    for index, prompt in enumerate(args.prompts, start=1):
        print(f"\n=== sticky call {index} ===")
        result = invoke(
            client,
            args.runtime_arn,
            args.session_id,
            prompt,
            http_stream=args.http_stream,
            on_stream_event=(
                lambda event: print_runtime_event(
                    event,
                    show_json=args.show_json,
                    render_state=main.render_state,
                )
            )
            if args.http_stream
            else None,
            collect_events=args.show_json,
        )
        if args.http_stream:
            _ensure_stream_line_closed(main.render_state)
            if args.show_json:
                print(json.dumps(result, indent=2))
            continue
        print_http_result(result, show_json=args.show_json)

    if args.compare_new_session:
        comparison_session_id = normalize_session_id(f"sticky-{uuid.uuid4()}")
        print(f"\n=== new session comparison ===")
        print(f"comparison_session_id={comparison_session_id}")
        result = invoke(
            client,
            args.runtime_arn,
            comparison_session_id,
            "comparison call with a new session",
            http_stream=args.http_stream,
            on_stream_event=(
                lambda event: print_runtime_event(
                    event,
                    show_json=args.show_json,
                    render_state=main.render_state,
                )
            )
            if args.http_stream
            else None,
            collect_events=args.show_json,
        )
        if args.http_stream:
            _ensure_stream_line_closed(main.render_state)
            if args.show_json:
                print(json.dumps(result, indent=2))
            return
        print_http_result(result, show_json=args.show_json)


main.render_state = {"assistant_open": False, "saw_assistant_delta": False}


if __name__ == "__main__":
    main()
