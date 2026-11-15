"""Out-of-band alerts.

Two kinds of message leave the system without going through a pull request:
pages for escalated (critical) reviews, and health alerts when the human
queue or the escalation rate says the agent needs attention. A critical
security finding is announced here, privately, and never in a public thread
until a human decides it is safe to do so.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.security.secret_masking import mask

log = logging.getLogger("alerts")


class Alerter:
    def __init__(self, slack_webhook_url: str = "", client: httpx.AsyncClient | None = None) -> None:
        self._url = slack_webhook_url
        self._client = client
        self.sent: list[dict[str, Any]] = []  # kept for tests and the /queue/status endpoint

    async def notify(self, title: str, text: str, severity: str = "info", **fields: Any) -> None:
        message = {"title": title, "text": mask(text), "severity": severity, "fields": fields}
        self.sent.append(message)
        del self.sent[:-50]
        log.warning("ALERT [%s] %s: %s %s", severity, title, message["text"], fields or "")
        if not self._url:
            return
        body = {"text": f"*[{severity.upper()}] {title}*\n{message['text']}"}
        if fields:
            body["text"] += "\n" + "\n".join(f"• *{k}*: {v}" for k, v in fields.items())
        try:
            client = self._client or httpx.AsyncClient(timeout=5.0)
            try:
                await client.post(self._url, json=body)
            finally:
                if self._client is None:
                    await client.aclose()
        except Exception as exc:  # noqa: BLE001 - an alert failing must not fail the review
            log.error("alert delivery failed: %s", exc)
