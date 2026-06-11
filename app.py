"""
SDR Ops Command Center — Streamlit Dashboard

Run: streamlit run app.py
"""
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st
import pandas as pd

from lib import logger

st.set_page_config(
    page_title="SDR Ops Command Center",
    page_icon="🎯",
    layout="wide",
)

# --- Brand ---
INDIGO = "#471CA8"
ORANGE = "#FF774F"

st.markdown(f"""
<style>
.metric-card {{
    background: white;
    padding: 1.2rem;
    border-radius: 12px;
    border: 1px solid #e5e5e5;
    margin-bottom: 1rem;
}}
.status-success {{ color: #16a34a; font-weight: 600; }}
.status-failed {{ color: #dc2626; font-weight: 600; }}
.status-running {{ color: #ca8a04; font-weight: 600; }}
.brand-bar {{
    height: 6px;
    background: linear-gradient(90deg, {INDIGO} 0%, {ORANGE} 100%);
    border-radius: 3px;
    margin-bottom: 1.5rem;
}}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="brand-bar"></div>', unsafe_allow_html=True)
st.title("🎯 SDR Ops Command Center")
st.caption("Three workflows · local Python · logs in `./logs/` · outputs in `./outputs/`")

# --- Workflow definitions ---
WORKFLOWS = [
    {
        "key": "kpi_report",
        "name": "📊 Weekly SDR KPI Report",
        "description": "HubSpot + Apollo metrics per SDR. Slack post + CSV.",
        "schedule": "Weekly · Mondays 8am ET (manual for now)",
        "module": "workflows.kpi_report",
    },
    {
        "key": "territory_lookalike",
        "name": "🗺️ Territory Lookalike",
        "description": "One customer per day → lookalike AEC firms + contacts → Apollo list.",
        "schedule": "Daily · 6am ET (manual for now)",
        "module": "workflows.territory_lookalike",
    },
    {
        "key": "daily_prospects",
        "name": "🎯 Daily Prospect List",
        "description": "AEC firm universe − customers − recently engaged → Apollo list.",
        "schedule": "Daily · 5pm ET EOD (manual for now)",
        "module": "workflows.daily_prospects",
    },
]


def status_badge(status: str) -> str:
    css = {"success": "status-success", "failed": "status-failed", "running": "status-running"}.get(status, "")
    return f'<span class="{css}">● {status.upper()}</span>'


def format_time(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", ""))
        return dt.strftime("%b %d, %Y · %H:%M UTC")
    except (ValueError, TypeError):
        return iso


def run_workflow(module: str):
    """Trigger a workflow via subprocess."""
    with st.spinner(f"Running {module}..."):
        try:
            result = subprocess.run(
                [sys.executable, "-m", module],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                timeout=600,
            )
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired:
            return {"returncode": -1, "stdout": "", "stderr": "Timeout after 10 minutes"}
        except Exception as e:
            return {"returncode": -1, "stdout": "", "stderr": str(e)}


# --- Dashboard cards ---
cols = st.columns(3)
for col, wf in zip(cols, WORKFLOWS):
    with col:
        with st.container(border=True):
            st.subheader(wf["name"])
            st.caption(wf["description"])
            st.caption(f"⏰ {wf['schedule']}")

            last = logger.last_run(wf["key"])
            if last:
                st.markdown(status_badge(last.get("status", "unknown")), unsafe_allow_html=True)
                st.caption(f"Last run: {format_time(last.get('started_at'))}")
                if last.get("summary"):
                    st.write(last["summary"])
            else:
                st.caption("Never run")

            if st.button(f"▶ Run now", key=f"run_{wf['key']}", use_container_width=True):
                result = run_workflow(wf["module"])
                if result["returncode"] == 0:
                    st.success("Completed")
                else:
                    st.error("Failed — check Run History tab")
                with st.expander("Output"):
                    st.code(result["stdout"] or "(no stdout)")
                    if result["stderr"]:
                        st.code(result["stderr"])
                st.rerun()

st.divider()

# --- Run history ---
st.subheader("Run History")
all_runs = logger.list_runs(limit=50)
if not all_runs:
    st.info("No runs yet. Trigger a workflow above to get started.")
else:
    history_rows = []
    for r in all_runs:
        history_rows.append({
            "Workflow": r.get("workflow", ""),
            "Status": r.get("status", ""),
            "Started": format_time(r.get("started_at", "")),
            "Summary": r.get("summary", ""),
            "Errors": "; ".join(r.get("errors", []))[:100],
        })
    df = pd.DataFrame(history_rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

st.divider()

# --- Recent outputs ---
st.subheader("Recent Output Files")
outputs_dir = Path(__file__).resolve().parent / "outputs"
if outputs_dir.exists():
    files = sorted(outputs_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    if files:
        for f in files:
            col1, col2, col3 = st.columns([3, 1, 1])
            col1.text(f.name)
            col2.text(f"{f.stat().st_size // 1024} KB")
            with col3:
                with open(f, "rb") as fp:
                    st.download_button(
                        "Download", fp.read(), file_name=f.name, key=str(f),
                        use_container_width=True,
                    )
    else:
        st.caption("No output files yet")
else:
    st.caption("Outputs directory not found")

# --- Config check ---
with st.expander("⚙️ Configuration Check"):
    import os
    from dotenv import load_dotenv
    load_dotenv()

    checks = [
        ("HUBSPOT_API_KEY", bool(os.getenv("HUBSPOT_API_KEY"))),
        ("APOLLO_API_KEY", bool(os.getenv("APOLLO_API_KEY"))),
        ("Slack (webhook OR bot token)", bool(os.getenv("SLACK_WEBHOOK_URL") or os.getenv("SLACK_BOT_TOKEN"))),
        ("SDR_USERS_JSON", bool(os.getenv("SDR_USERS_JSON"))),
        ("AEC firm snapshot CSV", (Path(__file__).resolve().parent / "data" / "aec_firms.csv").exists()),
    ]
    for name, ok in checks:
        st.write(f"{'✅' if ok else '❌'} {name}")
