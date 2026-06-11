"""
Project 3: Daily Prospect List

Generate a daily prospect list from the AEC firm universe, excluding:
1. Companies marked as customers in HubSpot
2. Companies with any engagement in HubSpot in last N days (default 180)

Enrich surviving companies with Apollo contacts matching target titles.
Push to a daily Apollo list. Post summary to Slack.

Run: python -m workflows.daily_prospects
"""
import os
import sys
import csv
import json
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
    csv_path = DATA_DIR / "aec_firms.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"AEC firm CSV not found at {csv_path}. Export from RDS first."
        )
    return pd.read_csv(csv_path)


def main():
    run = logger.start_run("daily_prospects")
    print(f"[prospects] Starting at {run['started_at']}")

    try:
        # 1. Load firm universe
        print("[prospects] Loading AEC firms snapshot...")
        firms = load_aec_firms()
        print(f"[prospects] Got {len(firms)} firms in universe")

        # 2. Build exclusion sets from HubSpot
        print("[prospects] Fetching customer domains...")
        customers = hubspot.get_customer_companies()
        customer_domains = {
            (c.get("properties", {}).get("domain") or "").lower().strip()
            for c in customers
            if c.get("properties", {}).get("domain")
        }
        print(f"[prospects] {len(customer_domains)} customer domains to exclude")

        days_back = int(os.getenv("PROSPECT_EXCLUDE_ENGAGED_DAYS", "180"))
        print(f"[prospects] Fetching companies engaged in last {days_back} days...")
        engaged_domains = hubspot.get_recently_engaged_company_domains(days_back)
        print(f"[prospects] {len(engaged_domains)} recently engaged domains to exclude")

        all_excluded = customer_domains | engaged_domains

        # 3. Apply exclusions
        if "domain" in firms.columns:
            firms["_dom"] = firms["domain"].astype(str).str.lower().str.strip()
            survivors = firms[~firms["_dom"].isin(all_excluded)].drop(columns="_dom")
        else:
            survivors = firms
        print(f"[prospects] {len(survivors)} firms surviving exclusion")

        # Take top N to keep Apollo calls bounded
        target_n = 25
        survivors = survivors.head(target_n)

        # 4. Enrich with Apollo contacts
        titles = [t.strip() for t in os.getenv("TARGET_TITLES", "").split(",") if t.strip()]
        if not titles:
            titles = ["Proposals", "Marketing", "Pursuit", "AI", "Innovation"]

        domains = [d for d in survivors.get("domain", []).tolist() if d]
        contacts = []
        chunk_size = 10
        for i in range(0, len(domains), chunk_size):
            chunk = domains[i : i + chunk_size]
            people = apollo.search_people_by_company_and_titles(
                company_domains=chunk, titles=titles, per_page=25
            )
            contacts.extend(people)
        print(f"[prospects] {len(contacts)} contacts enriched")

        # 5. Write CSV
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        csv_path = OUTPUTS_DIR / f"daily_prospects_{ts}.csv"
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

        # 6. Push to Apollo list
        list_name = f"Daily_Prospects_{datetime.utcnow().strftime('%Y%m%d')}"
        contact_ids = [c.get("id") for c in contacts if c.get("id")]
        push_result = {"success": False, "reason": "no contacts"}
        if contact_ids:
            push_result = apollo.add_contacts_to_list(contact_ids, list_name)
        print(f"[prospects] Apollo push: {push_result}")

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
            "Daily Prospect List",
            len(contacts),
            sample,
        )
        slack_result = slack.post_message(
            text, blocks=blocks, channel=os.getenv("SLACK_PROSPECTS_CHANNEL")
        )

        run["metrics"] = {
            "universe_size": len(firms),
            "customer_excluded": len(customer_domains),
            "engaged_excluded": len(engaged_domains),
            "survivors": len(survivors),
            "contacts_found": len(contacts),
            "apollo_push_success": push_result.get("success", False),
            "slack_posted": slack_result.get("ok", False),
        }
        summary = (
            f"{len(survivors)} firms · {len(contacts)} contacts · "
            f"Apollo: {'✓' if push_result.get('success') else 'CSV fallback'}"
        )
        log_path = logger.finish_run(run, "success", summary)
        print(f"[prospects] ✓ Done. Log: {log_path}")
        return run

    except Exception as e:
        log_path = logger.finish_run(run, "failed", error=str(e))
        print(f"[prospects] ✗ Failed: {e}")
        raise


if __name__ == "__main__":
    main()
