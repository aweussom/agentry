"""Read agentry.ini (gitignored, holds secrets). Returns plain dicts; callers
decide what's required. Absent file / section / fields -> None or empty."""
import configparser
from pathlib import Path

INI = Path(__file__).parent / "agentry.ini"


def copilot_quota():
    """Return the [copilot_quota] config as a dict, or None if unusable
    (file/section missing, or no pat+username — i.e. quota display disabled)."""
    if not INI.exists():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(INI, encoding="utf-8")
    except Exception:
        return None
    if not cp.has_section("copilot_quota"):
        return None
    g = cp["copilot_quota"]
    pat = (g.get("pat") or "").strip()
    username = (g.get("username") or "").strip()
    if not pat or not username:
        return None
    return {
        "pat": pat,
        "username": username,
        "plan": (g.get("plan") or "pro").strip().lower(),
        "monthly_limit": (g.get("monthly_limit") or "").strip(),
        "expiry": (g.get("expiry") or "").strip(),
        "pat_name": (g.get("pat_name") or "").strip(),
    }
