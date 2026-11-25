# 감지와 전달 규칙

이 문서는 INFRA 레포 `gitops/`에 넣을 알림 규칙과 Alertmanager 설정의 기준입니다. OPS는 여기 적은 알림 이름과 라벨을 `app/alerts.py`의 `ALERT_KINDS` 표로 읽습니다. 이름이나 라벨을 바꾸면 그 표도 같이 바꿉니다.

## 라벨 계약

| 라벨 | 값 | OPS가 쓰는 곳 |
| --- | --- | --- |
| `alertname` | 아래 표의 이름 | 알림 종류(근거 수집, 롤백 여부) |
| `namespace` | `maemusae` | Loki 조회, Pod 이벤트 조회 |
| `app` | `images.yaml`의 앱 이름(`web`, `api`) | 대상 앱, 배포 이력, 롤백 |
| `container` | 컨테이너 이름. Deployment의 컨테이너 이름을 앱 이름과 같게 둔다 | Pod 알림의 대상 앱, Loki 조회 |
| `pod` | Pod 이름 | Pod 로그와 이벤트 |
| `severity` | `critical`, `warning` | 메시지 표시 |

## 알림

| 이름 | 원천 | 조건 | OPS가 붙이는 근거 | 롤백 제안 |
| --- | --- | --- | --- | --- |
| `MaemusaeUnhandledException` | Loki ruler | api의 `logger="app.error"` 줄이 5분에 1건 이상 | 최근 `exc_info`, `request_id` | 배포 30분 안이면 |
| `MaemusaeHighErrorRate` | Loki ruler | api 접근 로그의 5xx 비율이 5분 동안 5% 초과 | 최근 `exc_info`가 있으면 | 배포 30분 안이면 |
| `KubePodCrashLooping` | kube-prometheus-stack 기본 규칙 | CrashLoopBackOff | Pod 마지막 로그, Pod 이벤트 | 배포 30분 안이면 |
| `KubePodOOMKilled` | 직접 추가 | 마지막 종료 사유가 OOMKilled이고 재시작이 늘었다 | Pod 마지막 로그, Pod 이벤트 | 없음 |
| `MaemusaeApiNotReady` | 직접 추가 | api `/health/ready`(DB `SELECT 1`) 탐침 실패 | 최근 `exc_info`가 있으면 | 없음. 이미지를 되돌려도 낫지 않는다 |

WatchNyang api는 Prometheus 지표를 내보내지 않습니다. 5xx 비율과 예외는 `backend/app/core/logging.py`의 JSON 로그(`logger`, `status`, `request_id`, `exc_info`)를 그대로 Loki 규칙으로 셉니다. web(Next.js)은 JSON 접근 로그가 없어서 1차 범위에서는 Pod 알림만 받습니다.

### Loki 규칙

```yaml
groups:
  - name: maemusae-api
    rules:
      - alert: MaemusaeUnhandledException
        expr: |
          sum by (namespace, container) (
            count_over_time({namespace="maemusae", container="api"} | json | logger="app.error" [5m])
          ) > 0
        labels:
          severity: critical
          app: api
        annotations:
          summary: "api에서 처리하지 못한 예외가 5분 동안 {{ $value }}건 났습니다"
      - alert: MaemusaeHighErrorRate
        expr: |
          sum by (namespace, container) (count_over_time({namespace="maemusae", container="api"} | json | logger="app.access" | status >= 500 [5m]))
          /
          sum by (namespace, container) (count_over_time({namespace="maemusae", container="api"} | json | logger="app.access" [5m]))
          > 0.05
        for: 2m
        labels:
          severity: warning
          app: api
        annotations:
          summary: "api 5xx 비율이 5분 동안 {{ $value | humanizePercentage }}입니다"
```

### Prometheus 규칙

```yaml
- alert: KubePodOOMKilled
  expr: |
    increase(kube_pod_container_status_restarts_total{namespace="maemusae"}[10m]) > 0
    and on (namespace, pod, container)
    kube_pod_container_status_last_terminated_reason{namespace="maemusae", reason="OOMKilled"} == 1
  labels:
    severity: warning
  annotations:
    summary: "{{ $labels.container }} 컨테이너가 메모리 한도를 넘어 종료됐습니다"
```

`KubePodCrashLooping`은 kube-prometheus-stack 기본 규칙을 씁니다.

`MaemusaeApiNotReady`는 blackbox exporter로 api Service의 `/health/ready`를 탐침합니다. 라벨 `namespace`, `app`, `container`는 탐침 대상에 붙입니다. 롤링 업데이트 중에는 준비된 Pod가 Service 뒤에 남아 있어 탐침이 실패하지 않습니다. `for`는 아래 inhibit이 듣도록 `group_wait`(30s)보다 짧게 둡니다. 로컬 데모(`demo/config/prometheus-rules.yml`)는 10s입니다.

```yaml
- alert: MaemusaeApiNotReady
  expr: probe_success{job="api-ready", namespace="maemusae"} == 0
  for: 15s
  # 예외, 5xx 규칙의 창(5m)만큼 더 떠 있어야 DB가 돌아온 뒤 남은 예외 알림이 inhibit에서 풀려 나가지 않는다
  keep_firing_for: 5m
  labels:
    severity: critical
  annotations:
    summary: "api 준비 상태 확인(/health/ready)이 실패합니다. DB 연결을 확인하세요"
```

## Alertmanager

같은 장애는 한 묶음으로 보내고, 예외 알림이 떠 있으면 같은 앱의 5xx 알림은 막습니다. 해소 알림도 보내야 메시지에 해소 확인이 달립니다.

```yaml
route:
  routes:
    - receiver: ops-maemusae
      matchers: ['namespace="maemusae"']
      group_by: [alertname, namespace, app, container]
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 4h
receivers:
  - name: ops-maemusae
    webhook_configs:
      - url: http://ops-api.ops.svc:8000/api/v1/alerts/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/ops-alertmanager-token/token
inhibit_rules:
  - source_matchers: ['alertname="MaemusaeUnhandledException"']
    target_matchers: ['alertname="MaemusaeHighErrorRate"']
    equal: [namespace, app]
  # DB가 원인이면 예외와 5xx 알림은 결과다. 준비 상태 알림 하나만 보낸다
  - source_matchers: ['alertname="MaemusaeApiNotReady"']
    target_matchers: ['alertname="MaemusaeUnhandledException"']
    equal: [namespace, app]
  - source_matchers: ['alertname="MaemusaeApiNotReady"']
    target_matchers: ['alertname="MaemusaeHighErrorRate"']
    equal: [namespace, app]
```

준비 상태 알림이 예외 알림을 막으려면 예외 알림 묶음이 나가기 전(`group_wait` 안)에 준비 상태 알림이 Alertmanager에 와 있어야 합니다. 준비 상태 알림의 `for`를 `group_wait`보다 짧게 둡니다. 거꾸로 해소는 `keep_firing_for`만큼 늦어집니다.

`ops-api.ops.svc`는 예시입니다. OPS를 올리는 namespace와 Service 이름에 맞춥니다. 토큰 값은 OPS의 `ALERTMANAGER_TOKEN`과 같습니다.
