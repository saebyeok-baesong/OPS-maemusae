---
service: api
category: general
doc_type: runbook
updated_at: 2026-11-24
---
# api 5xx 비율 상승(MaemusaeHighErrorRate)

## 증상

api 접근 로그(`logger=app.access`)에서 `status >= 500` 비율이 5분 동안 5%를 넘는다. 처리하지 못한 예외 알림이 같이 뜨면 Alertmanager가 이 알림을 막고 예외 알림만 보낸다.

## 판단

예외 없이 5xx만 오르면 api 밖을 먼저 본다. RDS 연결 수 고갈(`OperationalError`, `too many connections`), Redis 접속 실패, 업로드 S3 권한 오류가 흔하다. 배포 직후면 새 이미지를 의심한다.

## 조치

1. 같은 시간대 `{namespace="maemusae", container="api"} | json | status >= 500`을 경로별로 센다.
2. RDS CloudWatch에서 `DatabaseConnections`, `CPUUtilization`을 본다.
3. 배포 직후면 메시지의 롤백 버튼으로 되돌린다.
