"""Connects the bot to Slack over Socket Mode (Slack calls nothing of ours, so no public URL or request signing to expose)."""

import logging
import re

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from app.browser.login_probe import LoginProbe
from app.slack.bot import SlackBot

log = logging.getLogger("nami.slack")


def wire(app: AsyncApp, bot: SlackBot) -> None:
    @app.event("message")
    async def on_message(event, body):
        # Only a person's plain message in a direct message: no edits, deletions, bot posts (including our own), or channel chatter.
        if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id") or not event.get("user"):
            return
        team = body.get("team_id") or event.get("team") or ""
        bot.spawn(bot.on_direct_message(team=team, channel=event["channel"], user=event["user"], text=event.get("text", ""), ts=event["ts"]))

    @app.event("app_mention")
    async def on_mention(event):
        bot.spawn(bot.on_mention(channel=event["channel"], ts=event.get("thread_ts") or event["ts"]))

    @app.action({"action_id": re.compile(r"nami_.*")})
    async def on_action(ack, body, action):
        await ack()
        team = (body.get("team") or {}).get("id") or body.get("user", {}).get("team_id") or ""
        channel = (body.get("channel") or {}).get("id") or ""
        user = body["user"]["id"]
        if action["action_id"] == "nami_code":
            bot.spawn(bot.open_code_modal(team=team, channel=channel, user=user, task_id=action.get("value", ""), trigger_id=body["trigger_id"],
                                   message_ts=(body.get("message") or {}).get("ts", "")))
            return
        bot.spawn(bot.on_action(team=team, channel=channel, user=user, action_id=action["action_id"], task_id=action.get("value", ""),
                                message_ts=(body.get("message") or {}).get("ts", "")))

    @app.view("nami_code_modal")
    async def on_code(ack, body, view):
        value = view["state"]["values"]["code"]["v"].get("value") or ""
        err = await bot.submit_code(team=(body.get("team") or {}).get("id", ""), user=body["user"]["id"],
                                    metadata=view.get("private_metadata", ""), value=value)
        await ack(response_action="errors", errors={"code": err}) if err else await ack()


class SlackService:
    def __init__(self, handler: AsyncSocketModeHandler, bot: SlackBot):
        self.handler, self.bot = handler, bot

    async def stop(self) -> None:
        await self.handler.close_async()
        await self.bot.stop()


async def start_slack(rt) -> SlackService | None:
    s = rt.settings
    if not (s.slack_bot_token and s.slack_app_token):
        return None
    app = AsyncApp(token=s.slack_bot_token)
    bot = SlackBot(client=app.client, orchestrator=rt.orchestrator, links=rt.slack_links, stm=rt.services.stm, ltm=rt.services.ltm,
                   web_url=s.public_url or "http://localhost:8000", prober=LoginProbe(rt.provider))
    wire(app, bot)
    handler = AsyncSocketModeHandler(app, s.slack_app_token)
    await handler.connect_async()
    log.info("Slack bot connected (Socket Mode)")
    return SlackService(handler, bot)
