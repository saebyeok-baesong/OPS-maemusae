#!/usr/bin/env bash
# 보안 일일 보고(선택 시나리오): 예시 Trivy, gitleaks, kube-bench 결과를 넣고 오늘 보고를 바로 만든다.
# 결과: security DB에 결과가 쌓이고 보안 채널(또는 worker 로그)에 오늘 보고가 올라간다
. "$(dirname "$0")/../lib.sh"

for source in trivy gitleaks kube-bench; do
  printf '%s: ' "$source"
  { printf '{"target":"demo-%s","run_url":"http://localhost/demo","report":' "$source"; cat "$DEMO/security/$source.json"; printf '}'; } |
    curl -s -X POST "localhost:18000/api/v1/security/$source" -H "Authorization: Bearer demo-security-token" \
      -H "Content-Type: application/json" --data-binary @-
  echo
done

say "오늘 보고 기록을 지우고, 보고 시각을 0시(KST)로 바꿔 worker를 다시 띄웁니다"
dc exec -T ops-postgres psql -U ops -d security -qc \
  "DELETE FROM security_reports WHERE report_date = (now() AT TIME ZONE 'Asia/Seoul')::date"
SECURITY_REPORT_HOUR_KST=0 dc up -d ops-worker
wait_for "보안 보고" 180 sh -c "docker compose -p watchnyang-demo logs ops-worker | grep 'security report .* posted'"
dc logs --no-log-prefix ops-worker | grep '\[discord disabled\]' | grep -i '보안' | tail -1 | cut -c1-1500

say "보고 시각을 끈 상태로 worker를 되돌립니다"
dc up -d ops-worker
