---
service: common
category: general
doc_type: runbook
updated_at: 2026-11-24
---
# 배포 롤백

## 언제

배포 직후(30분 안) 시작된 장애는 원인 분석보다 롤백을 먼저 한다. 장애 알림에 롤백 버튼이 붙는 조건이 이것이다.

## 절차

1. 장애 대응 봇이 INFRA 레포에 `gitops/apps/images.yaml`의 그 앱 줄을 직전 digest로 되돌리는 PR을 연다.
2. 인프라팀이 Discord 메시지의 롤백 승인 버튼을 누르면 봇이 그 PR을 merge한다. 제안 뒤 이미지나 PR이 바뀌었으면 merge하지 않는다.
3. Argo CD가 sync한다. self-heal은 켠 채로 둔다. `kubectl`로 직접 되돌리지 않는다.
4. 알림이 해소되면 메시지에 해소 확인과 걸린 시간이 달린다.

봇이 되돌린 뒤 같은 앱에서 또 장애가 나면 봇은 다시 롤백을 제안하지 않는다. 사람이 판단한다.
