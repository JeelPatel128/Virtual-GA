"""Posts Adaptive Cards to a Teams channel through a Teams Workflows webhook.

Buttons are "open URL" links into the GA console. Real in-Teams buttons and replies need a
registered Teams bot (Azure + IT admin approval); this design avoids that and stays free."""
import logging

import requests

from .config import GA, settings

log = logging.getLogger("virtual_ga.teams")

# Tests swap this to capture cards instead of sending them.
sent_cards: list[dict] = []


def _mention_entities(gas: list[GA]) -> list[dict]:
    return [
        {"type": "mention", "text": f"<at>{g.name}</at>", "mentioned": {"id": g.teams_email, "name": g.name}}
        for g in gas if g.teams_email
    ]


def build_card(title: str, lines: list[tuple[str, str]], mention: list[GA], buttons: list[tuple[str, str]],
               body_text: str | None = None, style: str = "default") -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True,
             "color": "Attention" if style == "urgent" else "Default"}]
    if mention:
        body.append({"type": "TextBlock", "wrap": True,
                     "text": " ".join(f"<at>{g.name}</at>" for g in mention if g.teams_email)})
    if body_text:
        body.append({"type": "TextBlock", "text": body_text, "wrap": True})
    if lines:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in lines]})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
                "actions": [{"type": "Action.OpenUrl", "title": t, "url": u} for t, u in buttons],
                "msteams": {"width": "Full", "entities": _mention_entities(mention)},
            },
        }],
    }


def post(card: dict) -> bool:
    sent_cards.append(card)
    if not settings.TEAMS_WEBHOOK_URL:
        log.info("TEAMS_WEBHOOK_URL not set; card not sent: %s", card["attachments"][0]["content"]["body"][0]["text"])
        return False
    try:
        r = requests.post(settings.TEAMS_WEBHOOK_URL, json=card, timeout=10)
        if r.status_code >= 300:
            log.warning("Teams webhook returned %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.warning("Teams post failed: %s", e)
        return False
