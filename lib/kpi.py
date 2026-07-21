"""Reusable KPI helpers for scheduled jobs and on-demand Slack queries."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import apollo, hubspot

OUTPUTS_DIR = Path(__file__).resolve().parent.parent / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def get_sdr_users() -> dict:
    raw = os.getenv("SDR_USERS_JSON", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# Apollo "connected" outcomes = the SDR reached and had a live conversation with a
# person. Verified 2026-07-21 from call notes: 805845/846/847 are real conversations
# ("tt <name>…"), whereas the previously-counted 805840 (voicemail / "gk to vm") and
# 805842 (gatekeeper — "gk went and checked, she wasn't available") are NOT connects.
# NOTE: prod injects APOLLO_CONNECTED_OUTCOME_IDS from a GH secret which OVERRIDES this
# default — update that secret to the corrected value for the fix to take effect live.
DEFAULT_APOLLO_CONNECTED_OUTCOME_IDS = (
    "5ef42fb20c815e008c805845,5ef42fb20c815e008c805846,5ef42fb20c815e008c805847"
)


def get_connected_apollo_outcome_ids() -> set:
    raw = os.getenv("APOLLO_CONNECTED_OUTCOME_IDS", DEFAULT_APOLLO_CONNECTED_OUTCOME_IDS)
    return {part.strip() for part in raw.split(",") if part.strip()}


def get_connected_hubspot_disposition_ids() -> set:
    raw = os.getenv("HUBSPOT_CONNECTED_DISPOSITION_IDS", "f240bbac-87c9-4f6e-bf70-924b57d47db7")
    return {part.strip() for part in raw.split(",") if part.strip()}


def parse_hs_timestamp(call_record: dict):
    ts = call_record.get("properties", {}).get("hs_timestamp")
    if not ts:
        return None
    ts = str(ts).strip()
    if "T" in ts or "-" in ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_apollo_timestamp(call: dict):
    for key in ["called_at", "start_time", "created_at", "completed_at", "call_initiated_at"]:
        ts = call.get(key)
        if ts:
            try:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
    return None


def match_call_records(hs_calls: list, apollo_calls: list, window_seconds: int = 120):
    hs_with_times = []
    for call in hs_calls:
        ts = parse_hs_timestamp(call)
        if ts is not None:
            hs_with_times.append((call, ts))

    matched_hs_indices = set()
    matches = []
    unmatched_apollo = []

    for apollo_call in apollo_calls:
        apollo_ts = parse_apollo_timestamp(apollo_call)
        if apollo_ts is None:
            unmatched_apollo.append(apollo_call)
            continue

        best_idx = None
        best_delta = window_seconds + 1
        for idx, (_, hs_ts) in enumerate(hs_with_times):
            if idx in matched_hs_indices:
                continue
            delta = abs((apollo_ts - hs_ts).total_seconds())
            if delta <= window_seconds and delta < best_delta:
                best_idx = idx
                best_delta = delta

        if best_idx is None:
            unmatched_apollo.append(apollo_call)
        else:
            matched_hs_indices.add(best_idx)
            matches.append((hs_with_times[best_idx][0], apollo_call))

    unmatched_hs = [call for idx, (call, _) in enumerate(hs_with_times) if idx not in matched_hs_indices]
    return {
        "matches": matches,
        "unmatched_hs": unmatched_hs,
        "unmatched_apollo": unmatched_apollo,
    }


def normalize_meeting_title(title: str) -> str:
    title = (title or "").strip()
    if title.lower().startswith("[gong]"):
        title = title[6:].strip()
    return " ".join(title.split()).lower()


def dedupe_hubspot_meetings(meetings: list[dict]) -> list[dict]:
    grouped = {}
    for meeting in meetings:
        props = meeting.get("properties", {})
        ts = props.get("hs_timestamp") or ""
        title = props.get("hs_meeting_title") or ""
        key = (ts, normalize_meeting_title(title))
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = meeting
            continue
        existing_title = (existing.get("properties", {}).get("hs_meeting_title") or "").strip()
        current_is_gong = title.strip().lower().startswith("[gong]")
        existing_is_gong = existing_title.lower().startswith("[gong]")
        if existing_is_gong and not current_is_gong:
            grouped[key] = meeting
    return list(grouped.values())


def is_hubspot_connected_call(call: dict) -> bool:
    props = call.get("properties", {})
    disposition = (props.get("hs_call_disposition") or "").strip()
    body = (props.get("hs_call_body") or "").lower()
    connected_disposition_ids = get_connected_hubspot_disposition_ids()
    if disposition and disposition in connected_disposition_ids:
        return True
    return any(
        token in body
        for token in ["connected - positive", "connected - neutral", "connected - negative"]
    )


def is_apollo_connected_call(call: dict) -> bool:
    outcome_id = (call.get("phone_call_outcome_id") or "").strip()
    return outcome_id in get_connected_apollo_outcome_ids()


def get_connect_min_duration() -> int:
    """A real connect must be a live conversation of at least this many seconds.
    Filters out voicemails and quick gatekeeper brush-offs that get logged under a
    'connected' disposition. Set CONNECT_MIN_DURATION_SECONDS=0 to disable."""
    try:
        return int(os.getenv("CONNECT_MIN_DURATION_SECONDS", "30"))
    except (TypeError, ValueError):
        return 30


def _hs_call_seconds(call: dict) -> float:
    try:
        return float(call.get("properties", {}).get("hs_call_duration") or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _apollo_call_seconds(call: dict) -> float:
    for key in ("duration", "call_duration", "duration_seconds"):
        val = call.get(key)
        if val not in (None, ""):
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def is_quality_connect(hs_call: dict | None = None, apollo_call: dict | None = None) -> bool:
    """True only if a call is BOTH dispositioned connected AND a genuine live
    conversation: meets the minimum-duration floor and is not a dropped voicemail.
    Guards against the connect rate counting voicemails / gatekeeper-to-VM."""
    connected = (hs_call is not None and is_hubspot_connected_call(hs_call)) or (
        apollo_call is not None and is_apollo_connected_call(apollo_call)
    )
    if not connected:
        return False
    if apollo_call is not None and apollo_call.get("voicemail_dropped"):
        return False
    floor = get_connect_min_duration()
    if floor <= 0:
        return True
    seconds = max(_hs_call_seconds(hs_call) if hs_call else 0.0,
                  _apollo_call_seconds(apollo_call) if apollo_call else 0.0)
    return seconds >= floor


def format_week_range(start_dt: datetime, end_dt: datetime) -> str:
    return f"{start_dt.strftime('%b %d')} – {end_dt.strftime('%b %d, %Y')}"


def collect_sdr_metrics(name: str, ids: dict, start_dt: datetime, end_dt: datetime) -> dict:
    hubspot_owner_id = ids.get("hubspot_owner_id")
    apollo_user_id = ids.get("apollo_user_id")

    m = {
        "sdr": name,
        "emails_sent_hs": 0,
        "emails_sent_apollo": 0,
        "opens_hs": None,
        "opens_apollo": None,
        "replies_hs": 0,
        "replies_apollo": 0,
        "calls_hs": 0,
        "calls_apollo": 0,
        "calls_logged": 0,
        "call_dupes": 0,
        "connected_calls": None,
        "connect_rate": None,
        "meetings_hs": 0,
        "meetings_apollo": 0,
        "notes": [],
    }
    hs_calls = []
    apollo_calls = []

    if hubspot_owner_id:
        try:
            emails = hubspot.search_engagements_by_owner(hubspot_owner_id, "EMAIL", start_dt=start_dt, end_dt=end_dt)
            agg = hubspot.aggregate_email_metrics(emails)
            m["emails_sent_hs"] = agg["emails_sent"]
            m["replies_hs"] = agg["inbound_replies"]
        except Exception as e:
            m["notes"].append(f"HubSpot emails error: {e}")

        try:
            hs_calls = hubspot.search_engagements_by_owner(hubspot_owner_id, "CALL", start_dt=start_dt, end_dt=end_dt)
            agg = hubspot.aggregate_call_metrics(hs_calls)
            m["calls_hs"] = agg["calls_logged"]
        except Exception as e:
            m["notes"].append(f"HubSpot calls error: {e}")

        try:
            meetings = hubspot.search_engagements_by_owner(hubspot_owner_id, "MEETING", start_dt=start_dt, end_dt=end_dt)
            deduped_meetings = dedupe_hubspot_meetings(meetings)
            agg = hubspot.aggregate_meeting_metrics(deduped_meetings)
            m["meetings_hs"] = agg["meetings_booked"]
            removed = len(meetings) - len(deduped_meetings)
            if removed > 0:
                m["notes"].append(f"Meeting duplicates removed: {removed}")
        except Exception as e:
            m["notes"].append(f"HubSpot meetings error: {e}")
    else:
        m["notes"].append("No HubSpot owner_id configured")

    if apollo_user_id:
        try:
            seq = apollo.get_user_sequence_stats(apollo_user_id, start_dt=start_dt, end_dt=end_dt)
            m["emails_sent_apollo"] = seq.get("total_sent", 0)
            opens = seq.get("total_opens", 0)
            m["opens_apollo"] = opens if opens else None
            m["replies_apollo"] = seq.get("total_replies", 0)
            m["meetings_apollo"] = seq.get("total_meetings", 0)
            if seq.get("note"):
                m["notes"].append(seq["note"])
        except Exception as e:
            m["notes"].append(f"Apollo sequence error: {e}")

        try:
            apollo_calls = apollo.get_user_call_activities(apollo_user_id, start_dt=start_dt, end_dt=end_dt)
            m["calls_apollo"] = len(apollo_calls)
        except Exception as e:
            m["notes"].append(f"Apollo calls error: {e}")
    else:
        m["notes"].append("No Apollo user_id configured")

    call_match = match_call_records(hs_calls, apollo_calls)
    m["call_dupes"] = len(call_match["matches"])
    m["calls_logged"] = len(call_match["matches"]) + len(call_match["unmatched_hs"]) + len(call_match["unmatched_apollo"])

    connected_matches = sum(
        1
        for hs_call, apollo_call in call_match["matches"]
        if is_quality_connect(hs_call, apollo_call)
    )
    connected_unmatched_hs = sum(1 for hs_call in call_match["unmatched_hs"] if is_quality_connect(hs_call=hs_call))
    connected_unmatched_apollo = sum(1 for apollo_call in call_match["unmatched_apollo"] if is_quality_connect(apollo_call=apollo_call))
    connected_total = connected_matches + connected_unmatched_hs + connected_unmatched_apollo
    if connected_total > 0 or m["calls_logged"] > 0:
        m["connected_calls"] = connected_total
        m["connect_rate"] = round(m["connected_calls"] / m["calls_logged"], 3) if m["calls_logged"] else 0
        floor = get_connect_min_duration()
        m["notes"].append(
            f"Connect = connected disposition AND live conversation ≥{floor}s (excludes voicemails / gatekeeper-to-VM)"
        )

    m["emails_sent"] = m["emails_sent_hs"]
    m["opens"] = m["opens_apollo"]
    m["replies"] = m["replies_hs"]
    m["meetings_booked"] = m["meetings_hs"]
    m["open_rate"] = round(m["opens"] / m["emails_sent"], 3) if (m["opens"] is not None and m["emails_sent"]) else None
    m["reply_rate"] = round(m["replies"] / m["emails_sent"], 3) if m["emails_sent"] else 0
    m["notes"].append("Open rate hidden unless a trustworthy open-tracking source is available")
    return m


def build_kpi_report(start_dt: datetime, end_dt: datetime, target_sdrs: list[str] | None = None) -> dict:
    sdr_users = get_sdr_users()
    if not sdr_users:
        raise RuntimeError("SDR_USERS_JSON not configured")

    if target_sdrs:
        normalized = {name.lower(): name for name in sdr_users}
        selected = {}
        for requested in target_sdrs:
            match = normalized.get(requested.lower())
            if not match:
                raise ValueError(f"Unknown SDR '{requested}'. Available: {list(sdr_users.keys())}")
            selected[match] = sdr_users[match]
        sdr_users = selected

    per_sdr = {name: collect_sdr_metrics(name, ids, start_dt, end_dt) for name, ids in sdr_users.items()}
    return {
        "start_dt": start_dt,
        "end_dt": end_dt,
        "range_label": format_week_range(start_dt, end_dt),
        "per_sdr": per_sdr,
        "summary": {
            "sdr_count": len(per_sdr),
            "total_emails_sent": sum(mx["emails_sent"] for mx in per_sdr.values()),
            "total_replies": sum(mx["replies"] for mx in per_sdr.values()),
            "total_calls": sum(mx["calls_logged"] for mx in per_sdr.values()),
            "total_meetings": sum(mx["meetings_booked"] for mx in per_sdr.values()),
        },
    }
