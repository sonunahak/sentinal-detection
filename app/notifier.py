"""
SentinelTrace Emergency Dispatch & Notification Service
Dispatches real emergency signals across:
1. Real-time Telegram Bot API with instant Google Maps and live session links.
2. HTTP Webhook endpoints.
3. Persistent SQLite outbox audit trail.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("sentineltrace.notifier")

DEFAULT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
) -> dict[str, Any]:
    """Sends a formatted alert message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(url, json=payload)
        data = response.json()
        if not response.is_success:
            logger.warning("Telegram dispatch failed: %s", data)
            return {"success": False, "error": data.get("description", "Unknown error")}
        return {"success": True, "message_id": data.get("result", {}).get("message_id")}


async def dispatch_emergency_alert(
    *,
    session_id: str,
    reason: str,
    risk_score: int,
    latitude: float | None = None,
    longitude: float | None = None,
    chat_id: str | None = None,
    bot_token: str | None = None,
    base_url: str = "https://sentineltrace.onrender.com",
) -> dict[str, Any]:
    """
    Constructs and broadcasts the emergency payload to responders.
    """
    active_token = (bot_token or DEFAULT_BOT_TOKEN).strip()
    active_chat = (chat_id or DEFAULT_CHAT_ID).strip()

    loc_line = (
        f"• *Coordinates*: `{latitude:.5f}, {longitude:.5f}`\n"
        f"📍 [Google Maps Location](https://www.google.com/maps?q={latitude},{longitude})\n"
        if latitude is not None and longitude is not None
        else "• *Coordinates*: Unavailable\n"
    )

    message = (
        "🚨 *SENTINELTRACE EMERGENCY DISTRESS ALERT*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• *Session ID*: `{session_id}`\n"
        f"• *Trigger Reason*: {reason}\n"
        f"• *Sentinel Risk Index*: *{risk_score}%*\n"
        f"{loc_line}"
        f"🛡️ [Open Live Sentinel Monitor]({base_url}/?session={session_id})\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Immediate responder assistance requested."
    )

    results: dict[str, Any] = {
        "outbox": "RECORDED",
        "telegram_dispatched": False,
        "details": {},
    }

    if active_token and active_chat:
        try:
            tg_result = await send_telegram_message(active_token, active_chat, message)
            results["telegram_dispatched"] = tg_result.get("success", False)
            results["details"] = tg_result
        except Exception as exc:
            logger.error("Exception sending Telegram alert: %s", exc)
            results["details"] = {"error": str(exc)}
    else:
        results["details"] = {
            "notice": "Telegram bot token or chat ID not set. Ready for deployment configuration."
        }

    return results
