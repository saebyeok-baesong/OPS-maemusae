# 로컬 통합 데모

노트북 한 대에서 docker compose로 AI 장애 대응 흐름 전체를 돌려 봅니다. AWS, GitHub, Discord, OpenAI 키가 없어도 됩니다.

```text
장애 주입 -> Prometheus/Loki 알림 -> Alertmanager webhook -> OPS api(중복 제거, 큐) -> worker(분류, 런북 검색, 원인 분석)
-> 장애 메시지 -> 승인 -> 롤백(로컬 gitops 저장소의 images.yaml 한 줄) -> deployer가 이전 이미지로 교체 -> 알림 해소
```

## 준비물

- Docker Desktop. 메모리 4GB 이상을 권합니다. 모두 띄우면 약 1.4GB를 씁니다(worker 640MB, Grafana 250MB, api 140MB, Alloy 120MB, 나머지 각 50MB 이하). 3GB에서도 돌았습니다.
- 처음 한 번 이미지 빌드와 내려받기에 몇 분이 걸리고 디스크를 약 5GB 씁니다. 그다음부터 `up.sh`는 30초 안쪽입니다.
- 비어 있어야 하는 호스트 포트: 18000(OPS api), 18080(WatchNyang api), 13000(Grafana), 13100(Loki), 19090(Prometheus), 19093(Alertmanager).
- bash, curl. 스크립트는 macOS와 Linux에서 돌아갑니다.

## 폴더 배치

SERVICE 레포를 OPS 레포 옆에 둡니다. 다른 곳에 있으면 `SERVICE_DIR=/경로/SERVICE-watch-nyang`으로 알려 줍니다. SERVICE 레포는 고치지 않고 `backend/Dockerfile`로 이미지만 만듭니다.

```text
saebyeok-baesong/
  OPS-watch-nyang/      이 레포. demo/에서 실행
  SERVICE-watch-nyang/  backend/Dockerfile을 빌드한다
```

## 한 번에 띄우기와 내리기

```bash
demo/up.sh     # 이미지 빌드, 로컬 gitops 저장소 초기화(api v1), 전체 기동. 다시 불러도 같은 상태가 된다
demo/down.sh   # 컨테이너와 볼륨(gitops 저장소, 두 DB)을 모두 지운다. 이미지는 남긴다
```

`up.sh`가 끝나면 다음 주소가 열립니다.

| 주소 | 내용 |
| --- | --- |
| http://localhost:13000 | Grafana 대시보드 "WatchNyang 장애 대응 데모". 상태별 요청 수, 준비 상태, 울리는 알림, 예외 로그 |
| http://localhost:19090/alerts | Prometheus 알림 규칙 |
| http://localhost:19093 | Alertmanager. 막힌(inhibit) 알림도 보인다 |
| http://localhost:18080/docs | WatchNyang api (지금 떠 있는 이미지) |
| http://localhost:18000/healthz | OPS api |

Discord 메시지는 봇 토큰이 없으면 `docker compose -p watchnyang-demo -f demo/compose.yaml logs -f ops-worker`에 `[discord disabled]` 줄로 찍힙니다. 이 창을 하나 띄워 두면 흐름을 따라가기 쉽습니다.

## 무엇이 뜨는가

| 묶음 | 컨테이너 | 하는 일 |
| --- | --- | --- |
| WatchNyang | `service-db`(postgres:17), `service-migrate`(alembic, 관리자 시드), `watchnyang-api` | api는 compose가 아니라 deployer가 띄운다 |
| 배포 | `deployer` | Argo CD 대역. 로컬 gitops 저장소 `main`의 `gitops/apps/images.yaml` api 줄을 2초마다 읽고, 떠 있는 이미지가 다르면 컨테이너를 그 이미지로 바꾼다 |
| 트래픽 | `traffic` | 0.5초마다 `GET /api/auth/terms`, `GET /health` |
| 관측 | `alloy`, `loki`, `prometheus`, `blackbox`, `alertmanager`, `grafana` | api JSON 로그 -> Loki 규칙(예외, 5xx 비율), `/health/ready` 탐침 -> Prometheus 규칙(준비 상태), Alertmanager -> OPS webhook |
| OPS | `ops-redis`, `ops-postgres`(pgvector), `ops-migrate`, `ops-api`, `ops-worker`, `ops-approvals`(Discord 봇 토큰이 있을 때만) | 루트 `docker-compose.yml`과 같은 구성. 이미지에 git만 더했고 `INFRA_REPO_BACKEND=local-git`을 켠다 |

이미지는 넷입니다. `watchnyang-api:v1`은 SERVICE `backend/Dockerfile` 그대로, `watchnyang-api:v2`는 v1 위에 회귀 버그 하나를 심은 데모 전용 이미지(`images/api-bad.Dockerfile`, `GET /api/auth/terms`가 `KeyError`로 500), `watchnyang-ops:demo`는 OPS 이미지에 git을 더한 것, `watchnyang-demo-deployer`는 docker CLI와 git입니다.

## 시나리오 1. 나쁜 배포와 롤백 (`scenarios/bad-deploy.sh`)

배포 직후 장애가 나면 롤백 버튼이 붙은 장애 메시지가 오고, 승인하면 이전 이미지로 돌아가 해소됩니다.

```bash
demo/scenarios/bad-deploy.sh                 # 승인 단계에서 Enter를 기다린다
AUTO_APPROVE=1 demo/scenarios/bad-deploy.sh  # 묻지 않고 승인한다(리허설용)
```

| 단계 | 화면 | 걸리는 시간(실측) |
| --- | --- | --- |
| 1. CI 이미지 PR이 merge된 것처럼 gitops `main`의 api 줄을 v2 digest로 바꾼다 | 터미널. deployer가 v2로 바꾸고 `GET /api/auth/terms`가 500 | 배포 후 3초 |
| 2. Loki 규칙 `MaemusaeUnhandledException`이 울리고 Alertmanager가 OPS로 보낸다. OPS가 스택트레이스(Loki)와 직전 배포(gitops 이력)를 붙여 큐에 넣고 worker가 분석한다 | Grafana 5xx 급증, worker 로그의 장애 메시지(분류, 근거 `KeyError: 'TERMS_OF_SERVICE_V2'`, 배포 0분 전, 롤백 제안, 런북) | 배포 후 35초 안팎 |
| 3. 승인. Enter(또는 Discord 버튼)를 누르면 OPS가 롤백 PR(로컬 브랜치)의 diff를 확인하고 `main`에 squash merge한다 | 터미널의 `merged: PR #1 merge ...`, worker 로그의 답글 | 즉시 |
| 4. deployer가 바뀐 `main`을 보고 v1으로 되돌린다 | `GET /api/auth/terms`가 200, Grafana 5xx가 0 | 승인 후 3초 |
| 5. Loki 규칙 창(1분)이 지나면 알림이 풀리고 OPS가 해소 확인을 단다 | worker 로그의 `✅ 알림 해소 확인 · 롤백 merge 1분 뒤` | 승인 후 70초 안팎 |

끝에 단계별 걸린 시간을 찍습니다. 다시 돌리면 먼저 api를 v1으로 맞추고 앞선 알림이 풀리기를 기다립니다. 앞선 알림이 떠 있는 채로 배포하면 그 알림의 시작 시각이 배포 전이라 롤백 제안이 붙지 않기 때문입니다. 나쁜 배포 -> DB 장애 순서로 돌리기를 권합니다. 거꾸로 하면 사이에 1-3분을 기다릴 수 있습니다.

## 시나리오 2. DB 장애 (`scenarios/db-down.sh`)

이미지를 되돌려도 낫지 않는 장애입니다. 롤백 없이 런북 안내만 옵니다.

```bash
demo/scenarios/db-down.sh                 # DB를 다시 켜기 전에 Enter를 기다린다
AUTO_APPROVE=1 demo/scenarios/db-down.sh
```

| 단계 | 화면 | 걸리는 시간(실측) |
| --- | --- | --- |
| 1. `service-db`를 멈춘다 | `/health/ready`가 503 `DB_UNAVAILABLE` | 즉시 |
| 2. blackbox 탐침이 실패해 `MaemusaeApiNotReady`가 울린다. 같은 시간 예외, 5xx 알림도 울리지만 Alertmanager가 막는다(inhibit) | Alertmanager에 막힌 알림 둘, worker 로그의 장애 메시지(분류 `dependency`, 런북 `api/not-ready.md`, 롤백 버튼 없음) | DB 중단 후 30초 안팎 |
| 3. 런북대로 DB를 다시 켠다 | api가 다시 준비됨(재시작 없음) | 1초 |
| 4. 알림 해소. 준비 상태 알림은 DB가 돌아온 뒤 1분 더 떠 있다(`keep_firing_for`). 그동안 남은 예외 알림이 inhibit에서 풀려 두 번째 메시지로 나가지 않게 한다 | worker 로그의 `✅ 알림 해소 확인`. 끝에 이 시나리오에서 생긴 장애가 준비 상태 하나뿐인지 찍는다 | DB 시작 후 70초 안팎 |

## 선택. 보안 일일 보고 (`scenarios/security-report.sh`)

장애 대응 흐름과 별개인 선택 시나리오입니다. 예시 Trivy, gitleaks, kube-bench 결과(`security/*.json`)를 `POST /api/v1/security/{source}`로 넣고, 오늘 보고 기록을 지운 뒤 보고 시각을 0시(KST)로 바꿔 worker를 다시 띄워 바로 보고를 만듭니다. 끝나면 worker를 보고 꺼진 상태로 되돌립니다. 평소 데모에서는 `SECURITY_REPORT_HOUR_KST=24`(오지 않는 시각)로 보고를 꺼 둡니다.

## OpenAI와 Discord 켜기

`demo/.env`를 만들면 스크립트가 읽습니다(`.env.example` 참고). 둘 다 비우면 가짜 LLM과 로그 출력입니다.

- `OPENAI_API_KEY`가 있으면 `openai:gpt-4.1-mini`(분류), `openai:gpt-4.1`(원인 분석)을 씁니다. `LIGHT_MODEL`, `HEAVY_MODEL`로 바꿀 수 있습니다. 바꾼 뒤 `up.sh`를 다시 부릅니다.
- `DISCORD_BOT_TOKEN`, `DISCORD_INCIDENT_CHANNEL_ID`, `DISCORD_APPROVER_ROLE_IDS`가 있으면 장애 채널에 실제 메시지가 가고 승인 봇(`ops-approvals`)도 뜹니다. 시나리오는 Enter 대신 버튼 클릭을 기다립니다. 롤백 PR 링크는 자리표시 주소라 눌러도 열리지 않습니다.
- Discord 없이 승인하는 명령은 `docker compose -p watchnyang-demo -f demo/compose.yaml exec ops-api python -m app.approvals approve <장애 ID> --by <이름>`입니다. 역할 확인 대신 OPS 컨테이너에 명령을 넣을 수 있는 권한을 승인 권한으로 보고, 조치 기록에 `cli:<이름>`으로 남깁니다. `CLI_APPROVAL_ENABLED=true`일 때만 되고, 데모 compose만 켭니다.

## 데모 시간 값과 운영 값

빨리 보이도록 줄였습니다. 운영 값은 [docs/alerting.md](../docs/alerting.md)와 INFRA `gitops/`를 따릅니다.

| 항목 | 데모 | 운영 |
| --- | --- | --- |
| Loki 규칙 창 | 1m | 5m |
| `MaemusaeHighErrorRate` `for` | 20s | 2m |
| `MaemusaeApiNotReady` `for` | 10s | 15s(제안, docs/alerting.md) |
| Loki ruler 평가 주기 | 10s | 1m |
| Prometheus 수집, 평가 주기 | 5s | 30s |
| Alertmanager `group_wait`, `group_interval` | 15s, 10s | 30s, 5m |
| OPS 지문 중복 제거 | 60s | 600s |

걸리는 시간을 정하는 것은 이 값들입니다. 배포 -> 메시지는 대략 "첫 에러 로그 -> ruler 평가(10초 주기) -> group_wait 15초 -> OPS 접수와 분석(1초 안쪽, 가짜 LLM)"입니다. 해소는 마지막 에러가 Loki 규칙 창(1분) 밖으로 나가야 풀립니다. 실제 OpenAI를 쓰면 분석에 수 초가 더 듭니다. `group_wait`를 15초보다 줄이면 DB 장애 때 예외 알림이 준비 상태 알림보다 먼저 나가 inhibit이 듣지 않습니다.

## 흉내 낸 것과 실제

| 데모 | 실제(EKS 설계) | 같은 점과 다른 점 |
| --- | --- | --- |
| 로컬 bare git 저장소(볼륨 `gitops`)의 `gitops/apps/images.yaml` | GitHub INFRA-watch-nyang의 같은 파일 | 파일 형식(SPEC `contracts/images-yaml.md`), 이력으로 배포 시각과 직전 이미지 찾기, `[ops-rollback]` 표시가 같다 |
| `INFRA_REPO_BACKEND=local-git` (`app/sources/local_git.py`) | GitHub App `InfraRepo` (`app/sources/github.py`) | 같은 인터페이스. PR은 브랜치와 기록 파일 하나, merge는 squash. 승인 처리(`app/rollback.py`)의 제안 뒤 변경 확인, PR diff 확인(`diff_problem`)을 그대로 지난다 |
| `deployer` (docker CLI) | Argo CD sync | `main`을 보고 이미지를 맞춘다. 롤링 업데이트가 아니라 컨테이너를 바꾸므로 1-3초 끊긴다 |
| 이미지 digest = 로컬 이미지 ID | ECR digest | containerd 이미지 저장소(Docker Desktop 기본)에서는 둘이 같다 |
| Alloy가 docker 소켓으로 컨테이너 로그 수집, 라벨 `namespace`, `container`, `app`, `pod`를 붙임 | Pod 로그 수집 | Loki 규칙과 OPS의 Loki 조회는 운영과 같은 라벨을 쓴다 |
| blackbox로 `/health/ready` 탐침 | readinessProbe + kube-state-metrics, 또는 같은 blackbox | 알림 이름 `MaemusaeApiNotReady` |
| 가짜 LLM(`fake:light`, `fake:heavy`) | OpenAI | 알림 문구의 단서로 정해진 답을 낸다. 분류와 런북 검색 흐름은 같다 |
| Discord 메시지를 worker 로그로 | Discord 장애 채널 | 메시지 본문(JSON)이 같다 |
| CLI 승인 | Discord 버튼과 승인 역할 | 같은 `rollback.approve`를 부른다 |
| CrashLoop, OOMKilled | kube-state-metrics 규칙 | 데모에 없다. docker에는 재시작 지표가 없어 cAdvisor를 더해야 한다 |

docker 소켓은 `deployer`(쓰기)와 `alloy`(읽기)만 씁니다. 둘 다 데모 전용입니다. OPS 컨테이너에는 소켓을 주지 않습니다.

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| `SERVICE 레포가 없다` | 폴더 배치를 확인하거나 `SERVICE_DIR`을 준다 |
| v2 빌드가 `list_terms 모양이 달라` 로 멈춤 | SERVICE 코드가 바뀌었다. `images/break-terms.py`의 원문 줄을 맞춘다 |
| 포트 충돌 | `docker ps`로 같은 포트를 쓰는 컨테이너를 찾는다. compose.yaml의 `ports`와 스크립트의 주소를 같이 바꾼다 |
| 알림이 안 울림 | Grafana에서 요청이 보이는지, http://localhost:19090/targets 가 모두 UP인지, `curl -s localhost:13100/loki/api/v1/label/container/values`에 `api`가 있는지 본다 |
| 장애가 안 생김 | `logs ops-api`에서 webhook 202를 본다. 같은 알림을 60초 안에 다시 일으키면 지문 중복 제거로 새 장애를 만들지 않는다 |
| 시나리오가 `앞선 알림이 모두 해소될 때까지` 에서 오래 멈춤 | 앞선 장애가 아직 울린다. http://localhost:19093 을 본다. 급하면 `down.sh` 후 `up.sh` |
| worker가 메모리 부족으로 죽음 | Docker Desktop 메모리를 4GB 이상으로 올린다. Grafana를 빼려면 `dc stop grafana` |

## 다음 단계

- SERVICE #5(frontend Dockerfile)와 compose 이슈가 들어오면 SERVICE 팀의 이미지와 compose를 쓰도록 바꾸고 web도 넣습니다. 지금은 backend만 직접 빌드합니다.
- AWS로 옮길 때는 `INFRA_REPO_BACKEND`를 기본값(`github`)으로 두고 GitHub App 값을 넣습니다. 알림 규칙은 이 폴더의 값이 아니라 docs/alerting.md의 운영 값으로 INFRA `gitops/`에 넣습니다.
