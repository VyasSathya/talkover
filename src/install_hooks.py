#!/usr/bin/env python3
"""Add (or remove) the spoken-reply hooks in Claude Code's settings.json.

    python src/install_hooks.py                 # ~/.claude/settings.json, backup next to it
    python src/install_hooks.py --remove
    python src/install_hooks.py --settings path/to/settings.json

Adds two hooks that run this repo's speak hook (hooks/speak-response.ps1 on Windows, hooks/speak_response.py
on macOS and Linux):
  Stop        reads the finished reply aloud
  PreToolUse  (-Mode transcript) reads text Claude wrote before a tool call, so you hear it while it works
Idempotent: entries pointing at this repo's hook are replaced, never duplicated. If settings.json already runs a
speak hook from somewhere else, nothing is changed unless --replace is given.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from voice_config import HOOKS_DIR

# Windows: the PowerShell hook. macOS / Linux: the Python port, run by this venv's Python.
if sys.platform == "win32":
    HOOK = (HOOKS_DIR / "speak-response.ps1").resolve()
    BASE = 'powershell -NoProfile -ExecutionPolicy Bypass -File "%s"' % HOOK.as_posix()
else:
    HOOK = (HOOKS_DIR / "speak_response.py").resolve()
    BASE = '"%s" "%s"' % (Path(sys.executable).as_posix(), HOOK.as_posix())
ENTRIES = {
    "Stop": {"type": "command", "command": BASE, "timeout": 600, "statusMessage": "Speaking reply", "async": True},
    "PreToolUse": {"type": "command", "command": BASE + " -Mode transcript", "timeout": 600, "async": True},
}


def ours(cmd: str) -> bool:
    return HOOK.as_posix().lower() in cmd.replace("\\", "/").lower()


def foreign(cmd: str) -> bool:
    return ("speak-response.ps1" in cmd or "speak_response.py" in cmd) and not ours(cmd)


def strip(groups, pred):
    """Drop hook commands matching pred; drop groups left empty."""
    out = []
    for g in groups or []:
        hooks = [h for h in g.get("hooks", []) if not pred(h.get("command", ""))]
        if hooks:
            out.append(dict(g, hooks=hooks))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--replace", action="store_true", help="also remove speak hooks from other folders")
    a = ap.parse_args()

    path = Path(a.settings)
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})

    if not a.remove and not a.replace:
        clash = [h.get("command", "") for ev in ENTRIES for g in hooks.get(ev, []) for h in g.get("hooks", [])
                 if foreign(h.get("command", ""))]
        if clash:
            print("settings.json already runs another speak hook:\n  " + "\n  ".join(clash))
            print("Nothing changed. Re-run with --replace to point it at this repo.")
            return 2

    pred = (lambda c: ours(c) or foreign(c)) if a.replace else ours
    for ev in ENTRIES:
        groups = strip(hooks.get(ev), pred)
        if not a.remove:
            groups.append({"hooks": [ENTRIES[ev]]})
        if groups:
            hooks[ev] = groups
        else:
            hooks.pop(ev, None)
    if not hooks:
        data.pop("hooks", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        print("backup:", backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    print(("removed the voice hooks from " if a.remove else "voice hooks installed in ") + str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
