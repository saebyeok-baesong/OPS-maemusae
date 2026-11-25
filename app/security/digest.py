"""보안 일일 보고. 워커가 메시지를 기다리는 틈마다 tick()을 부르고, 그날 정해진 시각이 지났으면 한 번만 만든다."""
import logging
from datetime import date, datetime, timedelta, timezone

from app.discord.cards import security_card
from app.llm import prompts
from app.llm.cost import BudgetExceeded
from app.llm.schemas import SecuritySummary

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def due_date(now: datetime, hour_kst: int) -> date | None:
    local = now.astimezone(KST)
    return local.date() if local.hour >= hour_kst else None


class SecurityDigest:
    def __init__(self, store, router, discord, channel_id: str, hour_kst: int):
        self.store, self.router, self.discord = store, router, discord
        self.channel_id, self.hour_kst = channel_id, hour_kst

    def tick(self, now: datetime) -> date | None:
        """보고를 만들었으면 그 날짜."""
        day = due_date(now, self.hour_kst)
        if day is None or not self.store.claim_report(day):
            return None
        counts, new = self.store.window(now - timedelta(days=1))
        summary = self._summarize(day, counts, new)
        message_id = self.discord.post(self.channel_id, security_card(day.isoformat(), counts, len(new), summary.summary,
                                                                      summary.top_actions))
        self.store.finish_report(day, counts, len(new), summary.summary, summary.top_actions, message_id)
        log.info("security report %s posted (%d new)", day, len(new))
        return day

    def _summarize(self, day: date, counts: dict, new: list[dict]) -> SecuritySummary:
        if not new and not counts:
            return SecuritySummary(summary="지난 24시간 동안 들어온 보안 결과가 없다. 스캔 작업이 돌았는지 확인한다.")
        try:
            return self.router.invoke("security_summary", SecuritySummary, prompts.SECURITY_SYSTEM,
                                      prompts.security_user(counts, new), subject=f"security:{day.isoformat()}")
        except BudgetExceeded as e:
            return SecuritySummary(summary=f"LLM 비용 상한으로 요약을 건너뛰었다: {e}")
        except Exception as e:  # noqa: BLE001 - 요약이 없어도 집계는 보낸다
            log.exception("security summary failed")
            return SecuritySummary(summary=f"LLM 요약 실패({type(e).__name__}). 집계만 확인한다.")
