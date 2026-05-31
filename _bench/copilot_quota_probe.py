"""Locate copilot's premium-requests quota in ~/.copilot/session-state/*/events.jsonl
(read-only). Finds the most recent event carrying quota/premium fields and prints
the structure + value."""
import json
import os
import glob
import re

root = os.path.expanduser("~/.copilot/session-state")
files = glob.glob(os.path.join(root, "*", "events.jsonl"))
# newest first
files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

KEY = re.compile(r"premium|quota|remaining|entitlement|reqs", re.I)


def walk(obj, path=""):
    """Yield (path, value) for scalar leaves under keys matching KEY."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else k
            if KEY.search(k) and not isinstance(v, (dict, list)):
                yield kp, v
            yield from walk(v, kp)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


shown = 0
for f in files:
    found_here = []
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not KEY.search(line):
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                leaves = list(walk(ev))
                if leaves:
                    found_here.append((ev.get("type") or ev.get("event") or "?", leaves))
    except Exception:
        continue
    if found_here:
        sid = os.path.basename(os.path.dirname(f))
        mt = os.path.getmtime(f)
        import datetime
        print(f"\n=== {sid}  (events.jsonl mtime {datetime.datetime.fromtimestamp(mt):%Y-%m-%d %H:%M}) ===")
        # show the LAST few quota-bearing events (most current)
        for etype, leaves in found_here[-3:]:
            print(f"  event type={etype}")
            for kp, v in leaves:
                print(f"      {kp} = {v}")
        shown += 1
    if shown >= 3:
        break

if not shown:
    print("no quota-bearing events found")
