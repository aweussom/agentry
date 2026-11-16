"""PreToolUse hook for the agy backend: the tool lockdown.

agy runs this for EVERY tool call (subagents included) when it is registered
in <gemini-dir>/config/hooks.json, which AgyACPBackend writes at startup into
agentry's own gemini dir (never ~/.gemini). It reads the call as JSON on
stdin and prints one decision as JSON on stdout.

Allowed, nothing else:
  invoke_subagent  only TypeName "image-generator" (agy's image path)
  generate_image   only with reference paths inside agentry's brain dir
  send_message     the image subagent reporting back to its parent
  read_file        only inside agentry's brain dir (references, agy's images)

agy fails CLOSED on this hook: a missing command, a timeout and non-JSON
output all blocked the call (probed 2026-11-16 on agy 1.5.9). A crash here
therefore denies, which is the direction we want. Keep it fast and pure;
stdlib only, no agentry imports (it runs under -I in a fresh interpreter).

Usage (from hooks.json): python -I agy_hook.py <brain-root> <deny-log>
"""
import json
import os
import sys
import time

brain_root = os.path.normcase(os.path.realpath(sys.argv[1]))
deny_log = sys.argv[2]

raw = sys.stdin.read()
call = json.loads(raw)
tool_call = call.get("toolCall") or {}
name = tool_call.get("name") or ""
args = tool_call.get("args") or {}


def inside_brain(path):
    if not isinstance(path, str) or not path:
        return False
    real = os.path.normcase(os.path.realpath(path))
    return real == brain_root or real.startswith(brain_root + os.sep)


allowed = False
reason = f"agentry: tool {name or '?'} is disabled; answer from your own knowledge"
if name == "invoke_subagent":
    if args.get("TypeName") == "image-generator":
        allowed = True
    else:
        reason = "agentry: only the image-generator subagent is available"
if name == "generate_image":
    refs = args.get("ImagePaths") or []
    bad_refs = [p for p in refs if not inside_brain(p)]
    if not bad_refs:
        allowed = True
    else:
        reason = "agentry: image references must be images the user attached"
if name == "send_message":
    allowed = True
if name == "read_file":
    # The image subagent looks at its references before editing. Only the
    # brain dir: attachments, uploads and agy's own generated images.
    if inside_brain(args.get("Path")):
        allowed = True
    else:
        reason = "agentry: read_file may only open images attached in this chat"

if allowed:
    print(json.dumps({"decision": "allow"}))
    sys.exit(0)

# Denials are rare and interesting (a model trying a tool); keep a trail.
try:
    with open(deny_log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": name,
                            "args": json.dumps(args)[:300],
                            "conversationId": call.get("conversationId")}) + "\n")
except OSError:
    pass  # the deny below still stands; a log write must not flip it
print(json.dumps({"decision": "deny", "reason": reason}))
