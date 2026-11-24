---
service: common
category: infra
doc_type: runbook
updated_at: 2026-11-24
---
# 메모리 초과 종료(KubePodOOMKilled)

## 증상

컨테이너가 `OOMKilled`(Exit Code 137)로 종료되고 다시 뜬다. 처리 중이던 요청은 실패한다.

## 판단

한 번이면 순간 부하일 수 있다. 반복되면 메모리 한도가 작거나, 큰 업로드 문서나 목록 조회처럼 한 요청이 메모리를 많이 쓰는 경로가 있다. 배포 직후부터 반복되면 새 코드의 메모리 사용을 의심한다.

## 조치

1. `kubectl -n maemusae top pod`로 지금 사용량을 본다.
2. Grafana에서 `container_memory_working_set_bytes`를 limit과 비교한다.
3. 한도가 작으면 INFRA 레포 `gitops/`의 values에서 `resources.limits.memory`를 올리는 PR을 연다.
4. 특정 요청이 원인이면 같은 시각 api 로그의 `path`와 `request_id`로 요청을 찾아 담당 개발자에게 넘긴다.
