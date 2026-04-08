#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"
GENERATED_CONFIG_DIR="${GENERATED_CONFIG_DIR:-${SCRIPT_DIR}/generated}"


load_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$ENV_FILE"
    set +a
  fi
}


load_env_file

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-}"

AGENT_RUNTIME_ID="${AGENT_RUNTIME_ID:-}"
ECR_REPOSITORY_NAME="${ECR_REPOSITORY_NAME:-}"
AGENT_RUNTIME_ARN="${AGENT_RUNTIME_ARN:-}"
ECR_REPOSITORY_URI="${ECR_REPOSITORY_URI:-}"
AGENT_RUNTIME_ROLE_NAME="${AGENT_RUNTIME_ROLE_NAME:-}"
WORKLOAD_IDENTITY_NAME_PREFIX="${WORKLOAD_IDENTITY_NAME_PREFIX:-}"

if [[ -z "$AGENT_RUNTIME_ARN" && -n "$AWS_ACCOUNT_ID" && -n "$AGENT_RUNTIME_ID" ]]; then
  AGENT_RUNTIME_ARN="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/${AGENT_RUNTIME_ID}"
fi

if [[ -z "$ECR_REPOSITORY_URI" && -n "$AWS_ACCOUNT_ID" && -n "$ECR_REPOSITORY_NAME" ]]; then
  ECR_REPOSITORY_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY_NAME}"
fi

LOCAL_IMAGE_NAME="${LOCAL_IMAGE_NAME:-agentcore-runtime-local}"
LOCAL_CONTAINER_NAME="${LOCAL_CONTAINER_NAME:-agentcore-runtime-local}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
LOCAL_SESSION_ID="${LOCAL_SESSION_ID:-local-dev-session-20260407-0001}"
RUNTIME_QUALIFIER="${RUNTIME_QUALIFIER:-DEFAULT}"
AWS_LOG_GROUP_NAME="${AWS_LOG_GROUP_NAME:-}"

DOCKERFILE_PATH="${DOCKERFILE_PATH:-${SCRIPT_DIR}/Dockerfile}"
DOCKER_CONTEXT="${DOCKER_CONTEXT:-${SCRIPT_DIR}}"
DOCKER_BUILD_PLATFORM="${DOCKER_BUILD_PLATFORM:-}"

WAIT_INTERVAL_SECONDS="${WAIT_INTERVAL_SECONDS:-5}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-900}"
DEFAULT_LOCAL_TAG="${DEFAULT_LOCAL_TAG:-dev}"
DEFAULT_LOCAL_PROMPT="${DEFAULT_LOCAL_PROMPT:-Reply with exactly ok}"


log() {
  printf '==> %s\n' "$*"
}


die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}


require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}


require_non_empty() {
  local name="$1"
  local value="$2"
  [[ -n "$value" ]] || die "missing required configuration: ${name}. Set it in ${ENV_FILE} or your shell environment."
}


require_aws_runtime_config() {
  require_non_empty "AWS_ACCOUNT_ID" "$AWS_ACCOUNT_ID"
  require_non_empty "AGENT_RUNTIME_ID" "$AGENT_RUNTIME_ID"
  require_non_empty "ECR_REPOSITORY_NAME" "$ECR_REPOSITORY_NAME"
}


aws_cli() {
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
}


local_image_ref() {
  local tag="${1:-$DEFAULT_LOCAL_TAG}"
  printf '%s:%s' "$LOCAL_IMAGE_NAME" "$tag"
}


remote_image_ref() {
  local tag="$1"
  printf '%s:%s' "$ECR_REPOSITORY_URI" "$tag"
}


json_prompt_payload() {
  local prompt="$1"
  python3 -c 'import json, sys; print(json.dumps({"prompt": sys.argv[1]}))' "$prompt"
}


normalize_bool() {
  local value="$1"
  local lowered
  lowered="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    true|1|yes) printf 'true\n' ;;
    false|0|no) printf 'false\n' ;;
    *) die "unsupported boolean value: $value" ;;
  esac
}


resolve_local_tag() {
  if [[ -n "${1:-}" ]]; then
    printf '%s\n' "$1"
    return
  fi
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    printf '%s\n' "$IMAGE_TAG"
    return
  fi
  printf '%s\n' "$DEFAULT_LOCAL_TAG"
}


resolve_release_tag() {
  if [[ -n "${1:-}" ]]; then
    printf '%s\n' "$1"
    return
  fi
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    printf '%s\n' "$IMAGE_TAG"
    return
  fi
  next_tag
}


resolve_required_tag() {
  if [[ -n "${1:-}" ]]; then
    printf '%s\n' "$1"
    return
  fi
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    printf '%s\n' "$IMAGE_TAG"
    return
  fi
  die "tag required. Pass one explicitly or set IMAGE_TAG."
}


next_tag() {
  require_cmd aws
  require_aws_runtime_config

  local tags max tag number
  max=0
  tags="$(aws_cli ecr describe-images \
    --repository-name "$ECR_REPOSITORY_NAME" \
    --query 'imageDetails[].imageTags[]' \
    --output text)"

  for tag in $tags; do
    if [[ "$tag" == "None" ]]; then
      continue
    fi
    if [[ "$tag" =~ ^v([0-9]+)$ ]]; then
      number="${BASH_REMATCH[1]}"
      if (( number > max )); then
        max="$number"
      fi
    fi
  done

  printf 'v%s\n' "$((max + 1))"
}


docker_image_must_exist() {
  local image_ref="$1"
  docker image inspect "$image_ref" >/dev/null 2>&1 || die "docker image not found: $image_ref"
}


current_runtime_value() {
  require_aws_runtime_config
  local query="$1"
  aws_cli bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$AGENT_RUNTIME_ID" \
    --query "$query" \
    --output text
}


current_runtime_status() {
  current_runtime_value 'status'
}


current_runtime_version() {
  current_runtime_value 'agentRuntimeVersion'
}


current_runtime_container_uri() {
  current_runtime_value 'agentRuntimeArtifact.containerConfiguration.containerUri'
}


current_runtime_description() {
  current_runtime_value 'description'
}


current_runtime_role_arn() {
  current_runtime_value 'roleArn'
}


current_runtime_protocol() {
  current_runtime_value 'protocolConfiguration.serverProtocol'
}


current_runtime_idle_timeout() {
  current_runtime_value 'lifecycleConfiguration.idleRuntimeSessionTimeout'
}


current_runtime_max_lifetime() {
  current_runtime_value 'lifecycleConfiguration.maxLifetime'
}


current_runtime_require_mmdsv2() {
  current_runtime_value 'metadataConfiguration.requireMMDSV2'
}


resolve_log_group_name() {
  require_aws_runtime_config
  if [[ -n "$AWS_LOG_GROUP_NAME" ]]; then
    printf '%s\n' "$AWS_LOG_GROUP_NAME"
    return
  fi

  local exact_name candidate
  exact_name="/aws/bedrock-agentcore/runtimes/${AGENT_RUNTIME_ID}"
  candidate="$(aws_cli logs describe-log-groups \
    --log-group-name-prefix "$exact_name" \
    --query 'logGroups[0].logGroupName' \
    --output text)"

  if [[ -n "$candidate" && "$candidate" != "None" ]]; then
    printf '%s\n' "$candidate"
    return
  fi

  candidate="$(aws_cli logs describe-log-groups \
    --log-group-name-prefix '/aws/bedrock-agentcore/runtimes/' \
    --query "logGroups[?contains(logGroupName, \`${AGENT_RUNTIME_ID}\`)].logGroupName | [0]" \
    --output text)"

  if [[ -n "$candidate" && "$candidate" != "None" ]]; then
    printf '%s\n' "$candidate"
    return
  fi

  die "could not resolve CloudWatch log group for runtime ${AGENT_RUNTIME_ID}; set AWS_LOG_GROUP_NAME explicitly"
}


join_quoted_csv() {
  local first=1
  local item
  for item in "$@"; do
    if (( first )); then
      printf '"%s"' "$item"
      first=0
    else
      printf ',"%s"' "$item"
    fi
  done
}


current_runtime_network_configuration() {
  local mode
  mode="$(current_runtime_value 'networkConfiguration.networkMode')"

  if [[ "$mode" == "PUBLIC" ]]; then
    printf 'networkMode=PUBLIC\n'
    return
  fi

  if [[ "$mode" != "VPC" ]]; then
    die "unsupported network mode: $mode"
  fi

  local sg_text subnet_text
  local -a security_groups=() subnets=()

  sg_text="$(current_runtime_value 'networkConfiguration.networkModeConfig.securityGroups')"
  subnet_text="$(current_runtime_value 'networkConfiguration.networkModeConfig.subnets')"

  read -r -a security_groups <<< "$sg_text"
  read -r -a subnets <<< "$subnet_text"

  if (( ${#security_groups[@]} == 0 || ${#subnets[@]} == 0 )); then
    die "VPC runtime is missing subnet or security group configuration"
  fi

  printf 'networkMode=VPC,networkModeConfig={securityGroups=[%s],subnets=[%s]}\n' \
    "$(join_quoted_csv "${security_groups[@]}")" \
    "$(join_quoted_csv "${subnets[@]}")"
}


wait_until_runtime_ready() {
  local deadline status version container_uri
  deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))

  while (( SECONDS <= deadline )); do
    status="$(current_runtime_status)"
    version="$(current_runtime_version)"
    container_uri="$(current_runtime_container_uri)"

    log "runtime status=${status} version=${version} container=${container_uri}"

    case "$status" in
      READY)
        return
        ;;
      UPDATE_FAILED|CREATE_FAILED)
        die "runtime entered terminal failure state: $status"
        ;;
    esac

    sleep "$WAIT_INTERVAL_SECONDS"
  done

  die "timed out waiting for runtime ${AGENT_RUNTIME_ID} to become READY"
}


show_config() {
  cat <<EOF
ENV_FILE=${ENV_FILE}
AWS_PROFILE=${AWS_PROFILE}
AWS_REGION=${AWS_REGION}
AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID:-<unset>}
AGENT_RUNTIME_ID=${AGENT_RUNTIME_ID:-<unset>}
AGENT_RUNTIME_ARN=${AGENT_RUNTIME_ARN:-<unset>}
AGENT_RUNTIME_ROLE_NAME=${AGENT_RUNTIME_ROLE_NAME:-<unset>}
ECR_REPOSITORY_NAME=${ECR_REPOSITORY_NAME:-<unset>}
ECR_REPOSITORY_URI=${ECR_REPOSITORY_URI:-<unset>}
WORKLOAD_IDENTITY_NAME_PREFIX=${WORKLOAD_IDENTITY_NAME_PREFIX:-<unset>}
LOCAL_IMAGE_NAME=${LOCAL_IMAGE_NAME}
LOCAL_CONTAINER_NAME=${LOCAL_CONTAINER_NAME}
LOCAL_PORT=${LOCAL_PORT}
DOCKERFILE_PATH=${DOCKERFILE_PATH}
DOCKER_CONTEXT=${DOCKER_CONTEXT}
DOCKER_BUILD_PLATFORM=${DOCKER_BUILD_PLATFORM:-<default>}
WAIT_TIMEOUT_SECONDS=${WAIT_TIMEOUT_SECONDS}
RUNTIME_QUALIFIER=${RUNTIME_QUALIFIER}
AWS_LOG_GROUP_NAME=${AWS_LOG_GROUP_NAME:-<auto>}
GENERATED_CONFIG_DIR=${GENERATED_CONFIG_DIR}
EOF
}


render_template_file() {
  local input_path="$1"
  local output_path="$2"

  python3 - "$input_path" "$output_path" <<'PY'
import os
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1]).read_text()
missing = []

def replace(match):
    key = match.group(1)
    value = os.environ.get(key)
    if value is None or value == "":
        missing.append(key)
        return match.group(0)
    return value

rendered = re.sub(r"\$\{([A-Z0-9_]+)\}", replace, source)
if missing:
    keys = ", ".join(sorted(set(missing)))
    raise SystemExit(f"missing env vars for template rendering: {keys}")

output_path = pathlib.Path(sys.argv[2])
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(rendered)
PY
}


render_policies() {
  local output_dir="${1:-$GENERATED_CONFIG_DIR}"

  require_non_empty "AWS_ACCOUNT_ID" "$AWS_ACCOUNT_ID"
  require_non_empty "AWS_REGION" "$AWS_REGION"
  require_non_empty "ECR_REPOSITORY_NAME" "$ECR_REPOSITORY_NAME"
  require_non_empty "WORKLOAD_IDENTITY_NAME_PREFIX" "$WORKLOAD_IDENTITY_NAME_PREFIX"

  mkdir -p "$output_dir"
  render_template_file "${SCRIPT_DIR}/trust-policy.json" "${output_dir}/trust-policy.json"
  render_template_file "${SCRIPT_DIR}/runtime-execution-policy.json" "${output_dir}/runtime-execution-policy.json"

  log "rendered IAM policy templates into ${output_dir}"
}


build_image() {
  require_cmd docker

  local tag image_ref
  tag="$(resolve_local_tag "${1:-}")"
  image_ref="$(local_image_ref "$tag")"

  log "building ${image_ref}"

  local -a args
  args=(docker build -t "$image_ref" -f "$DOCKERFILE_PATH")
  if [[ -n "$DOCKER_BUILD_PLATFORM" ]]; then
    args+=(--platform "$DOCKER_BUILD_PLATFORM")
  fi
  args+=("$DOCKER_CONTEXT")

  "${args[@]}"

  log "built ${image_ref}"
}


run_local() {
  require_cmd docker

  local tag image_ref
  tag="$(resolve_local_tag "${1:-}")"
  image_ref="$(local_image_ref "$tag")"

  docker_image_must_exist "$image_ref"

  log "restarting local container ${LOCAL_CONTAINER_NAME}"
  docker rm -f "$LOCAL_CONTAINER_NAME" >/dev/null 2>&1 || true

  local -a args
  args=(
    docker run
    --detach
    --name "$LOCAL_CONTAINER_NAME"
    --publish "${LOCAL_PORT}:8080"
    --env "AWS_REGION=${AWS_REGION}"
    --env "AWS_DEFAULT_REGION=${AWS_REGION}"
    --env "AWS_PROFILE=${AWS_PROFILE}"
    --env "AWS_SDK_LOAD_CONFIG=1"
  )

  if [[ -n "${AWS_ACCESS_KEY_ID:-}" ]]; then
    args+=(--env "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}")
  fi
  if [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    args+=(--env "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}")
  fi
  if [[ -n "${AWS_SESSION_TOKEN:-}" ]]; then
    args+=(--env "AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN}")
  fi
  if [[ -n "${AWS_CA_BUNDLE:-}" ]]; then
    args+=(--env "AWS_CA_BUNDLE=${AWS_CA_BUNDLE}")
  fi
  if [[ -d "${HOME}/.aws" ]]; then
    args+=(--volume "${HOME}/.aws:/root/.aws:ro")
  fi

  args+=("$image_ref")

  "${args[@]}" >/dev/null

  log "local runtime listening on http://127.0.0.1:${LOCAL_PORT}"
}


stop_local() {
  require_cmd docker
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$LOCAL_CONTAINER_NAME"; then
    log "stopping ${LOCAL_CONTAINER_NAME}"
    docker rm -f "$LOCAL_CONTAINER_NAME" >/dev/null
    return
  fi
  log "container ${LOCAL_CONTAINER_NAME} is not running"
}


logs_local() {
  require_cmd docker
  docker logs -f "$LOCAL_CONTAINER_NAME"
}


logs_aws_group() {
  require_cmd aws
  resolve_log_group_name
}


logs_aws_streams() {
  require_cmd aws
  local group_name
  group_name="$(resolve_log_group_name)"
  aws_cli logs describe-log-streams \
    --log-group-name "$group_name" \
    --order-by LastEventTime \
    --descending \
    --output table
}


logs_aws() {
  require_cmd aws
  local group_name since
  group_name="$(resolve_log_group_name)"
  since="${1:-10m}"
  aws_cli logs tail "$group_name" --since "$since"
}


logs_aws_follow() {
  require_cmd aws
  local group_name since
  group_name="$(resolve_log_group_name)"
  since="${1:-10m}"
  aws_cli logs tail "$group_name" --since "$since" --follow
}


shell_local() {
  require_cmd docker
  docker exec -it "$LOCAL_CONTAINER_NAME" /bin/bash
}


ping_local() {
  require_cmd curl
  log "GET http://127.0.0.1:${LOCAL_PORT}/ping"
  curl -fsS "http://127.0.0.1:${LOCAL_PORT}/ping"
  printf '\n'
}


ping_aws() {
  require_cmd python3
  python3 "${SCRIPT_DIR}/sticky_session_client.py" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --runtime-arn "$AGENT_RUNTIME_ARN" \
    --session-id "$LOCAL_SESSION_ID" \
    --operation ping
}


probe_local() {
  require_cmd python3
  python3 "${SCRIPT_DIR}/sticky_session_client.py" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --runtime-arn "$AGENT_RUNTIME_ARN" \
    --runtime-qualifier "$RUNTIME_QUALIFIER" \
    --session-id "$LOCAL_SESSION_ID" \
    --operation probe \
    --endpoint-url "http://127.0.0.1:${LOCAL_PORT}"
}


probe_aws() {
  require_cmd python3
  python3 "${SCRIPT_DIR}/sticky_session_client.py" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --runtime-arn "$AGENT_RUNTIME_ARN" \
    --runtime-qualifier "$RUNTIME_QUALIFIER" \
    --session-id "$LOCAL_SESSION_ID" \
    --operation probe
}


invoke_local() {
  require_cmd curl
  require_cmd python3

  local prompt="${1:-$DEFAULT_LOCAL_PROMPT}"
  local payload
  payload="$(json_prompt_payload "$prompt")"

  log "POST http://127.0.0.1:${LOCAL_PORT}/invocations?stream=1"
  curl -fsS -N \
    -H 'Content-Type: application/json' \
    -H 'Accept: text/event-stream' \
    -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: ${LOCAL_SESSION_ID}" \
    --data "$payload" \
    "http://127.0.0.1:${LOCAL_PORT}/invocations?stream=1"
  printf '\n'
}


smoke_local() {
  ping_local
  invoke_local "${1:-$DEFAULT_LOCAL_PROMPT}"
  ws_local_check "${1:-$DEFAULT_LOCAL_PROMPT}"
}


ws_local_check() {
  require_cmd python3
  python3 "${SCRIPT_DIR}/sticky_session_client.py" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --transport ws \
    --ws-url "ws://127.0.0.1:${LOCAL_PORT}/ws" \
    --session-id "$LOCAL_SESSION_ID" \
    --prompts "${1:-$DEFAULT_LOCAL_PROMPT}"
}


ws_aws_check() {
  require_cmd python3
  python3 "${SCRIPT_DIR}/sticky_session_client.py" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --transport ws \
    --runtime-arn "$AGENT_RUNTIME_ARN" \
    --runtime-qualifier "$RUNTIME_QUALIFIER" \
    --session-id "$LOCAL_SESSION_ID" \
    --prompts "${1:-$DEFAULT_LOCAL_PROMPT}"
}


ecr_login() {
  require_cmd aws
  require_cmd docker
  require_aws_runtime_config

  log "logging into ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  aws_cli ecr get-login-password | docker login \
    --username AWS \
    --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
}


image_digest_for_tag() {
  require_aws_runtime_config
  local tag="$1"
  aws_cli ecr describe-images \
    --repository-name "$ECR_REPOSITORY_NAME" \
    --image-ids "imageTag=${tag}" \
    --query 'imageDetails[0].imageDigest' \
    --output text
}


push_image() {
  require_cmd docker
  require_cmd aws

  local tag local_ref remote_ref digest
  tag="$(resolve_required_tag "${1:-}")"
  local_ref="$(local_image_ref "$tag")"
  remote_ref="$(remote_image_ref "$tag")"

  docker_image_must_exist "$local_ref"
  ecr_login

  log "tagging ${local_ref} as ${remote_ref}"
  docker tag "$local_ref" "$remote_ref"

  log "pushing ${remote_ref}"
  docker push "$remote_ref"

  digest="$(image_digest_for_tag "$tag")"
  log "pushed digest ${digest}"
}


deploy_image() {
  require_cmd aws

  local tag digest pinned_image_ref role_arn network_configuration
  local protocol idle_timeout max_lifetime require_mmdsv2 description

  tag="$(resolve_required_tag "${1:-}")"
  digest="$(image_digest_for_tag "$tag")"
  [[ -n "$digest" && "$digest" != "None" ]] || die "could not resolve image digest for tag ${tag}"

  pinned_image_ref="${ECR_REPOSITORY_URI}@${digest}"
  role_arn="$(current_runtime_role_arn)"
  network_configuration="$(current_runtime_network_configuration)"
  protocol="$(current_runtime_protocol)"
  idle_timeout="$(current_runtime_idle_timeout)"
  max_lifetime="$(current_runtime_max_lifetime)"
  require_mmdsv2="$(normalize_bool "$(current_runtime_require_mmdsv2)")"
  description="$(current_runtime_description)"

  log "deploying ${pinned_image_ref} to ${AGENT_RUNTIME_ID}"

  local -a args
  args=(
    aws_cli
    bedrock-agentcore-control
    update-agent-runtime
    --agent-runtime-id "$AGENT_RUNTIME_ID"
    --agent-runtime-artifact "containerConfiguration={containerUri=${pinned_image_ref}}"
    --role-arn "$role_arn"
    --network-configuration "$network_configuration"
    --protocol-configuration "serverProtocol=${protocol}"
    --lifecycle-configuration "idleRuntimeSessionTimeout=${idle_timeout},maxLifetime=${max_lifetime}"
    --metadata-configuration "requireMMDSV2=${require_mmdsv2}"
  )

  if [[ -n "$description" && "$description" != "None" ]]; then
    args+=(--description "$description")
  fi

  "${args[@]}"

  wait_until_runtime_ready
}


release_image() {
  local tag
  tag="$(resolve_release_tag "${1:-}")"

  build_image "$tag"
  push_image "$tag"
  deploy_image "$tag"
}


show_status() {
  require_cmd aws

  local status version container_uri
  status="$(current_runtime_status)"
  version="$(current_runtime_version)"
  container_uri="$(current_runtime_container_uri)"

  printf 'runtime_id=%s\n' "$AGENT_RUNTIME_ID"
  printf 'runtime_arn=%s\n' "$AGENT_RUNTIME_ARN"
  printf 'runtime_status=%s\n' "$status"
  printf 'runtime_version=%s\n' "$version"
  printf 'runtime_container_uri=%s\n' "$container_uri"

  if command -v docker >/dev/null 2>&1; then
    local container_state
    container_state="$(docker ps -a \
      --filter "name=^/${LOCAL_CONTAINER_NAME}$" \
      --format '{{.Status}}' | head -n 1 || true)"
    if [[ -n "$container_state" ]]; then
      printf 'local_container=%s\n' "$container_state"
    else
      printf 'local_container=%s\n' "not created"
    fi
  fi
}


usage() {
  cat <<EOF
Usage:
  ./dev.sh <command> [arg]

Commands:
  help                  Show this help
  config                Print resolved configuration
  render-policies [dir] Render IAM policy templates into a concrete output directory
  next-tag              Print the next ECR version tag
  build [tag]           Build the local Docker image (default tag: ${DEFAULT_LOCAL_TAG})
  run [tag]             Run the local container on port ${LOCAL_PORT}
  stop                  Stop and remove the local container
  logs                  Follow local container logs
  logs-aws-group        Print the resolved CloudWatch log group for the runtime
  logs-aws-streams      List CloudWatch log streams for the runtime
  logs-aws [since]      Print recent CloudWatch logs (default: 10m)
  logs-aws-follow [since]
                        Follow CloudWatch logs (default: 10m)
  shell                 Open a shell in the running container
  ping-local            Call GET /ping on the local container
  ping-aws              Call the deployed runtime ping path through sticky_session_client.py
  probe-local           Call the lightweight local POST /invocations probe
  probe-aws             Call the lightweight signed AWS POST /invocations probe
  invoke-local [prompt] Call local POST /invocations with SSE enabled
  ws-local [prompt]     Call the local WebSocket endpoint directly
  ws-aws [prompt]       Call the deployed AgentCore WebSocket endpoint
  smoke-local [prompt]  Run ping-local, invoke-local, and ws-local
  ecr-login             Authenticate Docker to Amazon ECR
  push <tag>            Push a tagged local image to ECR
  deploy <tag>          Update the AgentCore runtime to an ECR image tag
  release [tag]         Build, push, deploy, and wait for READY
  status                Print remote runtime status and local container state

Environment overrides:
  AWS_PROFILE AWS_REGION AWS_ACCOUNT_ID AGENT_RUNTIME_ID AGENT_RUNTIME_ARN
  AGENT_RUNTIME_ROLE_NAME WORKLOAD_IDENTITY_NAME_PREFIX
  ECR_REPOSITORY_NAME ECR_REPOSITORY_URI IMAGE_TAG LOCAL_IMAGE_NAME
  LOCAL_CONTAINER_NAME LOCAL_PORT LOCAL_SESSION_ID DOCKER_BUILD_PLATFORM
  WAIT_INTERVAL_SECONDS WAIT_TIMEOUT_SECONDS RUNTIME_QUALIFIER
  AWS_LOG_GROUP_NAME ENV_FILE GENERATED_CONFIG_DIR

Examples:
  ./dev.sh render-policies
  ./dev.sh build
  ./dev.sh run
  ./dev.sh smoke-local "Reply with exactly ok"
  ./dev.sh release
  IMAGE_TAG=v8 ./dev.sh deploy
EOF
}


main() {
  local command="${1:-help}"
  shift || true

  case "$command" in
    help|-h|--help)
      usage
      ;;
    config)
      show_config
      ;;
    render-policies)
      render_policies "${1:-}"
      ;;
    next-tag)
      next_tag
      ;;
    build)
      build_image "${1:-}"
      ;;
    run)
      run_local "${1:-}"
      ;;
    stop)
      stop_local
      ;;
    logs)
      logs_local
      ;;
    logs-aws-group)
      logs_aws_group
      ;;
    logs-aws-streams)
      logs_aws_streams
      ;;
    logs-aws)
      logs_aws "${1:-}"
      ;;
    logs-aws-follow)
      logs_aws_follow "${1:-}"
      ;;
    shell)
      shell_local
      ;;
    ping-local)
      ping_local
      ;;
    ping-aws)
      ping_aws
      ;;
    probe-local)
      probe_local
      ;;
    probe-aws)
      probe_aws
      ;;
    invoke-local)
      invoke_local "${1:-}"
      ;;
    ws-local)
      ws_local_check "${1:-}"
      ;;
    ws-aws)
      ws_aws_check "${1:-}"
      ;;
    smoke-local)
      smoke_local "${1:-}"
      ;;
    ecr-login)
      ecr_login
      ;;
    push)
      push_image "${1:-}"
      ;;
    deploy)
      deploy_image "${1:-}"
      ;;
    release)
      release_image "${1:-}"
      ;;
    status)
      show_status
      ;;
    *)
      usage
      die "unknown command: $command"
      ;;
  esac
}


main "$@"
