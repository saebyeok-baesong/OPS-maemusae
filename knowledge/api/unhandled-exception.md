---
service: api
category: code_bug
doc_type: runbook
updated_at: 2026-11-24
---
# api 처리하지 못한 예외(MaemusaeUnhandledException)

## 증상

api가 500과 `{"code": "INTERNAL_ERROR", "request_id": ...}`를 돌려준다. 로그에 `logger=app.error`, `처리하지 못한 오류 <예외 이름>` 줄이 `exc_info`(스택트레이스)와 함께 남는다.

## 판단

장애 알림의 근거에 스택트레이스 마지막 줄과 `request_id`가 있다. 같은 `request_id`로 Loki에서 접근 로그(`logger=app.access`)를 찾으면 경로와 상태 코드가 나온다. 배포 직후면 새 이미지가 원인일 가능성이 높다.

## 조치

1. Grafana Explore에서 `{namespace="maemusae", container="api"} | json | request_id="<id>"`로 요청 전체 로그를 본다.
2. 배포 직후면 메시지의 롤백 버튼으로 먼저 완화하고, 원인은 SERVICE 레포에서 고친다.
3. 배포와 상관없으면 메시지의 원인 분석과 할 일을 담당 개발자(풀스택팀)에게 넘긴다.
