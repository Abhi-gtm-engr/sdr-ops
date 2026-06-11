"""
Call Outcome / Disposition Lookup

Prints all available call outcome labels and their IDs from both
Apollo and HubSpot. Run this once to identify which IDs correspond
to Walter's three "Connected" outcomes:
  - Connected - Positive
  - Connected - Neutral
  - Connected - Negative

Once you have the IDs, paste them back to Claude and we'll hardcode
them into the KPI workflow for connect rate calculation.

Run: python -m workflows.inspect_call_outcomes
"""
import os
import sys
import json
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()


def get_apollo_outcomes():
    """Fetch call outcome labels from Apollo."""
    key = os.getenv("APOLLO_API_KEY")
    if not key:
        return None, "APOLLO_API_KEY not set"

    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": key,
    }

    # Try phone_call_outcomes endpoint first
    for endpoint in [
        "https://api.apollo.io/api/v1/phone_call_outcomes",
        "https://api.apollo.io/api/v1/phone_call_purposes",
        "https://api.apollo.io/api/v1/call_dispositions",
    ]:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                # Try common key names
                for key_name in ["phone_call_outcomes", "phone_call_purposes", "outcomes", "dispositions", "results"]:
                    if data.get(key_name):
                        return data[key_name], None
                # If none matched, return raw
                return data, None
            elif resp.status_code == 404:
                continue
            else:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            continue

    # Fallback: extract from a sample call record
    return None, "No dedicated outcomes endpoint found — will extract from sample call"


def get_apollo_outcomes_from_calls():
    """Extract unique outcome IDs and names from recent calls."""
    key = os.getenv("APOLLO_API_KEY")
    if not key:
        return {}

    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": key,
    }

    # Pull calls for all SDRs
    sdr_json = os.getenv("SDR_USERS_JSON", "{}")
    try:
        sdr_users = json.loads(sdr_json)
    except Exception:
        sdr_users = {}

    outcome_map = {}  # id -> name
    seen_ids = set()

    for name, ids in sdr_users.items():
        user_id = ids.get("apollo_user_id")
        if not user_id:
            continue
        try:
            resp = requests.post(
                "https://api.apollo.io/api/v1/phone_calls/search",
                headers=headers,
                json={"user_ids": [user_id], "page": 1, "per_page": 100},
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            calls = resp.json().get("phone_calls", [])
            for c in calls:
                oid = c.get("phone_call_outcome_id")
                if oid and oid not in seen_ids:
                    seen_ids.add(oid)
                    outcome_map[oid] = {
                        "id": oid,
                        "name": None,  # name not in call record, need separate lookup
                        "status_seen": c.get("status"),
                    }
        except Exception:
            continue

    return outcome_map


def get_hubspot_dispositions():
    """Fetch call disposition options from HubSpot CRM properties."""
    token = os.getenv("HUBSPOT_API_KEY")
    if not token:
        return None, "HUBSPOT_API_KEY not set"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    url = "https://api.hubapi.com/crm/v3/properties/calls/hs_call_disposition"
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            options = data.get("options", [])
            return options, None
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return None, str(e)


def main():
    print("\n" + "=" * 65)
    print("CALL OUTCOME / DISPOSITION LOOKUP")
    print("=" * 65)

    # --- Apollo ---
    print("\n📞 APOLLO — Phone Call Outcomes\n")

    outcomes, err = get_apollo_outcomes()
    if outcomes and isinstance(outcomes, list) and len(outcomes) > 0:
        print(f"Found {len(outcomes)} outcomes via API:\n")
        for o in outcomes:
            oid = o.get("id") or o.get("_id") or o.get("key")
            name = o.get("name") or o.get("label") or o.get("value")
            print(f"  ID:   {oid}")
            print(f"  Name: {name}\n")
    else:
        if err:
            print(f"Direct endpoint error: {err}")
        print("Falling back to extracting outcome IDs from recent call records...\n")
        outcome_map = get_apollo_outcomes_from_calls()
        if outcome_map:
            print(f"Found {len(outcome_map)} unique outcome IDs in recent calls:\n")
            for oid, info in outcome_map.items():
                print(f"  ID:     {oid}")
                print(f"  Status seen on this call: {info['status_seen']}")
                print(f"  Name:   (cannot resolve name — see note below)\n")
            print("NOTE: Apollo doesn't always expose outcome names via API.")
            print("To find the names, go to Apollo UI:")
            print("  Settings → Calls → Call Outcomes")
            print("Match each UUID above to the name you see there.\n")
        else:
            print("No outcome IDs found in recent calls. All calls may have outcome_id: None.\n")

    # --- HubSpot ---
    print("\n📋 HUBSPOT — Call Dispositions\n")
    dispositions, err = get_hubspot_dispositions()
    if err:
        print(f"Error: {err}\n")
    elif not dispositions:
        print("No dispositions found.\n")
    else:
        print(f"Found {len(dispositions)} dispositions:\n")
        for d in dispositions:
            val = d.get("value")
            label = d.get("label")
            hidden = d.get("hidden", False)
            print(f"  ID:     {val}")
            print(f"  Label:  {label}")
            if hidden:
                print(f"  (hidden/archived)")
            print()

    print("=" * 65)
    print("NEXT STEP")
    print("=" * 65)
    print("""
Copy the full output above and paste it to Claude.
We'll identify which IDs correspond to Walter's 3 connected outcomes:
  - Connected - Positive
  - Connected - Neutral
  - Connected - Negative

Then we'll hardcode those IDs into the KPI workflow to calculate
connect rate = connected calls / total calls.
""")


if __name__ == "__main__":
    main()
