---
service: api
category: dependency
doc_type: runbook
updated_at: 2026-11-25
---
# api 준비 상태 실패(MaemusaeApiNotReady)

## 증상

`/health/ready`가 503을 돌려준다. 이 확인은 DB에 `SELECT 1`을 보내므로 대개 데이터베이스 연결 실패다. 같은 시간 api 요청도 `OperationalError`로 500이 나고, Alertmanager는 이 알림이 떠 있는 동안 같은 앱의 예외와 5xx 알림을 막는다.

## 판단

코드 문제가 아니라 의존성 문제로 먼저 본다. 이미지를 되돌려도 낫지 않으므로 롤백하지 않는다. DB 인스턴스가 내려갔는지, 연결 수가 찼는지, 보안 그룹이나 비밀번호가 바뀌었는지를 본다.

## 조치

1. RDS 콘솔에서 인스턴스 상태와 최근 이벤트(재시작, 장애 조치)를 본다. 로컬 데모에서는 `docker compose ps service-db`.
2. CloudWatch `DatabaseConnections`가 한도에 붙어 있으면 api Pod 수와 연결 풀 크기를 줄인다.
3. DB가 돌아오면 api는 다시 연결한다(`pool_pre_ping`). 재시작은 필요 없다. 알림이 해소되는지 본다.
