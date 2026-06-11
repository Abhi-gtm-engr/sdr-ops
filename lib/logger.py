"""
Local run logger — writes JSON log files to ./logs/

Each run gets one file: logs/{workflow}_{YYYYMMDD_HHMMSS}.json
Streamlit dashboard reads these for display.
"""
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def start_run(workflow: str) -> Dict:
    """Create a run record. Returns the run dict to be mutated."""
    return {
        "workflow": workflow,
        "started_at": datetime.utcnow().isoformat(),
        "ended_at": None,
        "status": "running",
        "summary": "",
        "metrics": {},
        "errors": [],
        "output_files": [],
    }


def finish_run(run: Dict, status: str = "success", summary: str = "", error: Optional[str] = None) -> str:
    """Finalize and persist the run. Returns log file path."""
    run["ended_at"] = datetime.utcnow().isoformat()
    run["status"] = status
    if summary:
        run["summary"] = summary
    if error:
        run["errors"].append(error)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{run['workflow']}_{ts}.json"
    path = LOG_DIR / filename
    with open(path, "w") as f:
        json.dump(run, f, indent=2, default=str)
    return str(path)


def list_runs(workflow: Optional[str] = None, limit: int = 30) -> list:
    """List recent runs, newest first."""
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("*.json"), reverse=True)
    runs = []
    for f in files:
        if workflow and not f.name.startswith(workflow):
            continue
        try:
            with open(f) as fp:
                run = json.load(fp)
                run["_log_path"] = str(f)
                runs.append(run)
        except (json.JSONDecodeError, OSError):
            continue
        if len(runs) >= limit:
            break
    return runs


def last_run(workflow: str) -> Optional[Dict]:
    runs = list_runs(workflow=workflow, limit=1)
    return runs[0] if runs else None
