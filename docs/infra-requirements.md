# INFRA 레포가 준비할 것

OPS 이미지 하나를 INFRA `gitops/`에서 세 Deployment와 마이그레이션 Job으로 올립니다. 이 문서는 그 정의를 쓰는 사람을 위한 목록입니다. 매니페스트 자체는 INFRA에 둡니다.

## 워크로드

| 이름 | 명령 | 개수 | 쿠버네티스 권한 | 바깥으로 나가는 연결 |
| --- | --- | --- | --- | --- |
| api | 이미지 기본 명령(`uvicorn app.main:app --host 0.0.0.0 --port 8000`) | 1개 이상 | `maemusae` namespace의 `events` get, list | Loki, Kubernetes API, `api.github.com`, Redis, RDS 둘 |
| worker | `python -m app.worker` | 2개 권장 | 없음(`automountServiceAccountToken: false`) | `api.openai.com`, `discord.com`, `api.github.com`, Redis, RDS 둘 |
| approvals | `python -m app.approvals` | 1개, `strategy: Recreate` | 없음 | `gateway.discord.gg`(wss), `discord.com`, `api.github.com`, RDS incident |
| migrate | `python -m app.db` | Argo CD PreSync Job | 없음 | RDS 둘 |

- 세 Deployment에 init container `python -m app.wait --schema`를 붙이면 마이그레이션이 끝난 뒤에 뜹니다.
- api의 Service는 ClusterIP입니다. Alertmanager가 있는 monitoring namespace와 kube-bench Job에서만 들어오게 NetworkPolicy로 막습니다.
- approvals를 두 개 띄우면 두 봇 세션이 같은 클릭을 받을 수 있습니다. merge는 DB 제약 때문에 한 번이지만 답이 두 번 달립니다. 그래서 한 개입니다.
- 런북은 이미지 안 `knowledge/`에 있고 worker가 시작할 때 바뀐 청크만 임베딩합니다. 따로 Job이 필요 없습니다.
- ECR 저장소는 이미 있는 `ops`를 씁니다. AWS API를 부르지 않으므로 IRSA 역할은 필요 없습니다.

## 데이터베이스와 Redis

- RDS `incident`: OPS의 `DATABASE_URL`. 마이그레이션이 `CREATE EXTENSION vector`, `CREATE EXTENSION pg_trgm`을 합니다. 마이그레이션을 도는 사용자에게 그 권한(`rds_superuser`)이 필요합니다. LangGraph 체크포인트 테이블도 여기에 생깁니다.
- RDS `security`: OPS의 `SECURITY_DATABASE_URL`.
- Redis: 큐(Redis Streams)와 캐시. AOF를 켜야 재시작 뒤에도 큐 메시지가 남습니다. 지금 INFRA Terraform에 Redis가 없습니다.
- RDS 비밀번호는 RDS가 Secrets Manager에서 회전합니다. External Secrets 템플릿으로 `postgresql://...` 주소를 만들고, 회전 뒤에는 Pod를 다시 띄워야 새 비밀번호를 씁니다.

## 비밀값

Secrets Manager `maemusae/ops`에 넣고 External Secrets로 옮깁니다.

| 키 | 쓰는 워크로드 |
| --- | --- |
| `ALERTMANAGER_TOKEN` | api. 같은 값을 monitoring namespace의 Alertmanager Secret에도 넣는다 |
| `SECURITY_REPORT_TOKEN` | api. 같은 값을 GitHub Actions Secrets와 kube-bench Job에도 넣는다 |
| `OPENAI_API_KEY` | worker |
| `DISCORD_BOT_TOKEN` | worker, approvals |
| `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` | api, worker, approvals |
| `DATABASE_URL`, `SECURITY_DATABASE_URL`, `REDIS_URL` | 위 표대로 |

비밀이 아닌 값(`LOKI_URL`, `DISCORD_*_CHANNEL_ID`, `DISCORD_APPROVER_ROLE_IDS`, `LIGHT_MODEL`, `HEAVY_MODEL`, `MODEL_PRICES`, `ROLLBACK_APPS`)은 ConfigMap에 둡니다.

## GitHub App

1. 조직 `saebyeok-baesong`에 GitHub App을 만듭니다. Webhook은 끕니다.
2. 권한은 Repository permissions의 Contents(Read and write), Pull requests(Read and write), Metadata(Read)만 줍니다.
3. INFRA-maemusae 하나에만 설치합니다. 설치 ID가 `GITHUB_APP_INSTALLATION_ID`입니다.
4. private key를 만들어 `GITHUB_APP_PRIVATE_KEY`로 넣습니다.
5. INFRA `main`에 리뷰를 요구하는 규칙이 있으면 봇의 merge가 막힙니다. 이 App을 ruleset의 우회 목록에 넣을지 정해야 합니다(PR 본문의 미결 질문).

봇이 쓰는 경로는 `gitops/apps/images.yaml` 한 파일입니다. 형식은 [SPEC contracts/images-yaml.md](https://github.com/saebyeok-baesong/SPEC-maemusae/blob/main/contracts/images-yaml.md)를 따릅니다.

## Discord 봇

1. Discord Developer Portal에서 애플리케이션과 봇을 만듭니다. Privileged Gateway Intents는 모두 끕니다. 봇은 버튼 클릭(interaction)만 받습니다.
2. Interactions Endpoint URL은 비워 둡니다. 클릭은 Gateway 연결로 받습니다.
3. OAuth2 URL Generator에서 scope `bot`, 권한 View Channels, Send Messages, Embed Links, Read Message History를 골라 서버에 초대합니다.
4. 장애 채널과 보안 채널 ID, 인프라팀 역할 ID를 개발자 모드에서 복사해 ConfigMap에 넣습니다.

## 보안 보고를 보내는 쪽

GitHub Actions(Trivy, gitleaks)와 kube-bench Job이 `POST /api/v1/security/{trivy|gitleaks|kube-bench}`로 보냅니다. 형식은 [security-reports.md](security-reports.md)에 있습니다. GitHub Actions 러너는 클러스터 밖이라 ClusterIP인 api에 닿지 않습니다. 어떻게 들여보낼지는 PR 본문의 미결 질문입니다.
