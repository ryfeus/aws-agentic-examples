# Agent Session Context

## Summary

This workspace was used to build and deploy an Amazon Bedrock AgentCore runtime backed by Claude Code and the Claude Agent SDK over Bedrock. The goal was to move from a simple echo container to a runtime that can hold sticky conversational state across repeated invocations that share the same AgentCore runtime session ID.

The most recent follow-up changes enabled Claude Agent SDK partial message streaming, updated the HTTP `POST /invocations` path to forward `text/event-stream` output as soon as text deltas arrive, rebuilt and redeployed the runtime image, and updated the local sticky-session client so it renders incremental HTTP streaming output instead of waiting for the full turn to complete. The client was then extended again to support a dedicated `ping` operation for `GET /ping`, including SigV4-signed AWS requests and optional direct local endpoint checks.

The latest changes added a reusable `dev.sh` workflow for local build, local run, ECR push, AgentCore deploy, CloudWatch log queries, and both local and deployed WebSocket checks. The runtime was also updated to emit structured JSON logs to stdout so AgentCore forwards startup, ping, WebSocket, and turn lifecycle events into CloudWatch Logs. The WebSocket client path was aligned with the AgentCore bidirectional streaming samples by using `qualifier` in the presigned URL and sending the runtime session ID in the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header.

After confirming that public signed `GET /ping` requests return `404`, the runtime and client were extended with a lightweight signed `POST /invocations` probe. The runtime now short-circuits requests whose JSON body is `{"__agentcore_probe__": true}` and returns an immediate health payload without invoking Claude, while normal invocations keep their existing behavior. The latest probe update extends that path again so probe requests can surface async task state: plain probe returns `Healthy` or `HealthyBusy` plus async task counters, and probe requests that pass a `task_id` can return that task's current status and optional final result.

The latest runtime update expanded Claude Code access for research and shell execution. The deployed image now allows `WebFetch` and `Bash`, switches the default SDK permission mode to `bypassPermissions`, enables Claude Code sandboxing inside the container with nested-sandbox support enabled, and sets `IS_SANDBOX=1` so bypass-permissions mode works correctly under the container's root user. After that change, both live Bash and WebFetch turns succeeded against the deployed runtime.

The latest repo cleanup moved deployment-specific AWS identifiers out of tracked source files and into a local `.env` file, added a checked-in `.env.example`, and converted the IAM policy JSON files into env-backed templates that `bash dev.sh render-policies` can materialize into a local ignored `generated/` directory.

The latest runtime change added AgentCore-style asynchronous background task support using the patterns described in the AgentCore long-running runtime documentation. `POST /invocations` now accepts reserved async control payloads to start work in the background and poll task state later, `GET /ping` returns `HealthyBusy` while a session has active background work, and both the local client and `dev.sh` expose commands for starting and inspecting async tasks. These changes were deployed live as image `v12` and verified against the deployed AgentCore runtime with real background Bash work plus CloudWatch lifecycle logs. The latest live deploy is `v13`, which adds task-aware probe responses on the same signed `POST /invocations` surface and verifies them against a real running and completed async task.

## Current Runtime

- AWS account: from `.env` `AWS_ACCOUNT_ID`
- Region: from `.env` `AWS_REGION`
- Runtime ARN: from `.env` `AGENT_RUNTIME_ARN`
- Runtime version: `12`
- IAM role: from `.env` `AGENT_RUNTIME_ROLE_NAME`
- ECR repository: from `.env` `ECR_REPOSITORY_URI`
- Current image tag: `v13`
- Current image digest: `sha256:bfa298a270c3e1c4fd191de2403ec405776ed8d4c9fdaa46f88b6764c1bcf4b0`

## Files and Purpose

- `Dockerfile`
  - Builds the runtime image from `python:3.11-slim`
  - Installs `bash`, `@anthropic-ai/claude-code`, `claude-agent-sdk`, `starlette`, and `uvicorn[standard]`
  - Configures Bedrock defaults with:
    - `CLAUDE_CODE_USE_BEDROCK=1`
    - `AWS_REGION=us-east-1`
    - `ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6`
    - `ANTHROPIC_DEFAULT_SONNET_MODEL=us.anthropic.claude-sonnet-4-6`
    - `ANTHROPIC_DEFAULT_HAIKU_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0`
    - `CLAUDE_AGENT_PERMISSION_MODE=bypassPermissions`
    - `CLAUDE_AGENT_ALLOWED_TOOLS=Read,Glob,Grep,WebFetch,Bash`
    - `CLAUDE_AGENT_SANDBOX_ENABLED=true`
    - `CLAUDE_AGENT_SANDBOX_AUTO_ALLOW_BASH=true`
    - `CLAUDE_AGENT_SANDBOX_ALLOW_UNSANDBOXED=false`
    - `CLAUDE_AGENT_SANDBOX_WEAKER_NESTED=true`
    - `IS_SANDBOX=1`
    - `AGENTCORE_LOG_LEVEL=INFO`

- `.env.example`
  - Documents the local deployment-specific configuration that should live in `.env`
  - Includes runtime ID, runtime ARN, role name, ECR repository, and workload identity prefix fields

- `app.py`
  - Runs an ASGI server for AgentCore using Starlette and Uvicorn
  - Exposes `GET /ping`, `POST /invocations`, and WebSocket `/ws`
  - Maintains one `ClaudeSDKClient` per runtime session ID
  - Preserves sticky state in memory inside `SESSION_STATES`
  - Tracks per-session background tasks in memory for AgentCore-style async execution
  - Emits structured JSON logs to stdout for startup, ping, WebSocket connections, and turn lifecycle events so AgentCore forwards them into CloudWatch Logs
  - Short-circuits `POST /invocations` probe bodies that contain `{"__agentcore_probe__": ...}` and returns an immediate health payload without invoking Claude
  - Probe responses now include async task counters, and can return one task's current status plus optional final result when a probe payload includes `task_id`
  - Accepts async control payloads on `POST /invocations` and WebSocket messages via `{"__agentcore_async__": ...}` with actions `start`, `status`, and `list`
  - Returns `{"status":"HealthyBusy"}` from `GET /ping` when the current runtime session still has queued or running background tasks
  - Passes Claude Code sandbox settings into `ClaudeAgentOptions(...)`
  - Passes `IS_SANDBOX=1` into the Claude subprocess environment by default
  - Logs the effective `allowed_tools`, `permission_mode`, `sandbox`, and Claude subprocess env at startup
  - Accepts WebSocket session IDs from either the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header or the `session_id` query parameter
  - Streams structured JSON events over WebSocket including:
    - `session_started`
    - `assistant_delta`
    - `assistant_message`
    - `system_message`
    - `tool_call_start`
    - `tool_call_delta`
    - `tool_call_stop`
    - `result`
    - `turn_complete`
  - Enables Claude Agent SDK partial messages with `include_partial_messages=True`
  - Returns metadata including:
    - `request_count`
    - `prompts`
    - `boot_id`
    - `pid`
    - `uptime_seconds`
    - `session_age_seconds`

- `sticky_session_client.py`
  - Invokes the AgentCore runtime with a chosen runtime session ID
  - Supports a dedicated `--operation ping` mode for `GET /ping`
  - Supports a dedicated `--operation probe` mode for a lightweight signed `POST /invocations` health check
  - Supports `--operation async-start`, `--operation async-status`, and `--operation async-list` over the same signed `POST /invocations` path
  - Supports both HTTP and bidirectional WebSocket transports
  - Supports direct WebSocket checks against either a presigned AgentCore runtime URL or a local `ws://.../ws` endpoint via `--ws-url`
  - In HTTP streaming mode, consumes `text/event-stream` output incrementally and prints assistant text as `assistant_delta` chunks arrive
  - Can call `GET /ping` either through the public AgentCore runtime URL or directly against a local base URL with `--endpoint-url`
  - Can call the lightweight probe either through the public AgentCore runtime URL or directly against a local base URL with `--endpoint-url`
  - WebSocket mode generates a SigV4 presigned `wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<runtimeArn>/ws` URL with `session_id` and `qualifier`
  - Supports batch mode with `--prompts`
  - Defaults to interactive mode if `--prompts` is omitted
  - Supports `--transport http` and `--transport ws`
  - In WebSocket mode, renders streaming events including:
    - `session_started`
    - `assistant_message`
    - `system_message`
    - `result`
    - `turn_complete`
  - Interactive commands:
    - `/help`
    - `/ping` in HTTP mode
    - `/probe` in HTTP mode
    - `/async <prompt>` in HTTP mode
    - `/task <task_id>` in HTTP mode
    - `/tasks` in HTTP mode
    - `/session`
    - `/new`
    - `/quit`
    - `/reconnect` in WebSocket mode
  - Can print compact response output and optional full JSON

- `dev.sh`
  - Automates local Docker build and local container workflows
  - Loads deployment-specific configuration from `.env` when present
  - Preserves shell-provided env overrides instead of letting `.env` overwrite them
  - Can render env-backed IAM policy templates with `render-policies`
  - Can smoke-test the local runtime with `GET /ping` and streaming `POST /invocations`
  - Can run a lightweight health probe through `probe-local` and `probe-aws`
  - Can start and inspect local or deployed background tasks with `async-start-*`, `async-status-*`, and `async-list-*`
  - Can check both local and deployed WebSocket connectivity using the same SigV4 and direct-`ws://` patterns used in the AgentCore bidirectional streaming samples
  - Can resolve and query the runtime's CloudWatch Logs group with `logs-aws-group`, `logs-aws-streams`, `logs-aws`, and `logs-aws-follow`
  - Can log in to ECR, push a tagged image, update the AgentCore runtime to that image digest, and wait for `READY`
  - Uses the current runtime configuration as deploy defaults so role, network mode, protocol, lifecycle, and MMDSv2 settings stay aligned

- `trust-policy.json`
  - IAM trust policy template for `bedrock-agentcore.amazonaws.com`
  - Uses `${AWS_ACCOUNT_ID}` and `${AWS_REGION}` placeholders that are rendered from `.env`

- `runtime-execution-policy.json`
  - Execution policy template for ECR image pull, CloudWatch Logs, X-Ray, CloudWatch metrics, AgentCore workload token access, and Bedrock model invocation
  - Uses `${AWS_ACCOUNT_ID}`, `${AWS_REGION}`, `${ECR_REPOSITORY_NAME}`, and `${WORKLOAD_IDENTITY_NAME_PREFIX}` placeholders that are rendered from `.env`

- `invoke-response.json`
  - Older response from the initial echo-based runtime

- `claude-runtime-response.json`
  - Successful response from the Claude-over-Bedrock runtime

## Verified Behavior

### Direct Bedrock Runtime Invoke

The deployed runtime successfully handled a prompt requesting the exact response `connected to Bedrock`.

Observed response in `claude-runtime-response.json`:

- `status`: `success`
- `response`: `connected to Bedrock`
- `runtime_session_id`: `claude-bedrock-test-session-20260406-0001`

### Sticky Session Memory Test

The runtime was tested with two prompts using the same runtime session:

1. `My favorite color is teal. Reply with ok.`
2. `What is my favorite color?`

Observed behavior:

- First response: `ok`
- Second response: `Your favorite color is teal.`

This confirmed that the runtime preserved conversational context for the same `runtimeSessionId`.

### Interactive Client Sanity Check

After the REPL update, the client was sanity-checked with:

`python3 sticky_session_client.py --profile default --region us-east-1 --session-id interactive-demo-20260406 --prompts "Reply with exactly ok"`

Observed result:

- Assistant response: `ok`

### Local WebSocket Handshake Smoke Test

The rebuilt `v5` container was started locally and a WebSocket client connected to:

`ws://127.0.0.1:8080/ws?session_id=local-websocket-smoke-test-20260406`

Observed first frame:

- `{"type":"session_started","runtime_session_id":"local-websocket-smoke-test-20260406",...}`

This confirmed that the container now accepts WebSocket connections on `/ws` as required by AgentCore Runtime.

### Streaming Client Update

The local `sticky_session_client.py` was updated to support AgentCore bidirectional streaming over WebSockets in addition to the existing HTTP invoke path.

Verification completed:

- `python3 -m py_compile sticky_session_client.py` passed
- A live AWS WebSocket client run later completed successfully from the host machine after the client was updated to use the sample-aligned `qualifier` URL plus session header pattern
- The host Python environment now has the `websockets` package installed and usable

### HTTP Response Streaming Verification

The runtime was updated to support AgentCore HTTP response streaming over `POST /invocations` using `text/event-stream`, and then updated again to stream Claude Agent SDK partial message output without waiting for the completed assistant message.

Verification completed on April 7, 2026:

- Built and pushed image tag `v6`
- Updated the AgentCore runtime to version `5`
- Verified the deployed runtime now points at image digest `sha256:07af43f24d96e2a4ce71670927b21eae9f7660f20a06ffee8f4a3f9f1c9bbb7e`
- Ran:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport http --http-stream --session-id http-stream-verify-20260407 --prompts "Reply with exactly ok"`

Observed streamed output included:

- `session_started`
- `system_message`
- `assistant_message` with `ok`
- `result`
- `turn_complete`

Additional verification completed on April 7, 2026:

- Built and pushed image tag `v7`
- Updated the AgentCore runtime to version `6`
- Verified the deployed runtime now points at image digest `sha256:a0b1e98616f4952375bb78f8e7bce332737c7ae88de01214828311914807dffb`
- Ran:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport http --http-stream --session-id http-stream-verify-20260407 --prompts "List the numbers 1 through 40, each on its own line, with no intro or outro text."`

Observed streamed output included:

- `session_started`
- `system_message` with `subtype=init`
- incremental assistant output `1` through `40`
- `result` with `error=False`
- `turn_complete`

Additional verification completed on April 7, 2026:

- Built and pushed image tag `v8`
- Updated the AgentCore runtime to version `7`
- Verified the deployed runtime now points at image digest `sha256:c2a2253ea1373972b444937c21d3551d1c03c0724f3e116693a99e76d74ca2bc`
- Verified `bash dev.sh logs-aws-group` resolves `/aws/bedrock-agentcore/runtimes/\${AGENT_RUNTIME_ID}-DEFAULT`
- Verified `bash dev.sh logs-aws` shows structured JSON app logs including `startup`, `ping`, `websocket_connected`, `turn_started`, and `turn_completed`
- Verified live WebSocket invoke with:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport ws --runtime-qualifier DEFAULT --session-id ws-check-v8b-20260407 --prompts "Reply with exactly ok"`

Observed result:

- `assistant> ok`
- CloudWatch logs recorded `websocket_connected`, `session_created`, `turn_started`, `turn_completed`, and `websocket_closed` for the same runtime session ID

### Ping Operation Verification

The local `sticky_session_client.py` was updated to support a dedicated `ping` operation for `GET /ping`.

Verification completed on April 7, 2026:

- `python3 -m py_compile sticky_session_client.py` passed
- Ran:

`python3 sticky_session_client.py --profile default --region us-east-1 --operation ping`

Observed result:

- The client successfully constructed and signed the AWS request
- The initial attempt failed TLS verification in the host Python `urllib` path, which was then fixed by using the same CA bundle strategy already used for WebSocket connections
- After the TLS fix, the AWS request completed and returned HTTP `404`

Current interpretation:

- The container still exposes `GET /ping` as part of the AgentCore HTTP contract
- The public signed runtime URL used for normal client traffic did not expose `GET /ping` as an invocable route for this deployed runtime, even though `/invocations` works
- The client-side `ping` operation remains useful for direct local checks via `--endpoint-url http://127.0.0.1:8080`

### Lightweight Invocation Probe Verification

The runtime was updated so a probe request can use the normal signed `/invocations` data plane path without invoking Claude.

Verification completed on April 7, 2026:

- Built and pushed image tag `v9`
- Updated the AgentCore runtime to version `8`
- Verified the deployed runtime now points at image digest `sha256:9c8a547e891cd2ebf2a83f0f7fc8dc4b2c36e8116aa8e8bc7675caf60fc3473d`
- Ran:

`bash dev.sh probe-aws`

Observed result:

- `probe> http_status=200 status=Healthy probe=True`
- The response returned immediately from `POST /invocations` without using the normal Claude turn path

Additional regression verification completed on April 7, 2026:

- Ran:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport http --http-stream --session-id http-stream-verify-20260407 --prompts "Reply with exactly ok"`

Observed result:

- The normal streamed invocation path still returned `assistant> ok`
- This confirmed the probe path is additive and does not replace normal agent invocations

### Research And Bash Configuration Update

The runtime was updated to allow Claude Code research and shell execution tools.

Verification completed on April 7, 2026:

- Built and pushed image tag `v10`
- Updated the AgentCore runtime to version `9`
- Verified the deployed runtime now points at image digest `sha256:fcef6f59a03e9242cac8fa592b442b38f2a0d3e255c4f6151cf56901d32df1b4`
- Queried CloudWatch startup logs and observed startup events with:
  - `allowed_tools=["Read","Glob","Grep","WebFetch","Bash"]`
  - `permission_mode="bypassPermissions"`
  - `sandbox={"enabled":true,"autoAllowBashIfSandboxed":true,"allowUnsandboxedCommands":false,"enableWeakerNestedSandbox":true}`

Additional live verification attempted on April 7, 2026:

- Forced a Bash turn with:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport http --http-stream --session-id bash-verify-20260407 --prompts "Use the Bash tool to run 'pwd' and reply with only the resulting path."`

- Forced a WebFetch turn with:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport http --http-stream --session-id webfetch-verify-20260407 --prompts "Use WebFetch to retrieve https://example.com and reply with only the page title."`

Observed result:

- Both turns reached the deployed runtime
- Both returned a generic Claude Code command failure with exit code `1`
- The runtime configuration is live, but actual Bash and WebFetch execution still needs a dedicated follow-up debug pass

### Sandbox Permission Fix Verification

The runtime was updated again to export `IS_SANDBOX=1` so Claude Code can use `bypassPermissions` correctly while running as root inside the container.

Verification completed on April 7, 2026:

- Built and pushed image tag `v11`
- Updated the AgentCore runtime to version `10`
- Verified the deployed runtime now points at image digest `sha256:be4f9c2b90aa04fa412b6001bd60ab9a033d833695218ab3f9bf7a469bbe2310`
- Confirmed via `aws bedrock-agentcore-control get-agent-runtime` that the runtime returned to `READY` on version `10`
- Queried CloudWatch startup logs and observed startup events with:
  - `allowed_tools=["Read","Glob","Grep","WebFetch","Bash"]`
  - `permission_mode="bypassPermissions"`
  - `sandbox={"enabled":true,"autoAllowBashIfSandboxed":true,"allowUnsandboxedCommands":false,"enableWeakerNestedSandbox":true}`
  - `subprocess_env={"IS_SANDBOX":"1"}`

Additional live verification completed on April 7, 2026:

- Forced a Bash turn with:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport http --http-stream --session-id bash-verify-20260407-v11 --prompts "Use the Bash tool to run 'pwd' and reply with only the resulting path."`

- Forced a WebFetch turn with:

`python3 sticky_session_client.py --profile default --region us-east-1 --transport http --http-stream --session-id webfetch-verify-20260407-v11 --prompts "Use WebFetch to retrieve https://example.com and reply with only the page title."`

Observed result:

- The Bash turn succeeded and returned `/app`
- The WebFetch turn succeeded and returned `Example Domain`
- This confirmed that `IS_SANDBOX=1` resolved the earlier runtime-side Claude Code tool failure

### Repo Sanitization For Public GitHub

The repo was updated to remove tracked account-specific runtime identifiers and move them into a local ignored `.env` file.

Verification completed on April 7, 2026:

- Added `.env.example` with the required runtime and deployment variables
- Added `.gitignore` entries for `.env`, `generated/`, and local cache files
- Updated `dev.sh` to load `.env` automatically and added `render-policies`
- Updated `sticky_session_client.py` to load `.env` automatically and derive its default runtime ARN from env values
- Converted `trust-policy.json` and `runtime-execution-policy.json` into env-backed templates
- Verified `bash dev.sh config` resolves values from the local `.env`
- Verified `bash dev.sh render-policies` writes concrete JSON files into `generated/`
- Verified tracked files no longer contain the previously hard-coded account ID, runtime ID, role name, or ECR repository name

### Async Background Task Support

The runtime was updated to support AgentCore-style asynchronous background tasks without changing the main synchronous invoke path.

Verification completed on April 8, 2026:

- Added reserved async control payloads on `POST /invocations` and WebSocket frames using `{"__agentcore_async__": ...}`
- Added support for async task actions `start`, `status`, and `list`
- Updated `GET /ping` to return `HealthyBusy` when the current runtime session has queued or running background tasks
- Updated `sticky_session_client.py` with `--operation async-start`, `--operation async-status`, `--operation async-list`, plus interactive `/async`, `/task`, and `/tasks`
- Updated `dev.sh` with `async-start-local`, `async-start-aws`, `async-status-local`, `async-status-aws`, `async-list-local`, and `async-list-aws`
- Verified `python3 -m py_compile app.py sticky_session_client.py`
- Verified `bash -n dev.sh`
- Verified local ASGI smoke tests with a stubbed `_run_turn(...)` show:
  - async start returns HTTP `202`
  - `GET /ping` returns `HealthyBusy` while the task is running
  - async status returns `running` during execution and `completed` with the final result afterward
  - the same async control flow works over WebSockets

Additional live verification completed on April 8, 2026:

- Built and pushed image tag `v12`
- Updated the AgentCore runtime to version `11`
- Verified the deployed runtime now points at image digest `sha256:cf2ad61809e9b24f7c1f73ad8bced291cd5b5cd6e515016b9a8b26d0c09b2c56`
- Started a live async task with:

`python3 sticky_session_client.py --profile default --region us-east-1 --runtime-arn arn:aws:bedrock-agentcore:us-east-1:339543757547:runtime/CodexDockerEcho20260406-hUwkgF2f99 --runtime-qualifier DEFAULT --session-id live-async-verify-20260408-0002-long --operation async-start --prompts "Use the Bash tool to run 'sleep 15 && printf ok' and then reply with exactly ok."`

- Observed:
  - async start returned HTTP `202`
  - immediate async status returned `running`
  - immediate async list returned `active=1`
  - later async status returned `completed`
  - final assistant result was `ok`
- Queried CloudWatch Logs for the same session and observed:
  - `async_task_queued`
  - `async_task_started`
  - `turn_started`
  - `turn_completed`
  - `async_task_completed`
- This confirmed the deployed runtime now supports the async background task flow end to end

Additional tooling follow-up completed on April 8, 2026:

- Updated `dev.sh` so shell-provided env overrides such as `LOCAL_SESSION_ID=... bash dev.sh async-start-aws ...` are no longer overwritten by `.env`
- Updated `bash dev.sh config` to print `LOCAL_SESSION_ID`

### Task-Aware Probe Verification

The signed `/invocations` probe path was updated so probe responses can report async task status without using the separate async status action.

Verification completed on April 8, 2026:

- Built and pushed image tag `v13`
- Updated the AgentCore runtime to version `12`
- Verified the deployed runtime now points at image digest `sha256:bfa298a270c3e1c4fd191de2403ec405776ed8d4c9fdaa46f88b6764c1bcf4b0`
- Ran a plain probe with `{"__agentcore_probe__": true}` against a fresh live session and observed:
  - `status=Healthy`
  - `active_async_task_count=0`
- Started a live async task in the same session and then ran a task-aware probe with `{"__agentcore_probe__":{"task_id":"..."}}`
- Observed during execution:
  - `status=HealthyBusy`
  - `task_found=true`
  - `async_task.status=running`
- Ran a completed probe with `{"__agentcore_probe__":{"task_id":"...","include_result":true,"include_tasks":true}}`
- Observed after completion:
  - `status=Healthy`
  - `async_task.status=completed`
  - the final result payload was included and the assistant response was `ok`
- This confirmed the live probe path now works as an external status check for both session health and individual async task state

## Current Usage

Run the interactive sticky-session client with:

```bash
python3 /Users/ryfeus/Code/claude-code-agentcore/sticky_session_client.py \
  --profile default \
  --region us-east-1
```

Batch mode still works by passing `--prompts`.

Build and run the runtime locally with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh build
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh run
```

Smoke-test the local container with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh smoke-local \
  "Reply with exactly ok"
```

Check the local WebSocket endpoint directly with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh ws-local \
  "Reply with exactly ok"
```

Check the deployed AgentCore WebSocket endpoint with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh ws-aws \
  "Reply with exactly ok"
```

Resolve and tail CloudWatch logs for the deployed runtime with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh logs-aws-group
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh logs-aws 10m
```

Follow CloudWatch logs continuously with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh logs-aws-follow 10m
```

Build, push, and deploy the next image version with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh release
```

Run the ping operation against AWS with:

```bash
python3 /Users/ryfeus/Code/claude-code-agentcore/sticky_session_client.py \
  --profile default \
  --region us-east-1 \
  --operation ping
```

Run the ping operation directly against a local container with:

```bash
python3 /Users/ryfeus/Code/claude-code-agentcore/sticky_session_client.py \
  --operation ping \
  --endpoint-url http://127.0.0.1:8080
```

Run the lightweight probe against AWS with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh probe-aws
```

Run the lightweight probe directly against a local container with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh probe-local
```

Start a local background task and inspect it later with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh async-start-local \
  "Research the latest Bedrock AgentCore long-running runtime guidance and summarize it"
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh async-list-local
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh async-status-local <task_id>
```

Start a deployed background task and inspect it later with:

```bash
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh async-start-aws \
  "Research the latest Bedrock AgentCore long-running runtime guidance and summarize it"
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh async-list-aws
bash /Users/ryfeus/Code/claude-code-agentcore/dev.sh async-status-aws <task_id>
```

Use WebSocket streaming mode with:

```bash
python3 /Users/ryfeus/Code/claude-code-agentcore/sticky_session_client.py \
  --profile default \
  --region us-east-1 \
  --transport ws
```

Batch WebSocket example:

```bash
python3 /Users/ryfeus/Code/claude-code-agentcore/sticky_session_client.py \
  --profile default \
  --region us-east-1 \
  --transport ws \
  --prompts "Hello" "What did I just say?"
```

## Important Implementation Notes

- The runtime default allowed tools are now `Read`, `Glob`, `Grep`, `WebFetch`, and `Bash`
- Permission mode is now `bypassPermissions`
- `app.py` now passes `IS_SANDBOX=1` into the Claude subprocess environment by default so bypass-permissions mode works inside the container
- `app.py` stores session state in process memory, so stickiness depends on repeated requests or WebSocket connections landing in the same warm runtime container and using the same AgentCore runtime session ID
- The runtime model default is currently Sonnet on Bedrock: `us.anthropic.claude-sonnet-4-6`
- `app.py` enables Claude Agent SDK partial streaming with `include_partial_messages=True` and forwards `text_delta` events as `assistant_delta`
- `app.py` now emits structured JSON logs for `startup`, `ping`, `session_created`, `session_reused`, `turn_started`, `turn_completed`, `websocket_connected`, `websocket_disconnected`, `websocket_closed`, and shutdown lifecycle events
- The client normalizes short session IDs to meet the runtime minimum length requirement
- The HTTP streaming client path now renders SSE events incrementally instead of buffering the whole response before printing
- The client now supports `--operation ping` and interactive `/ping` in HTTP mode
- The runtime now treats `{"__agentcore_probe__": ...}` as a reserved lightweight health probe payload on `POST /invocations`
- Probe responses now surface `Healthy` versus `HealthyBusy`, async task counters, and optional per-task status/result details when a probe payload includes `task_id`
- The runtime now also treats `{"__agentcore_async__": ...}` as a reserved async task control payload on `POST /invocations` and WebSocket frames
- The client now supports `--operation probe` and interactive `/probe` in HTTP mode
- The client now supports `--operation async-start`, `--operation async-status`, `--operation async-list`, plus `/async`, `/task`, and `/tasks` in interactive HTTP mode
- `bash dev.sh probe-aws` is now the public health check that uses the same signed `/invocations` surface as normal traffic
- `GET /ping` now returns `HealthyBusy` for a session while that session still has queued or running background work
- The public signed AWS `GET /ping` route still appears unavailable externally, so live async verification is currently done through async `status` / `list` calls plus CloudWatch logs rather than direct public `/ping`
- Claude Code sandboxing is now enabled in the runtime with nested-sandbox support turned on
- Forced live `Bash` and `WebFetch` turns both work on the deployed runtime after adding `IS_SANDBOX=1`
- The WebSocket client now presigns `wss://.../ws?qualifier=DEFAULT` and sends the runtime session ID in the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header, which matches the AgentCore bidirectional streaming samples
- A signed AWS `GET /ping` request against the deployed runtime returned HTTP `404` on April 7, 2026, so the public runtime URL may not expose `/ping` the same way it exposes `/invocations`
- The WebSocket handler currently accepts text frames and JSON payloads that contain `prompt`, `inputText`, `text`, or `message`
- Binary WebSocket frames are rejected
- WebSocket frame size is capped in the app at 32 KB to align with the documented AgentCore limit
- The deployed runtime's CloudWatch Logs group currently resolves to `/aws/bedrock-agentcore/runtimes/\${AGENT_RUNTIME_ID}-DEFAULT`

## Prior Session Intent

The sequence of user requests in the previous session was:

1. Create a new AWS AgentCore runtime deployment with a Docker image using the AWS CLI and the default profile.
2. Create a simple client that reuses the same session ID to check sticky session behavior.
3. Update the Docker image so it contains Claude Code and the Claude Agent SDK using Bedrock for Anthropic model access.
4. Update `sticky_session_client.py` to support an interactive session.
5. Update the runtime image to support bidirectional streaming via WebSockets.
6. Update `sticky_session_client.py` to support testing AgentCore bidirectional streaming over WebSockets.

## Likely Next Steps

- Decide whether async task metadata should survive container restarts or remain in-memory only
- Add first-class client and `dev.sh` helpers for task-aware probe payloads so external checks can query one async task without hand-crafted JSON
- Add a concrete research-oriented demo flow that showcases `WebFetch` over HTTP streaming or WebSockets
- Decide whether long-running async tasks need cancellation, task TTL cleanup, or concurrency limits per session
- Decide whether additional tools should be enabled beyond `Read`, `Glob`, `Grep`, `WebFetch`, and `Bash`
- Change the default Bedrock model if a different Claude tier is preferred for the runtime
- Add cleanup scripts for the IAM role, runtime, and ECR resources
- Add richer CloudWatch log filtering helpers to `dev.sh` if session-specific debugging becomes common
