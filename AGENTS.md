# Repository Guidelines

## Project Structure & Module Organization

`agentry.py` is the Flask HTTP server and CLI entry point. `backends.py`
contains the Copilot, Codex, and Claude backend implementations; shared console
and request logging lives in `logutil.py`. The browser UI is split between
`templates/index.html` and `static/` (`css/style.css`, `js/app.js`). Launchers
are `start.ps1` for Windows and `start.sh` for Linux/WSL. Keep exploratory
scripts in `_bench/`; project notes and historical plans belong in `TODO.md`,
`TODONT.md`, and `archive/`.

## Build, Test, and Development Commands

Use Python 3.11+ and the launchers, which create/manage the virtual environment
and start the local server:

```powershell
.\start.ps1                    # default Copilot backend on port 8765
.\start.ps1 -Backend codex -Port 9000
```

```bash
./start.sh --backend claude
```

For a lightweight syntax check after Python changes, run:

```powershell
python -m py_compile agentry.py backends.py logutil.py
```

There is no formal test suite or build step currently. Run relevant `_bench/`
probes only when changing the backend behavior they cover, and manually check
the web UI at `http://localhost:8765`.

## Coding Style & Naming Conventions

Follow the existing Python style: four-space indentation, `snake_case` for
functions and variables, `PascalCase` for classes, and concise module-level
comments to separate major sections. Prefer standard-library modules before
third-party imports. Keep backend protocol handling explicit and log useful
operational detail without exposing credentials or prompt contents. Use plain
JavaScript and CSS consistent with the existing UI; avoid adding a framework
without a clear need.

## Testing Guidelines

Name new focused probes descriptively, such as `_bench/codex_feature_probe.py`.
Validate error paths as well as successful streaming/chat completion. Changes
to launchers should be checked on their target shell; backend changes require a
locally authenticated corresponding CLI.

## Commit & Pull Request Guidelines

Recent commits use imperative, descriptive subjects, often scoped by area (for
example, `Codex backend: inherit the model from codex config instead of hardcoding`).
Keep commits focused and avoid committing `venv/`, `logs/`, credentials, or
generated local artifacts. PRs should state the affected backend/UI behavior,
list verification performed, link relevant issues, and include screenshots for
visible web UI changes.
