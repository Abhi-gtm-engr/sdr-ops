"""Slack helpers for SDR Ops."""
import os
import requests
from typing import List, Dict, Optional


def _has_bot_token() -> bool:
    return bool(os.getenv("SLACK_BOT_TOKEN"))


def _has_webhook() -> bool:
    return bool(os.getenv("SLACK_WEBHOOK_URL"))


def post_message(text: str, blocks: Optional[List[Dict]] = None, channel: Optional[str] = None) -> Dict:
    if _has_bot_token():
        return _post_via_bot(text, blocks, channel)
    if _has_webhook():
        return _post_via_webhook(text, blocks)
    return {"ok": False, "error": "No SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL configured"}


def _post_via_bot(text: str, blocks: Optional[List[Dict]], channel: Optional[str]) -> Dict:
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {os.getenv('SLACK_BOT_TOKEN')}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "channel": channel or os.getenv("SLACK_KPI_CHANNEL", "#sdr-ops"),
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    return resp.json()


def _post_via_webhook(text: str, blocks: Optional[List[Dict]]) -> Dict:
    url = os.getenv("SLACK_WEBHOOK_URL")
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    resp = requests.post(url, json=payload, timeout=30)
    return {"ok": resp.status_code == 200, "status": resp.status_code}


def format_kpi_report(week_range: str, per_sdr_metrics: Dict[str, Dict], notes: List[str] = None) -> tuple:
    """Build a clean KPI payload for SDRs and leadership."""
    fallback = f"SDR KPI Report — {week_range}"
    total_emails = sum(m.get("emails_sent", 0) for m in per_sdr_metrics.values())
    total_replies = sum(m.get("replies", 0) for m in per_sdr_metrics.values())
    total_calls = sum(m.get("calls_logged", 0) for m in per_sdr_metrics.values())
    total_meetings = sum(m.get("meetings_booked", 0) for m in per_sdr_metrics.values())
    total_reply_rate = (total_replies / total_emails) if total_emails else 0

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📊 SDR Weekly KPI Report"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_{week_range}_"}]},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Team Totals*\n"
                    f"📧 `{total_emails}` emails  ·  "
                    f"↩️ `{total_replies}` replies ({total_reply_rate:.1%})  ·  "
                    f"📞 `{total_calls}` calls  ·  "
                    f"📅 `{total_meetings}` meetings"
                ),
            },
        },
        {"type": "divider"},
    ]

    for sdr, m in per_sdr_metrics.items():
        sent = m.get("emails_sent", 0)
        opens = m.get("opens", 0)
        replies = m.get("replies", 0)
        calls = m.get("calls_logged", 0)
        meetings = m.get("meetings_booked", 0)
        open_rate = m.get("open_rate", 0)
        reply_rate = m.get("reply_rate", 0)
        connect_rate = m.get("connect_rate")

        text_md = (
            f"*{sdr}*\n"
            f"📧 Sent: `{sent}`  ·  "
            f"↩️ Replies: `{replies}` ({reply_rate:.1%})"
        )
        if opens is not None and open_rate is not None:
            text_md += f"  ·  👀 Opens: `{opens}` ({open_rate:.1%})"
        text_md += (
            "\n"
            f"📞 Calls: `{calls}`"
        )
        if connect_rate is not None:
            text_md += f"  ·  🤝 Connect Rate: `{connect_rate:.1%}`"
        text_md += f"  ·  📅 Meetings: `{meetings}`"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text_md}})

    if notes:
        blocks.append({"type": "divider"})
        notes_text = "*Notes:*\n" + "\n".join(f"• {n}" for n in notes)
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": notes_text}],
        })

    return fallback, blocks


def format_prospect_summary(list_name: str, count: int, sample: List[Dict], apollo_list_url: Optional[str] = None) -> tuple:
    fallback = f"{list_name}: {count} prospects"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎯 {list_name}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{count}* prospects added"}},
    ]
    if apollo_list_url:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"<{apollo_list_url}|Open in Apollo →>"}})
    if sample:
        sample_text = "\n".join(
            f"• {p.get('name', 'Unknown')} — {p.get('title', '')} @ {p.get('company', '')}"
            for p in sample[:5]
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Sample:*\n{sample_text}"}})
    return fallback, blocks
