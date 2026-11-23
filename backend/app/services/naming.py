"""The video naming convention: ``WHAT_WHERE_NN[_tag...].ext`` (docs/PHASE12_PLAN.md section 3).

    ALPR_toll-s1_01.mp4          ALPR, toll plaza camera s1, clip 1
    JAM_dhaka_01_web.mp4         traffic jam, Dhaka, clip 1, downloaded from the internet

* WHAT  - the analytics code (UPPERCASE, 2-8 letters/digits), one per analytics (``analytics.code``) plus ``MISC``.
* WHERE - site or camera, lowercase letters/digits, words joined by ``-``, at most 14 characters.
* NN    - clip number 01-99 for the same WHAT + WHERE (the app picks the next free one).
* tags  - optional, only from TAGS (or ``<n>fps``). The converter adds ``h264`` / ``720p`` / ``720p_16x9``.

Pure functions only: the API and the upload path call these with the codes from the database.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath

MODES = ("enforce", "warn", "off")
MISC = "MISC"
TAGS = ("720p", "1080p", "16x9", "h264", "4k", "web", "night")
WHERE_MAX = 14

_CODE = r"[A-Z][A-Z0-9]{1,7}"
_WORD = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_TAG = r"(?:720p|1080p|16x9|h264|4k|web|night|\d{1,3}fps)"
STEM_RE = re.compile(rf"^(?P<what>{_CODE})_(?P<where>{_WORD})_(?P<nn>\d{{2}})(?P<tags>(?:_{_TAG})*)$")
PATTERN = STEM_RE.pattern

# Codes agreed for the analytics that existed when the convention was introduced (by analytics name).
DEFAULT_CODES = {
    "Charging station": "EV", "videoANPR": "ANPR", "TrafficCongestion": "JAM", "VehicleALPR": "ALPR",
    "VehicleDwell": "DWELL", "Trigger line videos": "TRIG", "Camera Tampering": "TAMPER", "SlipAndFall": "FALL",
    "CrowdDensity": "CROWD", "RegionCount": "REGION", "LineCount": "LINE", "PPE": "PPE",
    "AbandonedObject": "ABANDON", "DigitalFence": "FENCE", "Human Attributes": "ATTR", "Elephant": "ELEPHANT",
    "FireSmoke": "FIRE", "Parking": "PARK", "RLVD": "RLVD", "RedLightViolation": "RLVD",
}


@dataclass
class Parsed:
    what: str
    where: str
    nn: int
    tags: list[str]
    ext: str

    @property
    def stem(self) -> str:
        return "_".join([self.what, self.where, f"{self.nn:02d}", *self.tags])


@dataclass
class Check:
    ok: bool
    errors: list[str] = field(default_factory=list)
    parsed: Parsed | None = None

    def view(self) -> dict:
        p = self.parsed
        return {"ok": self.ok, "errors": self.errors,
                "parts": None if p is None else {"what": p.what, "where": p.where, "nn": p.nn, "tags": p.tags}}


def split(name: str) -> tuple[str, str]:
    p = PurePosixPath(name)
    return p.stem, p.suffix.lower()


def parse(name: str) -> Parsed | None:
    """The parts of a conforming file name (codes not checked), else None."""
    stem, ext = split(name)
    m = STEM_RE.match(stem)
    if not m or not ext or len(m["where"]) > WHERE_MAX or m["nn"] == "00":
        return None
    tags = [t for t in m["tags"].split("_") if t]
    return Parsed(m["what"], m["where"], int(m["nn"]), tags, ext)


def check(name: str, codes: set[str] | None = None) -> Check:
    """Does ``name`` follow the convention? Errors are short, human sentences."""
    stem, ext = split(name)
    errors: list[str] = []
    if not ext:
        errors.append("Add the file extension (for example .mp4).")
    if re.search(r"[^A-Za-z0-9_.-]", stem):
        errors.append("Use only letters, digits, '-' and '_' (no spaces or brackets).")
    parts = stem.split("_")
    if len(parts) < 3:
        errors.append("Use three parts: WHAT_WHERE_NN, for example ALPR_toll-s1_01.")
    else:
        what, where, nn, *tags = parts
        if not re.fullmatch(_CODE, what):
            errors.append(f"WHAT '{what}' must be an UPPERCASE analytics code such as ALPR or FALL.")
        elif codes is not None and what not in codes:
            errors.append(f"WHAT '{what}' is not an analytics code. Known: {', '.join(sorted(codes))}.")
        if not re.fullmatch(_WORD, where):
            errors.append(f"WHERE '{where}' must be lowercase letters/digits, words joined by '-'.")
        elif len(where) > WHERE_MAX:
            errors.append(f"WHERE '{where}' is longer than {WHERE_MAX} characters.")
        if not re.fullmatch(r"\d{2}", nn) or nn == "00":
            errors.append(f"NN '{nn}' must be a two-digit clip number 01-99.")
        bad = [t for t in tags if not re.fullmatch(_TAG, t)]
        if bad:
            errors.append(f"Unknown tag(s) {', '.join(bad)}. Allowed: {', '.join(TAGS)}, <n>fps.")
    p = parse(name) if not errors else None
    return Check(ok=not errors, errors=errors, parsed=p)


def slug(text: str, limit: int = WHERE_MAX) -> str:
    """Turn free text into a WHERE value: 'Toll Plaza S1' -> 'toll-plaza-s1'."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if len(t) > limit:
        # cut at a word boundary: "tirupathi-bench" -> "tirupathi", not "tirupathi-benc"
        cut = t[:limit]
        t = cut if t[limit] == "-" or "-" not in cut else cut[: cut.rfind("-")]
        t = t.rstrip("-")
    return t


def auto_code(name: str, taken: set[str]) -> str:
    """A code for a new analytics: its capital letters/words, uppercase, unique, 2-8 characters."""
    words = re.findall(r"[A-Za-z0-9]+", unicodedata.normalize("NFKD", name))
    base = (words[0] if len(words) == 1 else "".join(w[0] for w in words)).upper()
    if len(base) < 2:
        base = "".join(words).upper()
    base = re.sub(r"^[0-9]+", "", base)[:8] or "AN"
    if len(base) < 2:
        base = (base + "X")[:2]
    code, n = base, 2
    while code in taken or code == MISC:
        suffix = str(n)
        code, n = base[: 8 - len(suffix)] + suffix, n + 1
    return code


def next_number(what: str, where: str, existing: list[str]) -> int:
    """The next free NN for WHAT + WHERE, given every file name in the library."""
    used = {p.nn for p in map(parse, existing) if p and p.what == what and p.where == where}
    n = 1
    while n in used:
        n += 1
    if n > 99:
        raise ValueError(f"All clip numbers 01-99 are used for {what}_{where}.")
    return n


def build(what: str, where: str, nn: int, tags: list[str], ext: str) -> str:
    ext = ext if ext.startswith(".") else "." + ext
    return "_".join([what, where, f"{nn:02d}", *tags]) + ext.lower()


def suggest(original: str, codes: set[str], existing: list[str], what: str | None = None,
            where: str | None = None) -> str | None:
    """A conforming name for an uploaded file, from whatever the original name reveals."""
    stem, ext = split(original)
    if not ext:
        return None
    words = [w for w in re.split(r"[^A-Za-z0-9]+", stem) if w]
    upper = {w.upper() for w in words}
    if not what:
        what = next((c for c in sorted(codes, key=len, reverse=True) if c in upper), MISC)
    tags = [t for t in TAGS if t in {w.lower() for w in words}]
    if not where:
        rest = [w.lower() for w in words if w.upper() != what and w.lower() not in TAGS
                and not re.fullmatch(r"\d+", w)]
        where = slug("-".join(rest[:2])) or "tbd"
    where = slug(where) or "tbd"
    return build(what, where, next_number(what, where, existing), tags, ext)
