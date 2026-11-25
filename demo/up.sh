#!/usr/bin/env bash
# 데모 전체를 띄운다. 결과: api v1이 떠 있고, 알림, OPS, 대시보드가 준비된 상태. 다시 불러도 같은 상태로 맞춘다
. "$(dirname "$0")/lib.sh"

[ -f "$SERVICE_DIR/backend/Dockerfile" ] || { echo "SERVICE 레포가 없다: $SERVICE_DIR (README의 폴더 배치 참고, SERVICE_DIR로 바꿀 수 있다)" >&2; exit 1; }

say "이미지를 만듭니다 (처음에는 몇 분 걸립니다)"
docker build -q -t watchnyang-api:v1 "$SERVICE_DIR/backend" >/dev/null
docker build -q -t watchnyang-api:v2 -f "$DEMO/images/api-bad.Dockerfile" "$DEMO/images" >/dev/null
docker build -q -t watchnyang-ops:base "$DEMO/.." >/dev/null
docker build -q -t watchnyang-ops:demo -f "$DEMO/images/ops.Dockerfile" "$DEMO/images" >/dev/null
dc build -q deployer

say "로컬 gitops 저장소에 api v1을 첫 배포로 넣습니다"
dc run --rm --no-deps deployer gitops.sh init "$(image_ref v1)"

say "컨테이너를 올립니다"
dc up -d --wait --wait-timeout 300 service-db ops-postgres ops-redis loki alertmanager prometheus blackbox grafana alloy ops-api
dc up -d deployer traffic ops-worker ${DISCORD_BOT_TOKEN:+ops-approvals}
wait_for "api 준비" 120 curl -sf localhost:18080/health/ready >/dev/null
wait_for "worker 시작" 180 sh -c "docker compose -p watchnyang-demo logs ops-worker | grep 'started, knowledge'" >/dev/null

say "준비됐습니다"
cat <<INFO
  api (WatchNyang v1)   http://localhost:18080/docs
  Grafana               http://localhost:13000
  Prometheus            http://localhost:19090/alerts
  Alertmanager          http://localhost:19093
  OPS api               http://localhost:18000/healthz
  LLM                   ${LIGHT_MODEL:-fake:light} / ${HEAVY_MODEL:-fake:heavy}
  Discord               $([ -n "${DISCORD_BOT_TOKEN:-}" ] && echo "켜짐" || echo "꺼짐 (메시지는 docker compose logs ops-worker 에 찍힙니다)")
시나리오: demo/scenarios/bad-deploy.sh, demo/scenarios/db-down.sh
INFO
