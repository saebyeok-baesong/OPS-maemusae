# INFRA 레포가 준비할 것

OPS 이미지 하나를 INFRA `gitops/`에서 세 Deployment, 마이그레이션 Job, 보안 스캔 수집 CronJob으로 올립니다. 이 문서는 그 정의를 쓰는 사람을 위한 목록입니다. 매니페스트 자체는 INFRA에 둡니다.

## 워크로드

모두 `incident` namespace에 둡니다.

| 이름 | 명령 | 개수 | 쿠버네티스 권한 | 바깥으로 나가는 연결 |
| --- | --- | --- | --- | --- |
| api | 이미지 기본 명령(`uvicorn app.main:app --host 0.0.0.0 --port 8000`) | 1개 이상 | `maemusae` namespace의 `events` get, list | Loki, Kubernetes API, `api.github.com`, Redis, RDS 둘 |
| worker | `python -m app.worker` | 2개 권장 | 없음(`automountServiceAccountToken: false`) | `api.openai.com`, `discord.com`, `api.github.com`, Redis, RDS 둘 |
| approvals | `python -m app.approvals` | 1개, `strategy: Recreate` | 없음 | `gateway.discord.gg`(wss), `discord.com`, `api.github.com`, RDS incident |
| migrate | `python -m app.db` | Argo CD PreSync Job | 없음 | RDS 둘 |
| ops-security-ingest | `python -m app.security.ingest_s3` | CronJob, `concurrencyPolicy: Forbid` | 없음 | S3, RDS security |

- 세 Deployment에 init container `python -m app.wait --schema`를 붙이면 마이그레이션이 끝난 뒤에 뜹니다.
- api의 Service는 ClusterIP입니다. Alertmanager가 있는 monitoring namespace와 kube-bench Job에서만 들어오게 NetworkPolicy로 막습니다.
- approvals를 두 개 띄우면 두 봇 세션이 같은 클릭을 받을 수 있습니다. merge는 DB 제약 때문에 한 번이지만 답이 두 번 달립니다. 그래서 한 개입니다.
- 런북은 이미지 안 `knowledge/`에 있고 worker가 시작할 때 바뀐 청크만 임베딩합니다. 따로 Job이 필요 없습니다.
- ECR 저장소는 `ops` 하나입니다. 위 다섯 워크로드가 모두 같은 이미지를 명령만 바꿔 씁니다.

### 보안 스캔 수집 CronJob

GitHub Actions 러너는 클러스터 밖이라 ClusterIP인 api에 닿지 않습니다. 그래서 CI는 Trivy, gitleaks JSON을 S3에 올리고, 이 CronJob이 가져와 security DB에 넣습니다. 계약은 [SPEC contracts/security-scan-upload.md](https://github.com/saebyeok-baesong/SPEC-maemusae/blob/main/contracts/security-scan-upload.md)에 있습니다.

| 항목 | 값 |
| --- | --- |
| namespace, ServiceAccount | `incident`, `ops-security-ingest` |
| 일정 | `*/30 * * * *` 권장. 일일 보고(9시 KST)에 전날 결과가 들어가려면 클러스터가 뜬 뒤 9시 전에 한 번은 돌아야 한다 |
| 환경변수 | `SECURITY_SCAN_BUCKET=maemusae-security-scans-<account_id>`, `AWS_REGION=ap-northeast-2`, `SECURITY_DATABASE_URL`, `INFRA_REPO`(실행 링크의 조직 이름을 여기서 읽는다) |
| AWS 권한 | EKS Pod Identity. 그 버킷에 `s3:ListBucket`, `s3:GetObject`만 |

- 이미 넣은 객체 키는 security DB의 `security_scan_objects`에 남깁니다. 몇 번을 돌려도 같은 객체는 한 번만 들어가고, 두 Job이 겹쳐도 키 unique 제약이 막습니다.
- 키나 JSON 형식이 틀린 객체는 사유와 함께 기록하고 다시 읽지 않습니다. S3 읽기가 실패하면 Job이 실패로 끝나고 다음 실행이 다시 읽습니다.
- 버킷 lifecycle은 90일입니다. 객체가 지워져도 이미 넣은 결과는 security DB에 남습니다.

## 데이터베이스와 Redis

- RDS `incident`: OPS의 `DATABASE_URL`. 마이그레이션이 `CREATE EXTENSION vector`, `CREATE EXTENSION pg_trgm`을 합니다. 마이그레이션을 도는 사용자에게 그 권한(`rds_superuser`)이 필요합니다. LangGraph 체크포인트 테이블도 여기에 생깁니다.
- RDS `security`: OPS의 `SECURITY_DATABASE_URL`.
- RDS 비밀번호는 RDS가 Secrets Manager에서 회전합니다. External Secrets 템플릿으로 `postgresql://...` 주소를 만들고, 회전 뒤에는 Pod를 다시 띄워야 새 비밀번호를 씁니다.

### Redis

`incident` namespace에 StatefulSet(Pod 하나)으로 띄웁니다. ElastiCache는 쓰지 않습니다. 큐(Redis Streams)와 캐시를 담습니다. AOF(`--appendonly yes`)와 PVC를 붙이면 하루 안의 Pod 재시작에는 큐 메시지가 남습니다.

클러스터를 매일 내리면 Redis 데이터도 함께 사라집니다. 받아들인 결정이고, 뜻은 다음과 같습니다.

- 코어 타임이 끝나면 큐가 비워집니다. 클러스터를 내릴 때 아직 분석하지 않은 장애는 다음 날로 넘어가지 않습니다. 그 장애는 incident DB에 `queued` 또는 `running`으로 남고 카드는 올라가지 않습니다. 다시 처리하려면 Alertmanager가 다시 보내야 합니다.
- 실패 메시지 보관 스트림(`incidents:dlq`)도 사라집니다. 실패 사유는 incident DB의 장애 상태(`failed`, `error`)에 남습니다.
- 중복 제거 키(10분), LLM 응답 캐시, 임베딩 캐시, 검색 캐시가 비워집니다. 다음 날 첫 장애는 캐시 없이 돌아 LLM 비용이 조금 더 듭니다.
- 장애 기록, 조치(롤백) 기록, LangGraph 체크포인트, 보안 결과는 RDS에 있어 남습니다.

## 비밀값

Secrets Manager `maemusae/ops`에 넣고 External Secrets로 옮깁니다.

| 키 | 쓰는 워크로드 |
| --- | --- |
| `ALERTMANAGER_TOKEN` | api. 같은 값을 monitoring namespace의 Alertmanager Secret에도 넣는다 |
| `SECURITY_REPORT_TOKEN` | api. 같은 값을 kube-bench Job에도 넣는다. GitHub Actions에는 넣지 않는다 |
| `OPENAI_API_KEY` | worker |
| `DISCORD_BOT_TOKEN` | worker, approvals |
| `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` | 롤백 App. api(배포 이력 읽기), worker(롤백 PR 열기), approvals(merge) |
| `DATABASE_URL`, `SECURITY_DATABASE_URL`, `REDIS_URL` | 위 표대로 |

비밀이 아닌 값(`LOKI_URL`, `DISCORD_*_CHANNEL_ID`, `DISCORD_APPROVER_ROLE_IDS`, `LIGHT_MODEL`, `HEAVY_MODEL`, `MODEL_PRICES`, `ROLLBACK_APPS`, `SECURITY_SCAN_BUCKET`)은 ConfigMap에 둡니다.

## GitHub App 두 개

봇을 두 개로 나눕니다. 키를 갖는 곳이 다르기 때문입니다. 이미지 PR App의 키는 GitHub Actions(CI)에, 롤백 App의 키는 클러스터에 있습니다. 하나가 새도 다른 쪽 권한은 드러나지 않습니다.

| App | 쓰는 곳 | 하는 일 | 리뷰 규칙 우회 |
| --- | --- | --- | --- |
| 이미지 PR App | SERVICE, OPS의 GitHub Actions | 새 digest로 `images.yaml`의 자기 줄을 바꾸는 PR을 연다 | 없음. 사람이 리뷰하고 merge한다 |
| 롤백 App | 클러스터 안 OPS | 배포 이력을 읽고, 롤백 PR을 열고, Discord 승인 뒤 merge한다 | INFRA `main` ruleset에서 이 App 하나만 |

### 롤백 App

Discord에서 사람이 승인했으면 merge 때 GitHub 리뷰를 다시 받지 않습니다. Discord 승인이 사람 리뷰입니다. 그래서 롤백 App은 사람 확인 없이 `main`에 merge할 수 있고, 접근 범위를 아래처럼 좁힙니다.

1. 조직 `saebyeok-baesong`에 GitHub App을 만듭니다. Webhook은 끕니다.
2. Repository permissions는 Contents(Read and write), Pull requests(Read and write), Metadata(Read)만 줍니다. Workflows, Administration, Actions, Secrets를 비롯한 다른 권한과 Organization permissions는 주지 않습니다.
3. 설치 범위는 "Only select repositories"로 INFRA-maemusae 하나만 고릅니다. 설치 ID가 `GITHUB_APP_INSTALLATION_ID`입니다.
4. private key를 만들어 Secrets Manager `maemusae/ops`의 `GITHUB_APP_PRIVATE_KEY`로 넣습니다. `maemusae/` 경로 밖에는 두지 않고, 내려받은 `.pem` 파일은 넣은 뒤 지웁니다. 이 키를 담은 쿠버네티스 Secret은 approvals ServiceAccount만 읽게 합니다(지금 코드의 api, worker 사용은 PR 본문의 미결 질문).
5. INFRA `main` ruleset의 우회 목록(bypass list)에 이 App 하나만 넣습니다. 우회는 `main` 브랜치에만 적용합니다. 다른 브랜치, 다른 레포, 다른 App이나 사람으로 우회 범위를 넓히려면 보안팀 리뷰를 받습니다.

GitHub App 권한은 파일 단위로 좁힐 수 없습니다. Contents 쓰기가 있으면 INFRA의 어떤 파일이든 바꿀 수 있습니다. 그래서 OPS 코드가 merge 직전에 PR을 다시 확인하고, 아래 중 하나라도 어긋나면 merge하지 않고 장애 카드에 거부 사유를 답니다(`app/rollback.py`의 `diff_problem`).

- PR의 base가 `main`이다.
- 바뀐 파일이 `gitops/apps/images.yaml` 하나이고 상태가 modified다.
- 바뀐 줄이 롤백 대상 앱 한 줄뿐이고, 그 줄이 제안 때 이미지에서 직전 이미지로 바뀐다.

봇이 쓰는 경로는 `gitops/apps/images.yaml` 한 파일입니다. 형식은 [SPEC contracts/images-yaml.md](https://github.com/saebyeok-baesong/SPEC-maemusae/blob/main/contracts/images-yaml.md)를 따릅니다.

### 이미지 PR App

SERVICE, OPS의 CI가 씁니다. 권한은 Contents, Pull requests 쓰기와 Metadata 읽기이고 INFRA-maemusae에만 설치합니다. 키는 각 레포의 GitHub Actions Secrets에 둡니다. ruleset 우회 목록에는 넣지 않습니다.

## Discord 봇

1. Discord Developer Portal에서 애플리케이션과 봇을 만듭니다. Privileged Gateway Intents는 모두 끕니다. 봇은 버튼 클릭(interaction)만 받습니다.
2. Interactions Endpoint URL은 비워 둡니다. 클릭은 Gateway 연결로 받습니다.
3. OAuth2 URL Generator에서 scope `bot`, 권한 View Channels, Send Messages, Embed Links, Read Message History를 골라 서버에 초대합니다.
4. 장애 채널과 보안 채널 ID, 승인 역할 ID를 개발자 모드에서 복사해 ConfigMap에 넣습니다.

`DISCORD_APPROVER_ROLE_IDS`는 쉼표로 나눈 역할 ID 목록입니다(예: `111,222,333`). 이 중 하나라도 가진 사람이 롤백을 승인합니다. 처음에는 풀스택팀, 인프라팀, 보안팀 역할 셋을 모두 넣고, 운영이 익숙해지면 줄입니다. 비우면 아무도 승인하지 못합니다.

## 보안 결과를 보내는 쪽

| 보내는 쪽 | 경로 |
| --- | --- |
| GitHub Actions(Trivy, gitleaks) | S3 버킷에 올린다. 위 CronJob이 가져온다 |
| kube-bench Job(클러스터 안) | `POST /api/v1/security/kube-bench`로 api에 바로 보낸다 |

형식은 [security-reports.md](security-reports.md)에 있습니다.
