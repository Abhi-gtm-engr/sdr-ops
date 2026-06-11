"""
Debug: Inspect raw call records from HubSpot and Apollo for one SDR.

Run: python -m workflows.debug_call_inspect [sdr_name]
Default SDR: Walter Mejia (highest call volume).
"""
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from lib import hubspot, apollo


def main():
    sdr_users = json.loads(os.getenv("SDR_USERS_JSON", "{}"))

    sdr_name = sys.argv[1] if len(sys.argv) > 1 else "Walter Mejia"
    if sdr_name not in sdr_users:
        print(f"SDR '{sdr_name}' not in SDR_USERS_JSON. Available: {list(sdr_users.keys())}")
        return
    ids = sdr_users[sdr_name]

    print(f"\n=== Inspecting calls for {sdr_name} ===")
    print(f"HubSpot owner: {ids.get('hubspot_owner_id')}")
    print(f"Apollo user:   {ids.get('apollo_user_id')}\n")

    # --- HubSpot side ---
    print("--- HubSpot calls (first 5 records, raw) ---")
    try:
        hs_calls = hubspot.search_engagements_by_owner(ids["hubspot_owner_id"], "CALL", 7)
        print(f"Total HS calls: {len(hs_calls)}\n")
        for i, c in enumerate(hs_calls[:5]):
            props = c.get("properties", {})
            ts_raw = props.get("hs_timestamp")
            ts_human = "N/A"
            if ts_raw:
                ts_str = str(ts_raw).strip()
                try:
                    if "T" in ts_str or "-" in ts_str:
                        ts_human = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).isoformat()
                    else:
                        ts_human = datetime.fromtimestamp(int(ts_str) / 1000, tz=timezone.utc).isoformat()
                except Exception as e:
                    ts_human = f"parse error on {ts_raw}: {e}"
            print(f"[{i}] id={c.get('id')}")
            print(f"    hs_timestamp: {ts_raw} → {ts_human}")
            print(f"    direction: {props.get('hs_call_direction')}, "
                  f"duration: {props.get('hs_call_duration')}, "
                  f"disposition: {props.get('hs_call_disposition')}")
            print()
    except Exception as e:
        print(f"HubSpot error: {e}\n")

    # --- Apollo side ---
    print("--- Apollo phone_calls (first 5 records, raw) ---")
    try:
        ap_calls = apollo.get_user_call_activities(ids["apollo_user_id"], 7)
        print(f"Total Apollo phone_calls: {len(ap_calls)}\n")
        for i, a in enumerate(ap_calls[:5]):
            print(f"[{i}] id={a.get('id')}")
            print(f"    keys: {list(a.keys())}")
            for k in ["called_at", "start_time", "created_at", "completed_at", "call_initiated_at"]:
                if a.get(k):
                    print(f"    {k}: {a[k]}")
            print(f"    duration: {a.get('duration')}, "
                  f"status: {a.get('status')}, "
                  f"outcome_id: {a.get('phone_call_outcome_id')}, "
                  f"to_number: {a.get('to_number') or a.get('phone_number')}")
            print()
    except Exception as e:
        print(f"Apollo error: {e}\n")

    # --- Timestamp range comparison ---
    print("--- Timestamp ranges ---")
    try:
        hs_ts = []
        for c in hs_calls:
            t = c.get("properties", {}).get("hs_timestamp")
            if t:
                t_str = str(t).strip()
                try:
                    if "T" in t_str or "-" in t_str:
                        hs_ts.append(datetime.fromisoformat(t_str.replace("Z", "+00:00")))
                    else:
                        hs_ts.append(datetime.fromtimestamp(int(t_str) / 1000, tz=timezone.utc))
                except Exception:
                    pass
        ap_ts = []
        for a in ap_calls:
            for k in ["called_at", "start_time", "created_at", "completed_at"]:
                v = a.get(k)
                if v:
                    try:
                        ap_ts.append(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
                        break
                    except Exception:
                        pass

        if hs_ts:
            print(f"HubSpot: {len(hs_ts)} parsed · earliest {min(hs_ts)} · latest {max(hs_ts)}")
        else:
            print("HubSpot: NO parsed timestamps (this is the bug)")
        if ap_ts:
            print(f"Apollo:  {len(ap_ts)} parsed · earliest {min(ap_ts)} · latest {max(ap_ts)}")
        else:
            print("Apollo:  NO parsed timestamps (this is the bug)")
    except Exception as e:
        print(f"Timestamp comparison error: {e}")


if __name__ == "__main__":
    main()
