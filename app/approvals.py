"""Discord 승인 봇(6단계 조치). Gateway에 나가는 연결만 맺어 버튼 클릭을 받는다. 공개 interactions 주소는 없다.

실행: python -m app.approvals
Discord 없이 승인: python -m app.approvals approve <장애 ID> --by <이름>
"""
import argparse
import asyncio
import logging

from app.container import Container
from app.discord import cards
from app.rollback import Approval, Status, approve, short

log = logging.getLogger("approvals")

# 누른 사람에게만 보이는 답. 조치가 실행된 경우(MERGED, FAILED, STALE, REJECTED)는 메시지에 모두가 보는 답을 단다
PRIVATE_REPLIES = {
    Status.NOT_ALLOWED: "승인 권한이 없습니다. 승인 역할이 필요합니다.",
    Status.UNKNOWN: "장애 기록을 찾지 못했습니다.",
    Status.NO_PROPOSAL: "이 장애에는 실행할 수 있는 조치가 없습니다.",
    Status.ALREADY: "이미 승인되어 실행했거나 실행 중인 조치입니다.",
}
CLI_ROLE = "cli"
PUBLIC_TITLE = {Status.MERGED: "✅ 롤백 PR merge", Status.FAILED: "❌ 롤백 실패", Status.STALE: "⏸️ 롤백 보류",
                Status.REJECTED: "🚫 롤백 거부"}


def handle_click(c: Container, incident_id: str, user_id: str, roles: list[str], channel_id: str, message_id: str) -> tuple[Status, str]:
    """돌려주는 문자열은 누른 사람에게만 보일 답이다(조치가 실행됐으면 빈 문자열)."""
    result = approve(c.store, c.infra, incident_id, user_id, roles, c.settings.discord_approver_role_ids)
    return result.status, _announce(c, result, f"<@{user_id}>", channel_id, message_id)


def _announce(c: Container, result: Approval, approver: str, channel_id: str, message_id: str) -> str:
    if result.status in PRIVATE_REPLIES:
        return PRIVATE_REPLIES[result.status]
    # 버튼을 지워 같은 메시지에서 다시 누르지 않게 한다. 다시 눌러도 actions의 unique 제약이 막는다
    c.discord.edit(channel_id, message_id, {"components": []})
    p = result.pr.plan
    text = (f"{PUBLIC_TITLE[result.status]} · `{p.app}` `{short(p.from_image)}` -> `{short(p.to_image)}` "
            f"· 승인 {approver} · [INFRA PR #{result.pr.number}]({result.pr.url})\n{result.detail}")
    c.discord.post(channel_id, cards.reply(text), reply_to=message_id)
    return ""


def approve_from_cli(c: Container, incident_id: str, operator: str) -> Approval:
    """Discord 없이 승인한다. 역할 확인 대신 이 컨테이너에 명령을 넣을 수 있는 권한을 승인 권한으로 본다.
    그 권한이면 롤백 App 키도 읽을 수 있어서 역할 확인이 더 막아 주는 것이 없다."""
    result = approve(c.store, c.infra, incident_id, f"cli:{operator}", [CLI_ROLE], [CLI_ROLE])
    record = c.store.get(incident_id)
    channel, message = (record.card_channel_id, record.card_message_id) if record else ("", "")
    _announce(c, result, f"`cli:{operator}`", channel or "", message or "")
    return result


def serve(c: Container) -> None:
    import discord

    client = discord.Client(intents=discord.Intents.none())

    @client.event
    async def on_ready() -> None:
        log.info("approvals: connected as %s", client.user)

    @client.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id", "")
        if interaction.type is not discord.InteractionType.component or not custom_id.startswith(cards.APPROVE_PREFIX):
            return
        # 3초 안에 응답해야 Discord가 실패로 표시하지 않는다. merge는 그 뒤에 한다
        await interaction.response.defer(ephemeral=True, thinking=True)
        roles = [str(r.id) for r in getattr(interaction.user, "roles", [])]
        status, private = await asyncio.to_thread(
            handle_click, c, custom_id.removeprefix(cards.APPROVE_PREFIX), str(interaction.user.id), roles,
            str(interaction.channel_id), str(interaction.message.id),
        )
        log.info("approval %s by %s: %s", custom_id, interaction.user.id, status)
        await interaction.followup.send(private or "처리했습니다. 메시지의 답을 확인하세요.", ephemeral=True)

    client.run(c.settings.discord_bot_token, log_handler=None)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.approvals")
    sub = parser.add_subparsers(dest="command")
    cli = sub.add_parser("approve", help="Discord 없이 롤백을 승인한다")
    cli.add_argument("incident_id")
    cli.add_argument("--by", required=True, help="승인한 사람. 조치 기록에 cli:<이름>으로 남는다")
    args = parser.parse_args()
    c = Container()
    if args.command == "approve":
        if not c.settings.cli_approval_enabled:
            raise SystemExit("CLI_APPROVAL_ENABLED가 꺼져 있다. Discord 버튼으로 승인한다")
        result = approve_from_cli(c, args.incident_id, args.by)
        print(f"{result.status}: {result.detail or PRIVATE_REPLIES.get(result.status, '')}")
        raise SystemExit(0 if result.status in (Status.MERGED, Status.ALREADY) else 1)
    if not c.settings.discord_bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN이 없어 승인 봇을 띄우지 않는다")
    serve(c)


if __name__ == "__main__":
    main()
