"""Opt-in live API smoke test: create one future booking and cancel it.

Requires a previously authenticated CLI, or credentials injected in the environment.
Never embeds or records passwords. Not collected by unittest discovery.
"""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import uuid


def call(*args):
    result = subprocess.run([sys.executable, "-m", "gpudeck_cli", *args],
                            capture_output=True, text=True, timeout=100)
    data = json.loads(result.stdout)
    if result.returncode:
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-reservation-test", action="store_true", required=True)
    parser.add_argument("--output", default="validation-live.json")
    args = parser.parse_args()
    report = {"platform": "https://gpudeck.cslee.cc", "account": None, "checks": {},
              "reservationId": None, "cleanupVerified": False}
    reservation_id = None
    project = "CLI debug auto-cancel " + uuid.uuid4().hex[:8]
    report["testProject"] = project
    try:
        user = call("whoami")
        report["account"] = user["username"]
        report["checks"]["loginSession"] = True
        resources = call("resources")
        report["serverTime"] = resources["serverTime"]
        report["nodeCount"] = len(resources["nodes"])
        report["gpuCount"] = sum(len(n["gpus"]) for n in resources["nodes"])
        report["checks"]["resources"] = True
        report["checks"]["gpus"] = bool(call("gpus", "--server", "H200"))
        call("processes", "--mine")
        report["checks"]["processes"] = True
        baseline = call("reservations", "--mine")
        report["existingLiveIds"] = [r["id"] for r in baseline if r["status"] not in {"cancelled", "completed"}]
        report["checks"]["reservations"] = True
        now = datetime.fromisoformat(resources["serverTime"].replace("Z", "+00:00"))
        # Stay beyond all of this account's current bookings. A five-minute
        # future window provides a reversible API test without activating jobs.
        current_ends = [datetime.fromisoformat(r["endsAt"].replace("Z", "+00:00"))
                        for r in baseline if r["status"] not in {"cancelled", "completed"}]
        earliest = max([now + timedelta(hours=2)] + current_ends) + timedelta(hours=1)
        for step in range(24 * 12):
            start = earliest + timedelta(hours=step)
            if start > now + timedelta(days=13):
                raise RuntimeError("No future test slot available within the scheduling horizon")
            end = start + timedelta(minutes=5)
            start_text, end_text = start.isoformat(), end.isoformat()
            available = call("availability", "--server", "H200", "--start", start_text, "--end", end_text)
            device = next((g for g in available["gpus"] if g["index"] == 0 and g["reservable"]), None)
            if device:
                break
        else:
            raise RuntimeError("No test GPU slot available")
        report["checks"]["availability"] = True
        reserve_args = ["reserve", "--gpu-id", device["id"], "--start", start_text, "--end", end_text,
                        "--project", project, "--purpose", "CLI API debug; cancel immediately"]
        preview = call(*reserve_args, "--dry-run")
        report["testRequest"] = preview["request"]
        report["checks"]["dryRun"] = preview["dryRun"] and preview["check"]["valid"]
        created = call(*reserve_args)
        reservation_id = created["reservation"]["id"]
        report["reservationId"] = reservation_id
        report["checks"]["createAndReadback"] = created["created"] and created["verified"]
        again = call(*reserve_args)
        report["checks"]["duplicateReuse"] = again["reused"] and again["reservation"]["id"] == reservation_id
        call("cancel", reservation_id, "--dry-run")
        report["checks"]["cancelPreview"] = True
    finally:
        # If the response was lost after commit, recover only this unique test
        # project before attempting cancellation. Never touch baseline bookings.
        if reservation_id is None:
            try:
                matches = [r for r in call("reservations", "--mine") if r["projectName"] == project]
                if matches:
                    reservation_id = matches[0]["id"]
                    report["reservationId"] = reservation_id
            except Exception as exc:
                report["recoveryError"] = type(exc).__name__
        if reservation_id:
            try:
                cancelled = call("cancel", reservation_id)
                report["checks"]["cancelAndReadback"] = cancelled["verified"]
                report["cleanupVerified"] = cancelled["reservation"]["status"] == "cancelled"
                report["finalStatus"] = cancelled["reservation"]["status"]
                report["finalGpuIds"] = cancelled["reservation"]["gpuIds"]
                again = call("cancel", reservation_id)
                report["checks"]["duplicateCancel"] = not again["changed"]
            except Exception as exc:
                report["cleanupError"] = str(exc)
        report["completedAt"] = datetime.now().astimezone().isoformat()
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["cleanupVerified"] or not all(report["checks"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
