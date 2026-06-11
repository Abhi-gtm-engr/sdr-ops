"""
HubSpot helpers for SDR Ops.

Endpoints used:
- /crm/v3/objects/{emails,meetings,calls}/search  for engagement counts per owner
- /crm/v3/objects/companies/search  for customers and recently-engaged exclusion lists
"""
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

BASE = "https://api.hubapi.com"


def _headers():
    key = os.getenv("HUBSPOT_API_KEY")
    if not key:
        raise RuntimeError("HUBSPOT_API_KEY not set in environment")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _post_with_retry(url: str, body: Dict, max_retries: int = 3) -> Dict:
    """POST with retry on 429 and 5xx."""
    for attempt in range(max_retries):
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


def search_engagements_by_owner(
    owner_id: str,
    engagement_type: str,  # "EMAIL", "MEETING", "CALL"
    days_back: int = 7,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict]:
    """Search HubSpot engagements for a specific owner in the last N days."""
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    if start_dt is None:
        start_dt = end_dt - timedelta(days=days_back)

    since_ms = int(start_dt.timestamp() * 1000)
    until_ms = int(end_dt.timestamp() * 1000)

    type_map = {"EMAIL": "emails", "MEETING": "meetings", "CALL": "calls"}
    object_type = type_map.get(engagement_type.upper())
    if not object_type:
        raise ValueError(f"Unknown engagement type: {engagement_type}")

    prop_map = {
        "EMAIL": [
            "hs_email_direction",
            "hs_email_status",
            "hs_timestamp",
            "hubspot_owner_id",
            "hs_email_tracker_key",
        ],
        "MEETING": [
            "hs_meeting_title",
            "hs_meeting_outcome",
            "hs_timestamp",
            "hubspot_owner_id",
        ],
        "CALL": [
            "hs_call_direction",
            "hs_call_disposition",
            "hs_call_duration",
            "hs_timestamp",
            "hubspot_owner_id",
        ],
    }

    url = f"{BASE}/crm/v3/objects/{object_type}/search"
    results = []
    after = None

    while True:
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hubspot_owner_id",
                            "operator": "EQ",
                            "value": owner_id,
                        },
                        {
                            "propertyName": "hs_timestamp",
                            "operator": "GTE",
                            "value": str(since_ms),
                        },
                    ]
                }
            ],
            "properties": prop_map[engagement_type.upper()],
            "limit": 100,
        }
        if after:
            body["after"] = after

        data = _post_with_retry(url, body)
        for record in data.get("results", []):
            ts_raw = record.get("properties", {}).get("hs_timestamp")
            if not ts_raw:
                results.append(record)
                continue
            ts_value = None
            ts_str = str(ts_raw).strip()
            if "T" in ts_str or "-" in ts_str:
                try:
                    ts_value = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    pass
            if ts_value is None:
                try:
                    ts_value = datetime.fromtimestamp(int(ts_str) / 1000, tz=timezone.utc)
                except (TypeError, ValueError):
                    pass
            if ts_value is None or start_dt <= ts_value <= end_dt:
                results.append(record)

        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    return results


def aggregate_email_metrics(emails: List[Dict]) -> Dict:
    """Aggregate raw email engagements into metrics.

    NOTE: HubSpot's standard CRM email object tracks send/status but
    open/click events live in the email events API. For v1 we count
    sends from direction=outbound; opens/replies need follow-up if
    your account exposes the email events endpoint.
    """
    sent = 0
    replied = 0
    bounced = 0
    for e in emails:
        props = e.get("properties", {})
        direction = (props.get("hs_email_direction") or "").upper()
        status = (props.get("hs_email_status") or "").upper()
        if direction == "EMAIL":
            sent += 1
        if status == "BOUNCED":
            bounced += 1
        # Replies typically tracked as inbound emails associated to threads
    inbound_replies = sum(
        1
        for e in emails
        if (e.get("properties", {}).get("hs_email_direction") or "").upper()
        == "INCOMING_EMAIL"
    )
    return {
        "emails_sent": sent,
        "emails_bounced": bounced,
        "inbound_replies": inbound_replies,
    }


def aggregate_call_metrics(calls: List[Dict]) -> Dict:
    total = len(calls)
    connected = 0
    total_duration_ms = 0
    for c in calls:
        props = c.get("properties", {})
        disp = (props.get("hs_call_disposition") or "").lower()
        if "connected" in disp or "answered" in disp:
            connected += 1
        try:
            total_duration_ms += int(props.get("hs_call_duration") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "calls_logged": total,
        "calls_connected": connected,
        "total_call_minutes": round(total_duration_ms / 60000, 1),
    }


def aggregate_meeting_metrics(meetings: List[Dict]) -> Dict:
    booked = 0
    completed = 0
    no_show = 0
    for m in meetings:
        props = m.get("properties", {})
        outcome = (props.get("hs_meeting_outcome") or "").lower()
        booked += 1
        if "completed" in outcome:
            completed += 1
        if "no_show" in outcome or "noshow" in outcome:
            no_show += 1
    return {
        "meetings_booked": booked,
        "meetings_completed": completed,
        "meetings_no_show": no_show,
    }


def get_customer_companies() -> List[Dict]:
    """All companies with lifecyclestage = customer."""
    url = f"{BASE}/crm/v3/objects/companies/search"
    results = []
    after = None

    while True:
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "lifecyclestage",
                            "operator": "EQ",
                            "value": "customer",
                        }
                    ]
                }
            ],
            "properties": [
                "name",
                "domain",
                "city",
                "state",
                "country",
                "numberofemployees",
                "annualrevenue",
                "industry",
            ],
            "limit": 100,
        }
        if after:
            body["after"] = after

        data = _post_with_retry(url, body)
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    return results


def get_recently_engaged_company_domains(days_back: int = 180) -> set:
    """Domains of companies with any engagement (notes_last_contacted) within window.

    Returns a set for fast exclusion lookups.
    """
    since_ms = int((datetime.utcnow() - timedelta(days=days_back)).timestamp() * 1000)
    url = f"{BASE}/crm/v3/objects/companies/search"
    domains = set()
    after = None

    while True:
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "notes_last_contacted",
                            "operator": "GTE",
                            "value": str(since_ms),
                        }
                    ]
                }
            ],
            "properties": ["domain", "notes_last_contacted"],
            "limit": 100,
        }
        if after:
            body["after"] = after

        data = _post_with_retry(url, body)
        for r in data.get("results", []):
            d = r.get("properties", {}).get("domain")
            if d:
                domains.add(d.lower().strip())

        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    return domains
