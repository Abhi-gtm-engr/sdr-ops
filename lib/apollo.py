"""
Apollo helpers for SDR Ops.

CHANGES vs v1:
- get_user_sequence_stats now uses emailer_messages/search with sent_at date
  filter to get TRUE last-N-day stats instead of lifetime campaign totals.
"""
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

BASE = "https://api.apollo.io/api/v1"


def _headers():
    key = os.getenv("APOLLO_API_KEY")
    if not key:
        raise RuntimeError("APOLLO_API_KEY not set in environment")
    return {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": key,
    }


def _post_with_retry(url: str, body: Dict, max_retries: int = 3) -> Dict:
    for attempt in range(max_retries):
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 422:
            return {"_error": "422", "_body": resp.text}
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


def get_user_sequence_stats(
    user_id: str,
    days_back: int = 7,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> Dict:
    """Get per-user email stats over the last N days using emailer_messages.

    This is per-message (not per-campaign-lifetime) so the counts reflect
    actual activity in the window. Paginates up to 5 pages (~500 messages).
    """
    url = f"{BASE}/emailer_messages/search"
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    if start_dt is None:
        start_dt = end_dt - timedelta(days=days_back)
    since = start_dt.strftime("%Y-%m-%d")

    sent = 0
    opens = 0
    replies = 0
    bounces = 0
    meetings = 0

    for page in range(1, 6):  # cap at 5 pages
        body = {
            "user_ids": [user_id],
            "sent_at_range[min]": since,
            "page": page,
            "per_page": 100,
        }
        data = _post_with_retry(url, body)
        if data.get("_error"):
            return {
                "total_sent": 0,
                "total_opens": 0,
                "total_replies": 0,
                "total_meetings": 0,
                "open_rate": 0,
                "reply_rate": 0,
                "error": data.get("_error"),
                "note": "Apollo emailer_messages endpoint error — check tier",
            }
        messages = data.get("emailer_messages", []) or data.get("messages", [])
        if not messages:
            break
        for m in messages:
            sent_at = m.get("sent_at")
            if sent_at:
                try:
                    sent_at_dt = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
                    if not (start_dt <= sent_at_dt <= end_dt):
                        continue
                except (TypeError, ValueError):
                    pass
            # Some messages have status fields; opens/replies are tracked
            if m.get("sent_at") or m.get("status") in ("sent", "delivered", "opened", "replied", "bounced"):
                sent += 1
            if m.get("opened_at") or m.get("opens", 0) > 0 or m.get("is_opened"):
                opens += 1
            if m.get("replied_at") or m.get("replies", 0) > 0 or m.get("is_replied"):
                replies += 1
            if m.get("bounced_at") or m.get("is_bounced"):
                bounces += 1
            if m.get("meeting_booked_at") or m.get("is_meeting_booked"):
                meetings += 1
        if len(messages) < 100:
            break

    return {
        "total_sent": sent,
        "total_opens": opens,
        "total_replies": replies,
        "total_meetings": meetings,
        "total_bounced": bounces,
        "open_rate": round(opens / sent, 3) if sent else 0,
        "reply_rate": round(replies / sent, 3) if sent else 0,
    }


def get_user_call_activities(
    user_id: str,
    days_back: int = 7,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict]:
    """Return phone_calls for the user in the last N days.

    Apollo's phone_calls/search returns newest-first but appears to ignore
    server-side date filters on many tiers, so we filter client-side by
    start_time and stop paginating once we see records outside the window.
    """
    url = f"{BASE}/phone_calls/search"
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    if start_dt is None:
        start_dt = end_dt - timedelta(days=days_back)

    in_window = []
    for page in range(1, 11):  # up to 10 pages = 1000 calls if needed
        body = {
            "user_ids": [user_id],
            "page": page,
            "per_page": 100,
        }
        try:
            data = _post_with_retry(url, body)
        except requests.HTTPError as e:
            print(f"[apollo] phone_calls/search HTTP error: {e}")
            return in_window
        if data.get("_error"):
            print(f"[apollo] phone_calls/search error: {data.get('_error')} — tier-gated?")
            return in_window
        calls = data.get("phone_calls", []) or data.get("calls", [])
        if not calls:
            break

        page_in_window = 0
        all_too_old = True
        for c in calls:
            ts_str = c.get("start_time") or c.get("called_at") or c.get("created_at")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if start_dt <= ts <= end_dt:
                in_window.append(c)
                page_in_window += 1
                all_too_old = False
            # If a call is outside the window we keep scanning the page in case
            # results aren't strictly sorted, but we set all_too_old to track
        # If an entire page returned no in-window records, we're past the window
        if all_too_old and page > 1:
            break
        if len(calls) < 100:
            break

    return in_window


def get_user_call_count(
    user_id: str,
    days_back: int = 7,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> Dict:
    """Convenience wrapper returning just a count + note."""
    activities = get_user_call_activities(user_id, days_back, start_dt=start_dt, end_dt=end_dt)
    return {
        "calls_logged": len(activities),
        "note": "Apollo dialer counts; disposition/duration limited by tier",
    }


def search_organizations_by_domain(domains: List[str]) -> List[Dict]:
    if not domains:
        return []
    url = f"{BASE}/mixed_companies/search"
    body = {
        "q_organization_domains": "\n".join(domains),
        "page": 1,
        "per_page": 100,
    }
    data = _post_with_retry(url, body)
    if data.get("_error"):
        return []
    return data.get("organizations", []) + data.get("accounts", [])


def search_people_by_company_and_titles(
    company_domains: List[str],
    titles: List[str],
    per_page: int = 25,
) -> List[Dict]:
    if not company_domains or not titles:
        return []
    url = f"{BASE}/mixed_people/search"
    body = {
        "person_titles": titles,
        "q_organization_domains": "\n".join(company_domains),
        "page": 1,
        "per_page": per_page,
    }
    data = _post_with_retry(url, body)
    if data.get("_error"):
        return []
    return data.get("people", []) + data.get("contacts", [])


def get_or_create_list(list_name: str) -> Optional[str]:
    list_url = f"{BASE}/labels"
    try:
        resp = requests.get(list_url, headers=_headers(), timeout=30)
        if resp.status_code == 200:
            for label in resp.json().get("labels", []):
                if label.get("name") == list_name:
                    return label.get("id")
    except requests.HTTPError:
        pass
    try:
        create_resp = requests.post(
            list_url,
            headers=_headers(),
            json={"label": {"name": list_name}},
            timeout=30,
        )
        if create_resp.status_code in (200, 201):
            return create_resp.json().get("label", {}).get("id")
    except requests.HTTPError:
        pass
    return None


def add_contacts_to_list(contact_ids: List[str], list_name: str) -> Dict:
    if not contact_ids:
        return {"success": False, "error": "No contact IDs provided"}
    label_id = get_or_create_list(list_name)
    if not label_id:
        return {"success": False, "error": "Could not create or find label", "fallback": "csv"}
    url = f"{BASE}/contacts/add_to_lists"
    body = {"contact_ids": contact_ids, "label_ids": [label_id]}
    try:
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        if resp.status_code in (200, 201):
            return {
                "success": True,
                "label_id": label_id,
                "list_name": list_name,
                "contacts_added": len(contact_ids),
            }
        return {
            "success": False,
            "status_code": resp.status_code,
            "error": resp.text[:500],
            "fallback": "csv",
        }
    except requests.HTTPError as e:
        return {"success": False, "error": str(e), "fallback": "csv"}
