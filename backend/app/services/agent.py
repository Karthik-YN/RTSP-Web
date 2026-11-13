"""Readable device names from a browser's User-Agent ("Windows · Chrome")."""

from __future__ import annotations

import re

_OS = [
    (r"iPhone", "iPhone"), (r"iPad", "iPad"), (r"Android", "Android"), (r"CrOS", "ChromeOS"),
    (r"Windows NT", "Windows"), (r"Mac OS X|Macintosh", "macOS"), (r"Linux", "Linux"),
]
_BROWSER = [
    (r"Edg(e|A|iOS)?/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox/|FxiOS/", "Firefox"),
    (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari"),
]
_TOOLS = [(r"^curl/", "curl"), (r"python-requests|python-httpx|aiohttp", "Python script"),
          (r"^Wget/", "wget"), (r"PowerShell", "PowerShell")]


def describe_agent(ua: str | None) -> str | None:
    if not ua:
        return None
    for pat, name in _TOOLS:
        if re.search(pat, ua, re.I):
            return name
    os_name = next((name for pat, name in _OS if re.search(pat, ua)), None)
    browser = next((name for pat, name in _BROWSER if re.search(pat, ua)), None)
    parts = [p for p in (os_name, browser) if p]
    return " · ".join(parts) or ua[:40]
