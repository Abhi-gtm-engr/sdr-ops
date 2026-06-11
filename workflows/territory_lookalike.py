"""
Project 2: Territory Lookalike Daily Sequence

For one customer per day (round-robin via date), find AEC firms similar in:
- Industry (A/E/C tag)
- Employee count (± tolerance)
- Revenue band (± tolerance)
- Geo proximity (city/state for now; lat/lng if available)

Enrich surviving companies with Apollo contacts matching target titles.
Push to Apollo list. Post summary to Slack.

Run: python -m workflows.territory_lookalike
"""
import os
import sys
import csv
import json
import math
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

from lib import hubspot, apollo, slack, logger

OUTPUTS_DIR = Path(__file__).resolve().parent.parent / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_aec_firms() -> pd.DataFrame:
    """Load the AEC firm snapshot. Expected columns: name, domain, city, state,
    employee_count, annual_revenue, aec_tag (A/E/C)."""
    csv_path = DATA_DIR / "aec_firms.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"AEC firm CSV not found at {csv_path}. "
            "Export from RDS first — see README."
        )
    df = pd.read_csv(csv_path)
    return df


def pick_customer_for_today(customers: list) -> dict:
    """Round-robin customer pick based on date."""
    if not customers:
        raise RuntimeError("No customers returned from HubSpot")
    idx = datetime.utcnow().toordinal() % len(customers)
    return customers[idx]


def _safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def find_lookalikes(customer: dict, firms: pd.DataFrame) -> pd.DataFrame:
    """Filter AEC firms similar to the given customer."""
    props = customer.get("properties", {}) if "properties" in customer else customer
    target_emp = _safe_float(props.get("numberofemployees"))
    target_rev = _safe_float(props.get("annualrevenue"))
    target_state = (props.get("state") or "").strip().upper()
    target_city = (props.get("city") or "").strip().lower()

    emp_tol = float(os.getenv("LOOKALIKE_EMPLOYEE_TOLERANCE", "0.30"))
    rev_tol = float(os.getenv("LOOKALIKE_REVENUE_TOLERANCE", "0.40"))

    df = firms.copy()

    # Geo filter: same state preferred (cheapest filter, no lat/lng required)
    if target_state and "state" in df.columns:
        df = df[df["state"].astype(str).str.upper().str.strip() == target_state]

    # Employee count filter
    if target_emp and "employee_count" in df.columns:
        lo, hi = target_emp * (1 - emp_tol), target_emp * (1 + emp_tol)
        df = df[
            df["employee_count"].apply(lambda x: _safe_float(x) is not None and lo <= _safe_float(x) <= hi)
        ]

    # Revenue filter
    if target_rev and "annual_revenue" in df.columns:
        lo, hi = target_rev * (1 - rev_tol), target_rev * (1 + rev_tol)
        df = df[
            df["annual_revenue"].apply(lambda x: _safe_float(x) is not None and lo <= _safe_float(x) <= hi)
        ]

    # Exclude the customer itself by domain
    customer_domain = (props.get("domain") or "").lower().strip()
    if customer_domain and "domain" in df.columns:
        df = df[df["domain"].astype(str).str.lower().str.strip() != customer_domain]

    # Rank by composite similarity (smaller delta is better)
    def score(row):
        s = 0
        if target_emp:
            ec = _safe_float(row.get("employee_count"))
            if ec:
                s += abs(ec - target_emp) / target_emp
        if target_rev:
            rv = _safe_float(row.get("annual_revenue"))
            if rv:
                s += abs(rv - target_rev) / target_rev
        if target_city and str(row.get("city", "")).lower().strip() == target_city:
            s -= 0.5  # same-city bonus
        return s

    if not df.empty:
        df = df.assign(_score=df.apply(score, axis=1)).sort_values("_score").head(30)

    return df


def enrich_with_apollo_contacts(firms_df: pd.DataFrame) -> list:
    """For each firm, find Apollo contacts matching target titles."""
    titles = [t.strip() for t in os.getenv("TARGET_TITLES", "").split(",") if t.strip()]
    if not titles:
        titles = ["Proposals", "Marketing", "Pursuit", "AI", "Innovation"]

    domains = [d for d in firms_df.get("domain", []).tolist() if d]
    if not domains:
        return []

    # Batch in chunks (Apollo limits per query)
    contacts = []
    chunk_size = 10
    for i in range(0, len(domains), chunk_size):
        chunk = domains[i : i + chunk_size]
        people = apollo.search_people_by_company_and_titles(
            company_domains=chunk, titles=titles, per_page=25
        )
        contacts.extend(people)

    return contacts


def main():
    run = logger.start_run("territory_lookalike")
    print(f"[lookalike] Starting at {run['started_at']}")

    try:
        # 1. Load customer list
        print("[lookalike] Fetching customers from HubSpot...")
        customers = hubspot.get_customer_companies()
        print(f"[lookalike] Got {len(customers)} customers")

        # 2. Pick today's customer
        today_customer = pick_customer_for_today(customers)
        customer_name = today_customer.get("properties", {}).get("name", "Unknown")
        print(f"[lookalike] Today's customer: {customer_name}")

        # 3. Load AEC firms and find lookalikes
        print("[lookalike] Loading AEC firms snapshot...")
        firms = load_aec_firms()
        print(f"[lookalike] Got {len(firms)} firms in snapshot")

        lookalikes = find_lookalikes(today_customer, firms)
        print(f"[lookalike] Found {len(lookalikes)} lookalikes")

        # 4. Enrich with Apollo contacts
        print("[lookalike] Enriching with Apollo contacts...")
        contacts = enrich_with_apollo_contacts(lookalikes)
        print(f"[lookalike] Got {len(contacts)} contacts")

        # 5. Write CSV
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c for c in customer_name if c.isalnum() or c in "-_")[:40]
        csv_path = OUTPUTS_DIR / f"lookalike_{safe_name}_{ts}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "company_name", "company_domain", "contact_name",
                "title", "email", "linkedin_url", "apollo_contact_id",
            ])
            for c in contacts:
                org = c.get("organization", {}) or {}
                writer.writerow([
                    org.get("name", ""),
                    org.get("primary_domain", ""),
                    c.get("name", ""),
                    c.get("title", ""),
                    c.get("email", ""),
                    c.get("linkedin_url", ""),
                    c.get("id", ""),
                ])
        run["output_files"].append(str(csv_path))
        print(f"[lookalike] Wrote {csv_path}")

        # 6. Push to Apollo list (best effort)
        list_name = f"Lookalike_{safe_name}_{datetime.utcnow().strftime('%Y%m%d')}"
        contact_ids = [c.get("id") for c in contacts if c.get("id")]
        push_result = {"success": False, "reason": "no contacts"}
        if contact_ids:
            push_result = apollo.add_contacts_to_list(contact_ids, list_name)
        print(f"[lookalike] Apollo push: {push_result}")

        # 7. Slack summary
        sample = [
            {
                "name": c.get("name", ""),
                "title": c.get("title", ""),
                "company": (c.get("organization") or {}).get("name", ""),
            }
            for c in contacts[:5]
        ]
        text, blocks = slack.format_prospect_summary(
            f"Lookalike — {customer_name}",
            len(contacts),
            sample,
        )
        slack_result = slack.post_message(
            text, blocks=blocks, channel=os.getenv("SLACK_PROSPECTS_CHANNEL")
        )

        run["metrics"] = {
            "customer": customer_name,
            "lookalike_companies": len(lookalikes),
            "contacts_found": len(contacts),
            "apollo_push_success": push_result.get("success", False),
            "slack_posted": slack_result.get("ok", False),
        }
        summary = (
            f"{customer_name}: {len(lookalikes)} cos, {len(contacts)} contacts "
            f"(Apollo push: {'✓' if push_result.get('success') else 'CSV fallback'})"
        )
        log_path = logger.finish_run(run, "success", summary)
        print(f"[lookalike] ✓ Done. Log: {log_path}")
        return run

    except Exception as e:
        log_path = logger.finish_run(run, "failed", error=str(e))
        print(f"[lookalike] ✗ Failed: {e}")
        raise


if __name__ == "__main__":
    main()
