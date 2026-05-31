"""Verify codex stops shelling out on a real enrichment prompt.

Runs an enrichment-style prompt (the kind that made codex grep the filesystem
for JSON field names) under two configs and reports, per config: which tool
items codex emitted (commandExecution / fileChange = bad), the assistant text
length, and whether the output parses as JSON.

  buggy : repo cwd, no developer instructions     (reproduces the agent behavior)
  fixed : empty scratch cwd + chat-only developer instructions (the backend fix)

Usage: python _bench/codex_toolcheck.py [effort]   (default high)
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHAT_ONLY = (
    "You are a stateless question-answering assistant exposed over an HTTP chat "
    "API. Answer each user message directly and completely using only your own "
    "knowledge and the content of the message itself. Do not use any tools. Do "
    "not run shell commands. Do not read, list, search, or otherwise inspect "
    "files or directories. There is no relevant codebase, repository, or "
    "workspace — ignore the working directory entirely. If the message asks for "
    "a specific output format (e.g. a JSON object), return exactly that and "
    "nothing else."
)

# Faithful copy of shiny-fiesta/udir-helper/enrich.py ENRICH_INSTRUCTIONS so the
# trigger (JSON field names the agent tries to grep for) matches production.
PROMPT = """\
Du beriker en tidligere Udir-eksamensoppgave med metadata for et søkbart bibliotek som lærere bruker til å bygge øvingssett til elevene sine.

Output: ETT JSON-objekt. Ingen markdown-fences. Ingen kommentarer. Ingen prosa rundt.

Produser JSON med følgende felter (alle obligatoriske):

{
  "tittel": "Kort beskrivende tittel, 3-8 ord, på samme målform som oppgaven.",
  "sammendrag": "1-2 setninger som beskriver hva eleven konkret skal gjøre. Maks 50 ord.",
  "tema": ["2-5 stikkord for hva oppgaven tester. Bruk fagets vokabular."],
  "oppgavetyper": ["1-3 verdier fra DENNE LUKKEDE LISTEN: 'flervalg', 'kort-svar', 'regning', 'drøfting', 'case-analyse', 'tegning', 'skjemautfylling', 'praktisk-beskrivelse'."],
  "vanskelighet": "Velg én: 'lett', 'middels', 'krevende'.",
  "tid_minutter": 30,
  "egnet_for": ["1-3 korte friform-strings som beskriver elevprofil oppgaven passer for."],
  "forutsetninger": ["1-3 stikkord for forkunnskaper eleven trenger."],
  "sensor_fokus": "Én setning, maks 25 ord, om hva en sensor primært vil vurdere.",
  "språk_nivå": "Velg én: 'enkelt', 'standard', 'krevende'."
}

Regler:
- Vær konkret. tema=['fag'] eller sensor_fokus='At eleven svarer riktig' er ubrukelig.
- Hvis oppgaven er på nynorsk: bruk nynorsk i feltene.
- "oppgavetyper" må KUN inneholde verdier fra den lukkede listen over.
- "vanskelighet" skal reflektere faktisk arbeidsmengde.
- Returner kun JSON-objektet, ingenting annet.

KRITISK: Output MÅ være nøyaktig ett gyldig JSON-objekt som starter med `{` og slutter med `}`.

Oppgave-detaljer følger nedenfor:

=== OPPGAVE ===
FAGKODE: RLF2001
FAG: Reservasjonsfag
TRINN: Vg2
PERIODE: Vår 2023
OPPGAVE: Oppgave 3 (30% av eksamen)

FULL OPPGAVE-TEKST (Markdown):
---
Gjør rede for hensikten med en risikovurdering (SJA) før en brønnoperasjon.
Beskriv minst tre farer som kan oppstå under en wireline-operasjon, og foreslå
tiltak for hver fare. Bruk eksempler fra praksis der det er relevant.
---
"""


class Codex:
    def __init__(self, cwd, dev_instructions):
        self.cwd = cwd
        self.dev = dev_instructions
        os.makedirs(cwd, exist_ok=True)
        self.proc = subprocess.Popen(["codex", "app-server"], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace")
        self.nid = 1; self.pending = {}; self.notifs = None
        threading.Thread(target=self._read, daemon=True).start()
        self._req("initialize", {"clientInfo": {"name": "toolcheck", "version": "0.1"}})

    def _read(self):
        for line in iter(self.proc.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in m and ("result" in m or "error" in m):
                q = self.pending.pop(m["id"], None)
                if q:
                    q.put(m)
            elif "id" in m and "method" in m:
                self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"],
                    "error": {"code": -32601, "message": "no"}}) + "\n")
                self.proc.stdin.flush()
            elif "method" in m and self.notifs is not None:
                self.notifs.put((m["method"], m.get("params", {})))

    def _req(self, method, params, timeout=60):
        i = self.nid; self.nid += 1
        q = queue.Queue(maxsize=1); self.pending[i] = q
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        r = q.get(timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    def run(self, prompt, effort, timeout=300):
        params = {"approvalPolicy": "never", "sandbox": "read-only", "cwd": self.cwd}
        if self.dev:
            params["developerInstructions"] = self.dev
        tid = self._req("thread/start", params)["thread"]["id"]
        self.notifs = queue.Queue()
        i = self.nid; self.nid += 1; self.pending[i] = queue.Queue(maxsize=1)
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "turn/start",
            "params": {"threadId": tid, "input": [{"type": "text", "text": prompt}],
                       "model": "gpt-5.4-mini", "effort": effort}}) + "\n")
        self.proc.stdin.flush()
        text = []
        item_types = {}
        while True:
            method, p = self.notifs.get(timeout=timeout)
            if method == "item/agentMessage/delta":
                text.append(p.get("delta", ""))
            elif method in ("item/started", "item/completed"):
                it = (p.get("item") or {}).get("type")
                if it:
                    item_types[it] = item_types.get(it, 0) + 1
            elif method == "turn/completed":
                break
        self.notifs = None
        return "".join(text), item_types

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def valid_json(s):
    s = s.strip()
    try:
        json.loads(s)
        return True
    except Exception:
        return False


TOOL_KINDS = ("commandExecution", "fileChange", "mcpToolCall",
              "dynamicToolCall", "webSearch")


def main():
    effort = sys.argv[1] if len(sys.argv) > 1 else "high"
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    scratch = os.path.join(tempfile.gettempdir(), "agentry-codex-scratch")
    configs = [
        ("buggy (repo cwd, no instr)", REPO, None),
        ("fixed (scratch cwd + chat-only)", scratch, CHAT_ONLY),
    ]
    for label, cwd, dev in configs:
        print(f"\n=== {label} ===  ({trials} trials, effort={effort})")
        shelled = 0
        for t in range(1, trials + 1):
            cx = Codex(cwd, dev)
            try:
                text, items = cx.run(PROMPT, effort)
                tool_items = {k: v for k, v in items.items() if k in TOOL_KINDS}
                if tool_items:
                    shelled += 1
                print(f"  trial {t}: tools={tool_items or 'NONE':<24} "
                      f"json={valid_json(text)} len={len(text)}")
            finally:
                cx.close()
        print(f"  >>> {label}: shelled-out in {shelled}/{trials} trials")


if __name__ == "__main__":
    main()
