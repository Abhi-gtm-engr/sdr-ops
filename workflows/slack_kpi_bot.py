"""
Slack Socket Mode bot for on-demand KPI queries.

Examples:
  @info kpi for Walter last 7 days
  @info kpi for Nolan this week
  @info kpi for SDRs last month
  @info kpi from 2026-06-01 to 2026-06-10 for Quinn

Requires:
  - SLACK_BOT_TOKEN
  - SLACK_APP_TOKEN (xapp-..., with connections:write)
  - app_mentions:read scope
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from lib import slack
from lib.kpi import build_kpi_report, get_sdr_users


HELP_TEXT = (
    "Try one of these:\n"
    "• `@info kpi for Walter last 7 days`\n"
    "• `@info kpi for Nolan this week`\n"
    "• `@info kpi for SDRs last month`\n"
    "• `@info kpi from 2026-06-01 to 2026-06-10 for Quinn`\n"
    "Supported timeframes: today, yesterday, last N days, this week, last week, this month, last month, or explicit dates."
)


def utc_now():
    return datetime.now(timezone.utc)


def normalize_name(raw: str) -> str:
    return raw.strip().lower().replace("-", " ")


def resolve_targets(text: str):
    sdr_users = get_sdr_users()
    normalized = {normalize_name(name): name for name in sdr_users}
    first_names = {normalize_name(name.split()[0]): name for name in sdr_users}

    if any(token in text for token in ["all sdrs", "sdrs", "team", "everyone", "all"]):
        return None

    match = re.search(r"\bfor\s+([a-z][a-z\-\s]+?)(?:\s+(today|yesterday|last|this|from)\b|$)", text)
    if not match:
        return None

    target_raw = normalize_name(match.group(1))
    if target_raw in normalized:
        return [normalized[target_raw]]
    if target_raw in first_names:
        return [first_names[target_raw]]

    words = target_raw.split()
    if words and words[0] in first_names:
        return [first_names[words[0]]]
    raise ValueError(f"Unknown SDR target '{match.group(1).strip()}'.")


def parse_timeframe(text: str):
    text = text.lower()
    now = utc_now()

    explicit = re.search(r"\bfrom\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})\b", text)
    if explicit:
        start_dt = datetime.fromisoformat(explicit.group(1)).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(explicit.group(2)).replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(seconds=1)
        return start_dt, end_dt

    if "today" in text:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_dt, now
    if "yesterday" in text:
        end_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
        start_dt = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_dt, end_dt

    match = re.search(r"\blast\s+(\d+)\s+days?\b", text)
    if match:
        days = int(match.group(1))
        return now - timedelta(days=days), now

    if "this week" in text:
        start_dt = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start_dt, now

    if "last week" in text:
        this_week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = this_week_start - timedelta(seconds=1)
        start_dt = (this_week_start - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start_dt, end_dt

    if "this month" in text:
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start_dt, now

    if "last month" in text:
        this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = this_month_start - timedelta(seconds=1)
        start_dt = end_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start_dt, end_dt

    return now - timedelta(days=7), now


def parse_request(text: str):
    cleaned = re.sub(r"<@[^>]+>", "", text).strip()
    if "kpi" not in cleaned.lower():
        raise ValueError("No KPI request found.")
    start_dt, end_dt = parse_timeframe(cleaned)
    targets = resolve_targets(cleaned.lower())
    return {
        "start_dt": start_dt,
        "end_dt": end_dt,
        "targets": targets,
        "cleaned": cleaned,
    }


def build_response_blocks(request_text: str):
    parsed = parse_request(request_text)
    report = build_kpi_report(parsed["start_dt"], parsed["end_dt"], parsed["targets"])
    notes = ["On-demand query. Calls are deduplicated across HubSpot and Apollo before reporting."]
    text, blocks = slack.format_kpi_report(report["range_label"], report["per_sdr"], notes=notes)
    return text, blocks, report


def dry_run(text: str):
    msg_text, _, report = build_response_blocks(text)
    print(msg_text)
    print(report["summary"])
    for name, metrics in report["per_sdr"].items():
        print(name, metrics["emails_sent"], metrics["replies"], metrics["calls_logged"], metrics["meetings_booked"])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        sample = " ".join(sys.argv[2:]) or "@info kpi for Walter last 7 days"
        dry_run(sample)
        return

    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required for Slack bot mode")

    app = App(token=bot_token)

    @app.event("app_mention")
    def handle_app_mention(body, say, logger=None):
        event = body.get("event", {})
        print(f"[slack_kpi_bot] app_mention event: channel={event.get('channel')} user={event.get('user')} text={event.get('text')!r}", flush=True)
        text = event.get("text", "")
        thread_ts = event.get("ts")
        channel = event.get("channel")
        try:
            msg_text, blocks, _ = build_response_blocks(text)
            say(text=msg_text, blocks=blocks, thread_ts=thread_ts, channel=channel)
        except Exception as e:
            say(text=f"Could not process KPI request. {e}\n\n{HELP_TEXT}", thread_ts=thread_ts, channel=channel)

    @app.event("message")
    def handle_direct_message(body, say, logger=None):
        event = body.get("event", {})
        if event.get("channel_type") != "im":
            return
        if event.get("subtype"):
            return
        print(f"[slack_kpi_bot] dm event: channel={event.get('channel')} user={event.get('user')} text={event.get('text')!r}", flush=True)
        text = event.get("text", "")
        if "kpi" not in text.lower():
            say(text=HELP_TEXT, channel=event.get("channel"))
            return
        try:
            msg_text, blocks, _ = build_response_blocks(text)
            say(text=msg_text, blocks=blocks, channel=event.get("channel"))
        except Exception as e:
            say(text=f"Could not process KPI request. {e}\n\n{HELP_TEXT}", channel=event.get("channel"))

    print("Slack KPI bot is listening via Socket Mode...")
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
