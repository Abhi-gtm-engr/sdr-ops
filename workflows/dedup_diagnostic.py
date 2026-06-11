"""
Deduplication Diagnostic

For each SDR, pulls calls from HubSpot AND Apollo over the last N days,
then matches them by timestamp proximity (±2 minutes by default) to estimate
how many are likely duplicates.

This helps answer "is our raw sum overcounting?" without manually checking
records one by one.

Caveats:
- Heuristic, not perfect. Real back-to-back calls within 2 min for the same
  user will look like duplicates. Apollo dialer sync delay >2 min will miss
  duplicates. Tune MATCH_WINDOW_SECONDS to fit reality.
- Currently only checks CALLS (the biggest overlap surface). Email overlap is
  harder to dedup deterministically because Apollo creates HubSpot email
  engagement records with different IDs.

Run: python -m workflows.dedup_diagnostic
"""
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from lib import hubspot, apollo, slack, logger

MATCH_WINDOW_SECONDS = 120  # ±2 minutes
DAYS_BACK = 7


def get_sdr_users() -> dict:
    raw = os.getenv("SDR_USERS_JSON", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def parse_hs_timestamp(call_record: dict):
    """HubSpot returns timestamps either as ISO strings (newer) or ms-since-epoch (older).
    Handle both."""
    ts = call_record.get("properties", {}).get("hs_timestamp")
    if not ts:
        return None
    ts = str(ts).strip()
    # Try ISO first
    if "T" in ts or "-" in ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
    # Fall back to ms-since-epoch
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_apollo_timestamp(call: dict):
    """Apollo phone_calls return ISO strings. Try multiple field names."""
    for k in ["called_at", "start_time", "created_at", "completed_at", "call_initiated_at"]:
        ts = call.get(k)
        if ts:
            try:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
    return None


def match_calls(hs_times, apollo_times, window_seconds=MATCH_WINDOW_SECONDS):
    """Greedy match: for each Apollo timestamp, find an unmatched HS timestamp
    within the window. Returns the count of matches."""
    matched_hs_indices = set()
    matches = 0
    for at in apollo_times:
        if at is None:
            continue
        best_idx = None
        best_delta = window_seconds + 1
        for i, ht in enumerate(hs_times):
            if i in matched_hs_indices or ht is None:
                continue
            delta = abs((at - ht).total_seconds())
            if delta <= window_seconds and delta < best_delta:
                best_idx = i
                best_delta = delta
        if best_idx is not None:
            matched_hs_indices.add(best_idx)
            matches += 1
    return matches


def diagnose_sdr(name: str, ids: dict, days_back: int = DAYS_BACK) -> dict:
    hs_id = ids.get("hubspot_owner_id")
    ap_id = ids.get("apollo_user_id")

    result = {
        "sdr": name,
        "hs_calls": 0,
        "apollo_calls": 0,
        "likely_duplicates": 0,
        "unique_estimate": 0,
        "raw_sum": 0,
        "overcount_pct": 0,
        "errors": [],
    }

    hs_calls = []
    apollo_calls = []

    if hs_id:
        try:
            hs_calls = hubspot.search_engagements_by_owner(hs_id, "CALL", days_back)
        except Exception as e:
            result["errors"].append(f"HubSpot: {e}")
    if ap_id:
        try:
            apollo_calls = apollo.get_user_call_activities(ap_id, days_back)
        except Exception as e:
            result["errors"].append(f"Apollo: {e}")

    hs_times = [parse_hs_timestamp(c) for c in hs_calls]
    apollo_times = [parse_apollo_timestamp(a) for a in apollo_calls]
    hs_times = [t for t in hs_times if t is not None]
    apollo_times = [t for t in apollo_times if t is not None]

    matches = match_calls(hs_times, apollo_times)

    result["hs_calls"] = len(hs_calls)
    result["apollo_calls"] = len(apollo_calls)
    result["likely_duplicates"] = matches
    result["raw_sum"] = len(hs_calls) + len(apollo_calls)
    result["unique_estimate"] = result["raw_sum"] - matches
    if result["raw_sum"]:
        result["overcount_pct"] = round((matches / result["raw_sum"]) * 100, 1)
    return result


def main():
    run = logger.start_run("dedup_diagnostic")
    print(f"[dedup] Starting at {run['started_at']}")
    print(f"[dedup] Window: last {DAYS_BACK} days · match window: ±{MATCH_WINDOW_SECONDS}s\n")

    sdr_users = get_sdr_users()
    if not sdr_users:
        raise RuntimeError("SDR_USERS_JSON not configured")

    print("=" * 70)
    print(f"{'SDR':<22}{'HS':>6}{'Apollo':>8}{'Dupes':>8}{'Unique':>9}{'Raw':>6}{'Over%':>8}")
    print("=" * 70)

    all_results = []
    for name, ids in sdr_users.items():
        r = diagnose_sdr(name, ids)
        all_results.append(r)
        print(
            f"{r['sdr']:<22}"
            f"{r['hs_calls']:>6}"
            f"{r['apollo_calls']:>8}"
            f"{r['likely_duplicates']:>8}"
            f"{r['unique_estimate']:>9}"
            f"{r['raw_sum']:>6}"
            f"{r['overcount_pct']:>7}%"
        )
        if r["errors"]:
            for e in r["errors"]:
                print(f"  ⚠ {e}")

    print("=" * 70)
    print()
    print("Interpretation:")
    print("  HS      = HubSpot calls logged for this owner")
    print("  Apollo  = Apollo activities (type=call) for this user")
    print("  Dupes   = Pairs within ±2 minutes (likely the same call in both systems)")
    print("  Unique  = Raw sum minus likely duplicates")
    print("  Over%   = What % of raw sum is duplication")
    print()
    print("If Over% is high for an SDR who uses Apollo dialer, the KPI report's")
    print("'calls_logged' is inflated. Use the per-source breakdown to decide which")
    print("system to trust for that SDR.")
    print()

    # Optional Slack post (normally off; this is an internal debugging tool)
    if os.getenv("DEDUP_POST_TO_SLACK", "false").lower() == "true":
        lines = []
        for r in all_results:
            lines.append(
                f"*{r['sdr']}*: HS `{r['hs_calls']}` + Apollo `{r['apollo_calls']}` "
                f"= raw `{r['raw_sum']}`, dupes `{r['likely_duplicates']}`, "
                f"unique `{r['unique_estimate']}` ({r['overcount_pct']}% over)"
            )
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "🔍 Call Deduplication Diagnostic"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Last {DAYS_BACK} days · ±{MATCH_WINDOW_SECONDS}s match window_"}]},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ]
        slack_result = slack.post_message(
            "Call dedup diagnostic", blocks=blocks, channel=os.getenv("SLACK_KPI_CHANNEL")
        )
        print(f"[dedup] Slack result: {slack_result}")

    run["metrics"] = {
        "sdr_count": len(all_results),
        "results": all_results,
    }
    log_path = logger.finish_run(run, "success", f"{len(all_results)} SDRs analyzed")
    print(f"[dedup] ✓ Done. Log: {log_path}")


if __name__ == "__main__":
    main()
