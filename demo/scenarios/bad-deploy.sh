#!/usr/bin/env bash
# 나쁜 배포: v2를 배포하면 예외 알림 -> 롤백 버튼이 붙은 장애 메시지 -> 승인 -> v1으로 돌아가 해소된다.
# 결과: api가 v1으로 돌아오고 장애 기록에 롤백 merge와 해소 시각이 남는다. AUTO_APPROVE=1이면 묻지 않고 승인한다
. "$(dirname "$0")/../lib.sh"

V1=$(image_ref v1) V2=$(image_ref v2)
if [ "$(gitops current)" != "$V1" ]; then
  say "api가 v1이 아니라서 먼저 v1으로 맞춥니다"
  gitops deploy "$V1" "deploy(api): v1 (데모 초기화)"
  wait_for "v1 반영" 60 sh -c "[ \"\$(docker inspect -f '{{.Image}}' watchnyang-api 2>/dev/null)\" = '${V1#*@}' ] && echo ok" >/dev/null
fi
wait_quiet

say "1. CI 이미지 PR이 merge된 것처럼 api v2를 배포합니다 (images.yaml 한 줄)"
T_DEPLOY=$(now)
gitops deploy "$V2" "deploy(api): v2 (#42)"
wait_for "v2 반영" 60 sh -c "[ \"\$(docker inspect -f '{{.Image}}' watchnyang-api 2>/dev/null)\" = '${V2#*@}' ] && echo ok" >/dev/null
wait_for "v2 응답" 60 sh -c '[ "$(curl -s -o /dev/null -w %{http_code} localhost:18080/api/auth/terms)" = 500 ] && echo ok' >/dev/null
T_V2=$(now)
say "   v2가 떴습니다. GET /api/auth/terms 가 500을 냅니다"
curl -s localhost:18080/api/auth/terms; echo

say "2. 알림 -> OPS 접수 -> 분석 -> 장애 메시지를 기다립니다"
ID=$(wait_for "롤백 제안이 붙은 장애" 240 sql "SELECT id FROM incidents WHERE alertname = 'MaemusaeUnhandledException'
       AND created_at > to_timestamp($T_DEPLOY) AND result->>'rollback_pr' IS NOT NULL AND finished_at IS NOT NULL ORDER BY created_at LIMIT 1")
T_CARD=$(epoch_of finished_at "incidents WHERE id = '$ID'")
T_FIRE=$(epoch_of occurred_at "incidents WHERE id = '$ID'")
show_incident "$ID"
say "   장애 메시지 원문 (Discord가 꺼져 있으면 worker 로그)"
dc logs --no-log-prefix ops-worker | grep '\[discord disabled\]' | grep "$ID" | tail -1 | cut -c1-1500

say "3. 롤백 승인"
T_APPROVE=$(now)
approve "$ID"
sql "SELECT action, approver, status, detail FROM actions WHERE incident_id = '$ID'"

say "4. deployer가 main의 images.yaml을 보고 v1으로 되돌리기를 기다립니다"
wait_for "v1 복귀" 60 sh -c "[ \"\$(docker inspect -f '{{.Image}}' watchnyang-api 2>/dev/null)\" = '${V1#*@}' ] && echo ok" >/dev/null
wait_for "v1 응답" 60 sh -c '[ "$(curl -s -o /dev/null -w %{http_code} localhost:18080/api/auth/terms)" = 200 ] && echo ok' >/dev/null
T_V1=$(now)
echo "   GET /api/auth/terms -> 200"

say "5. 알림 해소를 기다립니다 (Loki 규칙 창 1분이 지나야 풀립니다)"
wait_for "해소" 240 sql "SELECT alert_resolved_at FROM incidents WHERE id = '$ID' AND alert_resolved_at IS NOT NULL" >/dev/null
T_RESOLVED_SEEN=$(now)
dc logs --no-log-prefix ops-worker | grep '알림 해소 확인' | tail -1 | cut -c1-400

say "걸린 시간"
cat <<T
  배포 -> v2 기동              $(secs "$T_DEPLOY" "$T_V2")
  배포 -> 알림 시작(startsAt)   $(secs "$T_DEPLOY" "$T_FIRE")
  배포 -> 장애 메시지           $(secs "$T_DEPLOY" "$T_CARD")
  승인 -> v1 기동               $(secs "$T_APPROVE" "$T_V1")
  승인 -> OPS가 해소 받음        $(secs "$T_APPROVE" "$T_RESOLVED_SEEN")
T
