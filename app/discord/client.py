"""Discord REST로 카드를 올리고 고친다. 봇 토큰 하나로 워커(게시)와 승인 봇(버튼 수신)이 같은 봇 계정을 쓴다.

토큰이 없으면 로그만 남긴다. 보내기에 실패해도 예외를 올리지 않는다. 분석 결과는 이미 저장됐고,
알림 실패로 워커가 같은 메시지를 다시 처리하면 안 된다.
"""
import logging

import httpx

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"


class DiscordClient:
    def __init__(self, bot_token: str, http: httpx.Client | None = None):
        self.enabled = bool(bot_token)
        self.http = http or httpx.Client(base_url=API, timeout=10, headers={"Authorization": f"Bot {bot_token}"})

    def post(self, channel_id: str, message: dict, reply_to: str | None = None) -> str:
        """보낸 메시지 ID. 보내지 않았거나 실패하면 빈 문자열."""
        if not self.enabled or not channel_id:
            log.info("[discord disabled] #%s %s", channel_id, str(message)[:300])
            return ""
        if reply_to:
            message = {**message, "message_reference": {"message_id": reply_to, "fail_if_not_exists": False}}
        try:
            resp = self.http.post(f"/channels/{channel_id}/messages", json=message)
            resp.raise_for_status()
            return resp.json()["id"]
        except httpx.HTTPError as e:
            log.warning("discord post to %s failed: %s", channel_id, e)
            return ""

    def edit(self, channel_id: str, message_id: str, changes: dict) -> None:
        """PATCH라서 changes에 없는 필드(embed 등)는 그대로 남는다."""
        if not self.enabled or not message_id:
            log.info("[discord disabled] edit #%s %s", channel_id, str(changes)[:200])
            return
        try:
            self.http.patch(f"/channels/{channel_id}/messages/{message_id}", json=changes).raise_for_status()
        except httpx.HTTPError as e:
            log.warning("discord edit of %s/%s failed: %s", channel_id, message_id, e)
