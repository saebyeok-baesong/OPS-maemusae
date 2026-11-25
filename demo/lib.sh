# demo/의 스크립트가 같이 쓰는 함수. source로 읽는다
set -euo pipefail

DEMO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="${SERVICE_DIR:-$DEMO/../../SERVICE-watch-nyang}"

if [ -f "$DEMO/.env" ]; then
  set -a; . "$DEMO/.env"; set +a
fi
# 키가 있으면 실제 OpenAI, 없으면 가짜 LLM. 모델을 직접 적었으면 그 값을 쓴다
if [ -n "${OPENAI_API_KEY:-}" ]; then
  export LIGHT_MODEL="${LIGHT_MODEL:-openai:gpt-4.1-mini}" HEAVY_MODEL="${HEAVY_MODEL:-openai:gpt-4.1}"
fi
if [ -n "${DISCORD_BOT_TOKEN:-}" ]; then
  export COMPOSE_PROFILES=discord
fi

dc() { docker compose -p watchnyang-demo -f "$DEMO/compose.yaml" "$@"; }
say() { printf '\n\033[1m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
now() { date +%s; }

image_ref() { echo "watchnyang-api@$(docker image inspect -f '{{.Id}}' "watchnyang-api:$1")"; }
running_image() { docker inspect -f '{{.Image}}' watchnyang-api 2>/dev/null || true; }
gitops() { dc exec -T deployer gitops.sh "$@"; }
sql() { dc exec -T ops-postgres psql -U ops -d incident -tAq -F ' | ' -c "$1"; }

# wait_for <설명> <초> <명령...>: 명령이 빈 문자열이 아닌 값을 내면 그 값을 찍고 끝난다
wait_for() {
  local what="$1" limit="$2"; shift 2
  local start out; start=$(now)
  while [ $(( $(now) - start )) -lt "$limit" ]; do
    out="$("$@" 2>/dev/null || true)"
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 1
  done
  echo "시간 초과(${limit}s): $what" >&2
  return 1
}

# 앞 시나리오의 알림이 남아 있으면 새 장애가 그 알림에 묶인다. 조용해질 때까지 기다린다
wait_quiet() {
  say "앞선 알림이 모두 해소될 때까지 기다립니다"
  wait_for "알림 해소" 240 sh -c '[ -z "$(curl -s "localhost:19093/api/v2/alerts?active=true" | grep -o alertname)" ] && echo quiet' >/dev/null
}

# 장애 하나의 요약. 카드 원문은 ops-worker 로그의 [discord disabled] 줄에 있다
show_incident() {
  dc exec -T ops-postgres psql -U ops -d incident -xq -c "SELECT id, alertname, status, category, needs_code_fix,
    occurred_at, created_at AS accepted_at, finished_at AS card_at, result->'rollback_pr'->>'url' AS rollback_pr,
    result->>'context_doc_ids' AS runbooks FROM incidents WHERE id = '$1'"
  echo "--- 보고 ---"
  sql "SELECT report FROM incidents WHERE id = '$1'"
}

# 롤백 승인. AUTO_APPROVE=1이면 바로, Discord 봇이 있으면 버튼을 기다리고, 아니면 Enter를 기다려 CLI로 승인한다
approve() {
  local id="$1"
  if [ "${AUTO_APPROVE:-0}" != 1 ] && [ -n "${DISCORD_BOT_TOKEN:-}" ]; then
    say "Discord 메시지의 롤백 승인 버튼을 누르세요(승인 역할 필요)"
    wait_for "Discord 승인" 600 sql "SELECT status FROM actions WHERE incident_id = '$id' AND finished_at IS NOT NULL" >/dev/null
    return
  fi
  if [ "${AUTO_APPROVE:-0}" != 1 ]; then
    read -r -p "Enter를 누르면 롤백을 승인합니다(Discord 버튼 대신 CLI) " _
  fi
  dc exec -T ops-api python -m app.approvals approve "$id" --by "${APPROVER:-demo-presenter}"
}

# 시각(UTC epoch) 사이 초. 빈 값이면 -
secs() { if [ -n "$1" ] && [ -n "$2" ]; then echo "$(( $2 - $1 ))s"; else echo "-"; fi; }
epoch_of() { sql "SELECT floor(extract(epoch FROM $1))::bigint FROM $2"; }
