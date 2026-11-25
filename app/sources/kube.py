"""클러스터 안에서 서비스 계정 토큰으로 Pod 이벤트를 읽는다. 권한은 events get/list 하나다. 클러스터를 바꾸는 호출은 없다."""
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def in_cluster() -> bool:
    return (SA_DIR / "token").exists()


class Kube:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(
            base_url="https://kubernetes.default.svc", verify=str(SA_DIR / "ca.crt"), timeout=5,
            headers={"Authorization": f"Bearer {(SA_DIR / 'token').read_text()}"},
        )

    def pod_events(self, namespace: str, pod: str, limit: int = 10) -> list[str]:
        """최근 이벤트를 시간순으로 `사유: 메시지` 한 줄씩. 실패하면 빈 목록(접수는 한다)."""
        try:
            resp = self.http.get(f"/api/v1/namespaces/{namespace}/events",
                                 params={"fieldSelector": f"involvedObject.name={pod}"})
            resp.raise_for_status()
            items = resp.json()["items"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("events of %s/%s failed: %s", namespace, pod, e)
            return []
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        return [f"{e.get('reason', '?')}: {e.get('message', '').strip()}"[:300] for e in items[-limit:]]
