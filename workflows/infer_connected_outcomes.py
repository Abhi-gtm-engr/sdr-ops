"""
Infer likely Apollo "connected" outcome IDs from recent call behavior.

Use this when Apollo UI access is unavailable. It groups recent phone calls by
phone_call_outcome_id and shows counts, statuses, average duration, and sample
rows so you can identify which IDs are likely the connected outcomes.

Heuristic guidance:
- Connected outcomes usually have `status=completed`
- They usually have non-trivial duration
- Busy / no-answer / voicemail outcomes often have short or zero duration

Run:
  python -m workflows.infer_connected_outcomes
  python -m workflows.infer_connected_outcomes "Walter Mejia"
"""
import os
import sys
import json
from collections import defaultdict
from statistics import mean
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from lib import apollo


def iter_target_sdrs():
    sdr_users = json.loads(os.getenv("SDR_USERS_JSON", "{}"))
    if len(sys.argv) > 1:
        name = sys.argv[1]
        if name not in sdr_users:
            raise SystemExit(f"SDR '{name}' not in SDR_USERS_JSON. Available: {list(sdr_users.keys())}")
        return [(name, sdr_users[name])]
    return list(sdr_users.items())


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def summarize_calls():
    by_outcome = defaultdict(lambda: {
        "count": 0,
        "durations": [],
        "statuses": defaultdict(int),
        "sdrs": defaultdict(int),
        "samples": [],
    })

    total_calls = 0
    for sdr_name, ids in iter_target_sdrs():
        apollo_user_id = ids.get("apollo_user_id")
        if not apollo_user_id:
            continue
        calls = apollo.get_user_call_activities(apollo_user_id, days_back=14)
        total_calls += len(calls)
        for call in calls:
            outcome_id = call.get("phone_call_outcome_id") or "NONE"
            row = by_outcome[outcome_id]
            row["count"] += 1
            row["sdrs"][sdr_name] += 1
            status = call.get("status") or "NONE"
            row["statuses"][status] += 1
            duration = safe_int(call.get("duration"))
            if duration is not None:
                row["durations"].append(duration)
            if len(row["samples"]) < 3:
                row["samples"].append({
                    "sdr": sdr_name,
                    "status": status,
                    "duration": duration,
                    "started_at": call.get("start_time") or call.get("called_at") or call.get("created_at"),
                    "hubspot_id": call.get("hubspot_id"),
                    "to_number": call.get("to_number") or call.get("phone_number"),
                })
    return total_calls, by_outcome


def main():
    total_calls, by_outcome = summarize_calls()
    print("\n" + "=" * 72)
    print("APOLLO OUTCOME INFERENCE")
    print("=" * 72)
    print(f"Total Apollo calls scanned (last 14 days): {total_calls}\n")

    rows = []
    for outcome_id, data in by_outcome.items():
        avg_duration = round(mean(data["durations"]), 1) if data["durations"] else None
        max_duration = max(data["durations"]) if data["durations"] else None
        completed = data["statuses"].get("completed", 0)
        busy = data["statuses"].get("busy", 0)
        rows.append({
            "outcome_id": outcome_id,
            "count": data["count"],
            "avg_duration": avg_duration,
            "max_duration": max_duration,
            "completed": completed,
            "busy": busy,
            "statuses": dict(sorted(data["statuses"].items(), key=lambda kv: (-kv[1], kv[0]))),
            "sdrs": dict(sorted(data["sdrs"].items(), key=lambda kv: (-kv[1], kv[0]))),
            "samples": data["samples"],
        })

    rows.sort(key=lambda r: (-r["count"], -(r["avg_duration"] or 0), r["outcome_id"]))

    for row in rows:
        print(f"Outcome ID: {row['outcome_id']}")
        print(f"  Count: {row['count']}")
        print(f"  Avg duration: {row['avg_duration']}")
        print(f"  Max duration: {row['max_duration']}")
        print(f"  Statuses: {row['statuses']}")
        print(f"  SDRs: {row['sdrs']}")
        for sample in row["samples"]:
            print(f"  Sample: {sample}")
        print()

    print("=" * 72)
    print("HOW TO USE THIS")
    print("=" * 72)
    print(
        "Pick the 2-3 outcome IDs that look like real human connects:\n"
        "- high completed count\n"
        "- average duration well above 0\n"
        "- representative samples with real talk time\n\n"
        "Then set APOLLO_CONNECTED_OUTCOME_IDS in .env with those IDs."
    )


if __name__ == "__main__":
    main()
