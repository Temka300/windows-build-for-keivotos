"""Platform launch options for desktop helpers, preserving caller flags."""
import subprocess
import sys


def quiet_options(options=None):
    result = dict(options or {})
    if sys.platform == "win32":
        result["creationflags"] = result.get("creationflags", 0) | getattr(subprocess,"CREATE_NO_WINDOW",0)
    return result
