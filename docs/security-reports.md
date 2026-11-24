# 보안 결과를 보내는 방법

스캔 도구의 JSON 출력을 그대로 감싸서 OPS api로 보냅니다. OPS는 비밀값을 버리고 security DB에 쌓은 뒤, 매일 `SECURITY_REPORT_HOUR_KST`(기본 9시)에 지난 24시간을 요약해 보안 채널에 올립니다.

## 요청 형식

```
POST /api/v1/security/{source}
Authorization: Bearer <SECURITY_REPORT_TOKEN>
Content-Type: application/json

{"target": "<저장소, 이미지, 클러스터 이름>", "run_url": "<실행 링크, 선택>", "report": <도구 JSON 출력>}
```

| `source` | 도구 명령 | `report` 모양 |
| --- | --- | --- |
| `trivy` | `trivy image --format json` 또는 `trivy fs --format json` | 객체(`Results`) |
| `gitleaks` | `gitleaks detect --report-format json` | 배열 |
| `kube-bench` | `kube-bench --json` | 객체(`Controls`) |

응답은 `202 {"run_id": 3, "findings": 12}`입니다. 형식이 틀리면 422, 토큰이 틀리면 401입니다.

## GitHub Actions에서 보내기

```bash
trivy image --format json --output trivy.json "$IMAGE"
jq -n --arg t "$IMAGE" --arg u "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
  --slurpfile r trivy.json '{target: $t, run_url: $u, report: $r[0]}' \
| curl -sf -X POST "$OPS_URL/api/v1/security/trivy" \
    -H "Authorization: Bearer $SECURITY_REPORT_TOKEN" -H "Content-Type: application/json" -d @-
```

gitleaks 출력에는 찾은 비밀값이 들어 있습니다. OPS는 `Secret`, `Match`를 저장하지 않지만, 보내는 쪽도 아티팩트로 올리지 않습니다.
