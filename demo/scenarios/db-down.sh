#!/usr/bin/env bash
# DB 장애: service DB를 멈추면 준비 상태 알림 -> 런북 안내 메시지(롤백 없음) -> DB를 다시 켜면 해소된다.
# 결과: 장애 기록에 롤백 제안 없이 런북(api/not-ready.md)과 해소 시각이 남는다. AUTO_APPROVE=1이면 묻지 않고 DB를 켠다
. "$(dirname "$0")/../lib.sh"

dc start service-db >/dev/null
wait_for "api 준비" 60 curl -sf localhost:18080/health/ready >/dev/null
wait_quiet

say "1. WatchNyang DB를 멈춥니다"
T_STOP=$(now)
dc stop service-db
curl -s localhost:18080/health/ready; echo

say "2. 준비 상태 알림 -> OPS 분석 -> 장애 메시지를 기다립니다"
ID=$(wait_for "준비 상태 장애" 240 sql "SELECT id FROM incidents WHERE alertname = 'MaemusaeApiNotReady'
       AND created_at > to_timestamp($T_STOP) AND finished_at IS NOT NULL ORDER BY created_at LIMIT 1")
T_CARD=$(epoch_of finished_at "incidents WHERE id = '$ID'")
T_FIRE=$(epoch_of occurred_at "incidents WHERE id = '$ID'")
show_incident "$ID"
say "   장애 메시지 원문 (Discord가 꺼져 있으면 worker 로그)"
dc logs --no-log-prefix ops-worker | grep '\[discord disabled\]' | grep "$ID" | tail -1 | cut -c1-1500

say "3. 런북대로 DB를 다시 켭니다"
if [ "${AUTO_APPROVE:-0}" != 1 ]; then read -r -p "Enter를 누르면 DB를 켭니다 " _; fi
T_START=$(now)
dc start service-db
wait_for "api 준비" 60 curl -sf localhost:18080/health/ready >/dev/null
T_READY=$(now)

say "4. 알림 해소를 기다립니다 (준비 상태 알림은 DB가 돌아온 뒤 1분 더 떠 있습니다)"
wait_for "해소" 240 sql "SELECT alert_resolved_at FROM incidents WHERE id = '$ID' AND alert_resolved_at IS NOT NULL" >/dev/null
T_RESOLVED_SEEN=$(now)
dc logs --no-log-prefix ops-worker | grep '알림 해소 확인' | tail -1 | cut -c1-400

say "같은 시간 예외 알림은 Alertmanager가 막았습니다(inhibit). 이 시나리오에서 생긴 장애:"
sql "SELECT alertname, count(*) FROM incidents WHERE created_at > to_timestamp($T_STOP) GROUP BY alertname"

say "걸린 시간"
cat <<T
  DB 중단 -> 알림 시작(startsAt)  $(secs "$T_STOP" "$T_FIRE")
  DB 중단 -> 장애 메시지          $(secs "$T_STOP" "$T_CARD")
  DB 시작 -> api 준비             $(secs "$T_START" "$T_READY")
  DB 시작 -> OPS가 해소 받음       $(secs "$T_START" "$T_RESOLVED_SEEN")
T
