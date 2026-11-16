"""Suggest which analytics a library video belongs to, from words in its file name.

Only suggestions: nothing is linked until someone confirms them in the library.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .converter import PROFILE_SUFFIX

# Extra words per seeded analytics (matched as whole words, case-insensitive).
# Every analytics also matches its own name, e.g. "firesmoke" or "camera tampering".
ALIASES: dict[str, list[str]] = {
    "FireSmoke": ["fire", "smoke", "flame", "flames"],
    "PPE": ["ppe", "helmet", "hardhat", "vest", "safety"],
    "videoANPR": ["anpr", "numberplate", "plate", "licence", "license"],
    "VehicleALPR": ["alpr"],
    "CrowdDensity": ["crowd", "crowded", "density"],
    "AbandonedObject": ["bag", "baggage", "luggage", "abandoned", "unattended", "suitcase"],
    "SlipAndFall": ["slip", "fall", "fallen", "falling"],
    "Camera Tampering": ["tamper", "tampering"],
    "Elephant": ["elephant", "elephants"],
    "Parking": ["parking", "parked"],
    "Charging station": ["charging", "charger", "ev"],
    "TrafficCongestion": ["traffic", "congestion", "jam"],
    "VehicleDwell": ["dwell"],
    "RegionCount": ["region"],
    "LineCount": ["linecount", "crossing"],
    "DigitalFence": ["fence", "intrusion", "perimeter"],
    "Human Attributes": ["attribute", "attributes"],
}


def words(text: str) -> list[str]:
    """'crowd2_PPE-FireSmoke.mp4' -> ['crowd', 'ppe', 'fire', 'smoke', 'mp'] (splits case changes and digits)."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w.lower() for w in re.findall(r"[A-Za-z]+", spaced)]


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def base_name(rel_path: str) -> str:
    """The original's rel_path for a converted copy ("a (720p).mp4" -> "a"), else the path without extension."""
    p = PurePosixPath(rel_path)
    stem = p.stem
    for sfx in PROFILE_SUFFIX.values():
        if stem.endswith(sfx):
            stem = stem[: -len(sfx)]
            break
    return str(p.with_name(stem))


def match(filename: str, analytics_name: str) -> str | None:
    """The word that ties a file to an analytics, or None."""
    stem = PurePosixPath(filename).stem
    ws = set(words(stem))
    ws |= {w[:-1] for w in ws if len(w) > 3 and w.endswith("s")}  # "bags" -> "bag"
    for alias in ALIASES.get(analytics_name, []):
        if alias in ws:
            return alias
    own = _squash(analytics_name)
    if len(own) >= 4 and own in _squash(stem):
        return analytics_name
    if own in ws:  # short names ("PPE") only as a whole word
        return analytics_name
    return None


def suggest(videos, analytics, linked: set[tuple[int, int]]) -> list[dict]:
    """Suggestions for READY videos not yet in a matching analytics.

    `videos`: rows with id, rel_path, status, compat_mode and is_16_9 (bool or None);
    `analytics`: rows with id and name; `linked`: (analytics_id, video_id) pairs that exist.
    An original and its converted copies count as one video: if any of them is already
    in the analytics there is no suggestion, otherwise the best one is suggested
    (16:9 first, then the original over a copy).
    """
    groups: dict[str, list] = {}
    for v in videos:
        if v.status == "READY" and v.compat_mode != "reject":
            groups.setdefault(base_name(v.rel_path), []).append(v)
    out = []
    for a in analytics:
        for base, members in groups.items():
            word = match(PurePosixPath(base).name, a.name)
            if not word or any((a.id, v.id) in linked for v in members):
                continue
            best = min(members, key=lambda v: (v.is_16_9 is False, v.rel_path != base + PurePosixPath(v.rel_path).suffix))
            out.append({"video_id": best.id, "rel_path": best.rel_path, "analytics_id": a.id,
                        "analytics_name": a.name, "word": word})
    out.sort(key=lambda d: (d["analytics_name"].lower(), d["rel_path"].lower()))
    return out
