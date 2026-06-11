"""
Project 1: Weekly SDR KPI Report

Scheduled workflow entrypoint. Builds a rolling 7-day KPI report, writes CSV,
posts to Slack, and logs the run.
"""
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from lib import logger, slack
from lib.kpi import OUTPUTS_DIR, build_kpi_report


def main():
    run = logger.start_run("kpi_report")
    print(f"[kpi_report] Starting run at {run['started_at']}")

    try:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=7)
        report = build_kpi_report(start_dt, end_dt)
        print(f"[kpi_report] Window: {report['range_label']}")

        for name, mx in report["per_sdr"].items():
            print(f"[kpi_report] Collecting metrics for {name}...")
            print(
                f"  → CLEAN Sent: {mx['emails_sent']} Replies: {mx['replies']} "
                f"Calls: {mx['calls_logged']} Meetings: {mx['meetings_booked']}"
            )
            print(
                f"    internal → HS calls {mx['calls_hs']} | Apollo calls {mx['calls_apollo']} "
                f"| dupes removed {mx['call_dupes']}"
            )

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        csv_path = OUTPUTS_DIR / f"kpi_report_{ts}.csv"
        with open(csv_path, "w", newline="") as f:
            fieldnames = [
                "sdr",
                "emails_sent", "emails_sent_hs", "emails_sent_apollo",
                "opens", "opens_hs", "opens_apollo",
                "replies", "replies_hs", "replies_apollo",
                "open_rate", "reply_rate",
                "calls_logged", "calls_hs", "calls_apollo", "call_dupes",
                "connected_calls", "connect_rate",
                "meetings_booked", "meetings_hs", "meetings_apollo",
                "notes",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for mx in report["per_sdr"].values():
                row = {k: mx.get(k, "") for k in fieldnames}
                row["notes"] = "; ".join(mx.get("notes", []))
                writer.writerow(row)
        run["output_files"].append(str(csv_path))
        print(f"[kpi_report] Wrote CSV: {csv_path}")

        notes = set()
        for mx in report["per_sdr"].values():
            for note in mx.get("notes", []):
                if "tier" in note.lower() or "blocked" in note.lower() or "pedro" in note.lower():
                    notes.add(note)
        notes.add("Rolling last 7 days. Calls are deduplicated across HubSpot and Apollo before reporting.")

        text, blocks = slack.format_kpi_report(report["range_label"], report["per_sdr"], notes=list(notes))
        slack_result = slack.post_message(text, blocks=blocks)
        print(f"[kpi_report] Slack result: {slack_result}")

        run["metrics"] = {
            **report["summary"],
            "slack_posted": slack_result.get("ok", False),
        }
        summary = (
            f"{report['summary']['sdr_count']} SDRs · "
            f"{report['summary']['total_emails_sent']} emails · "
            f"{report['summary']['total_calls']} calls · "
            f"{report['summary']['total_replies']} replies · "
            f"{report['summary']['total_meetings']} meetings"
        )
        log_path = logger.finish_run(run, "success", summary)
        print(f"[kpi_report] ✓ Done. Log: {log_path}")
        return run

    except Exception as e:
        log_path = logger.finish_run(run, "failed", error=str(e))
        print(f"[kpi_report] ✗ Failed: {e}")
        raise


if __name__ == "__main__":
    main()
