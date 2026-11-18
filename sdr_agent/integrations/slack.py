"""Post a finished draft to Slack for human review (incoming webhook)."""

from __future__ import annotations

import httpx

from ..models import SDRResult


def build_payload(result: SDRResult) -> dict:
    p, brief, final = result.prospect, result.brief, result.final
    status = ":white_check_mark: Ready to send" if result.approved else ":warning: Needs human edit"
    triggers = "\n".join(f"• {t.summary} (<{t.source_url}|source>)" for t in brief.triggers) or "• none found"
    name = f"{p.first_name} {p.last_name}".strip()

    return {
        "text": f"New SDR draft: {name} @ {brief.company_name}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{name} · {brief.company_name}"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Title*\n{p.title}"},
                    {"type": "mrkdwn", "text": f"*ICP fit*\n{brief.icp_fit_score}/100"},
                    {"type": "mrkdwn", "text": f"*Grade*\n{final.grade.overall:.1f}/5 ({len(result.attempts)} attempt(s))"},
                    {"type": "mrkdwn", "text": f"*Status*\n{status}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why now*\n{triggers}"}},
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Subject:* {final.draft.subject}\n\n{final.draft.body}",
                },
            },
        ],
    }


def post(result: SDRResult, webhook_url: str, client: httpx.Client | None = None) -> None:
    client = client or httpx.Client(timeout=15)
    resp = client.post(webhook_url, json=build_payload(result))
    resp.raise_for_status()
