# 보안 결과를 보내는 방법

보안 결과가 들어오는 길은 둘입니다. 클러스터 밖 GitHub Actions(Trivy, gitleaks)는 S3에 올리고, 클러스터 안 kube-bench Job은 OPS api로 바로 보냅니다. OPS는 비밀값을 버리고 security DB에 쌓은 뒤, 매일 `SECURITY_REPORT_HOUR_KST`(기본 9시)에 지난 24시간을 요약해 보안 채널에 올립니다.

| 보내는 쪽 | 도구 | 경로 |
| --- | --- | --- |
| SERVICE, OPS, INFRA의 GitHub Actions | `trivy`, `gitleaks` | S3 버킷 -> 수집 CronJob(`python -m app.security.ingest_s3`) |
| kube-bench Job | `kube-bench` | `POST /api/v1/security/kube-bench` |

GitHub Actions 러너는 클러스터 밖이라 ClusterIP인 api에 닿지 않습니다. 그래서 CI는 api로 보내지 않습니다.

## 심각도

| 도구 | 심각도 |
| --- | --- |
| `gitleaks` | 모두 CRITICAL. 비밀번호나 키가 커밋에 들어간 것 자체가 바로 교체할 일이다 |
| `trivy` | 도구가 준 값(`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `UNKNOWN`) |
| `kube-bench` | `FAIL`은 HIGH, `WARN`은 MEDIUM. `PASS`, `INFO`는 버린다 |

## GitHub Actions: S3에 올리기

계약은 [SPEC contracts/security-scan-upload.md](https://github.com/saebyeok-baesong/SPEC-maemusae/blob/main/contracts/security-scan-upload.md)에 있습니다. 요점만 옮깁니다.

- 버킷: `maemusae-security-scans-<account_id>`(OPS 환경변수 `SECURITY_SCAN_BUCKET`)
- 객체 키: `<repo>/<tool>/<YYYY-MM-DD>/<github_run_id>.json`. `repo`는 `SERVICE-maemusae`, `OPS-maemusae`, `INFRA-maemusae` 중 하나, `tool`은 `trivy`, `gitleaks` 중 하나
- 본문: 도구의 JSON 출력 그대로. 감싸지 않는다
- 쓰기: 각 레포의 GitHub OIDC 역할이 자기 `<repo>/` 아래에만 `PutObject`
- 읽기: `incident` namespace의 `ops-security-ingest` ServiceAccount(Pod Identity)가 `ListBucket`, `GetObject`

| `tool` | 도구 명령 | 본문 모양 |
| --- | --- | --- |
| `trivy` | `trivy image --format json` 또는 `trivy fs --format json` | 객체(`Results`) |
| `gitleaks` | `gitleaks detect --report-format json` | 배열 |

수집 Job은 결과의 `target`을 `repo`로, 실행 링크를 `https://github.com/<조직>/<repo>/actions/runs/<github_run_id>`로 남깁니다. 이미 넣은 키는 다시 넣지 않습니다. 키나 JSON 형식이 틀린 객체는 사유를 기록하고 건너뜁니다.

gitleaks 출력에는 찾은 비밀값이 들어 있습니다. OPS는 `Secret`, `Match`를 저장하지 않습니다. 보내는 쪽은 그 파일을 S3 말고 다른 곳(Actions 아티팩트, 로그)에 남기지 않습니다.

## kube-bench: api로 보내기

```
POST /api/v1/security/kube-bench
Authorization: Bearer <SECURITY_REPORT_TOKEN>
Content-Type: application/json

{"target": "<클러스터 이름>", "run_url": "<실행 링크, 선택>", "report": <kube-bench --json 출력>}
```

응답은 `202 {"run_id": 3, "findings": 12}`입니다. 형식이 틀리면 422, 토큰이 틀리면 401입니다. 이 토큰은 kube-bench Job에만 있고 GitHub Actions에는 두지 않습니다.
