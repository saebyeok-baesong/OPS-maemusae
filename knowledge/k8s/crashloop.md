---
service: common
category: general
doc_type: runbook
updated_at: 2026-11-24
---
# Pod 기동 실패 반복(KubePodCrashLooping)

## 증상

`KubePodCrashLooping` 알림이 뜬다. Pod가 `CrashLoopBackOff`이고 재시작 횟수가 오른다. 남은 Pod가 요청을 받거나, 모두 죽으면 ALB가 502, 503을 돌려준다.

## 판단

배포 직후에 시작됐으면 새 이미지나 설정(Secret, ConfigMap)이 먼저다. 이미지가 그대로인데 시작됐으면 RDS, Redis 같은 의존 서비스 접속 실패를 의심한다. 기동 중 예외는 대개 코드 수정 대상이 아니다.

## 조치

1. 직전 컨테이너의 종료 로그를 본다. `kubectl -n maemusae logs <pod> --previous`
2. `kubectl -n maemusae describe pod <pod>`에서 `Last State`, `Reason`, `Exit Code`, Events를 본다.
3. `Missing required env`, `ValidationError`처럼 설정 이름이 나오면 External Secrets가 만든 Secret의 키 이름을 확인한다. 값은 출력하지 않는다.
4. 배포 직후면 장애 알림의 롤백 버튼으로 직전 digest로 되돌린다. 직접 `kubectl rollout undo`를 하면 Argo CD self-heal이 다시 되돌린다.

## 확인

재시작 횟수가 멈추고 Ready가 되면 해소다. 알림은 몇 분 뒤 resolved로 바뀌고 메시지에 해소 확인이 달린다.
