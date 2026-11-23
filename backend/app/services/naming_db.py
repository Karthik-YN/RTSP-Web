"""Naming convention helpers that need the database: analytics codes, known WHERE values, names in use."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from ..models import Analytics, AnalyticsCategory, Video
from . import naming


def ensure_codes(db) -> int:
    """Give every analytics without a WHAT code one (agreed defaults first, else derived from its name)."""
    with db.session() as s:
        rows = list(s.scalars(select(Analytics).order_by(Analytics.id)))
        taken = {a.code for a in rows if a.code}
        n = 0
        for a in rows:
            if a.code:
                continue
            want = naming.DEFAULT_CODES.get(a.name)
            a.code = want if want and want not in taken else naming.auto_code(a.name, taken)
            taken.add(a.code)
            n += 1
        return n


def codes(db) -> list[dict]:
    """Every WHAT code with its analytics (sorted by category, then analytics), plus MISC."""
    with db.session() as s:
        q = (select(Analytics.code, Analytics.id, Analytics.name, AnalyticsCategory.name)
             .join(AnalyticsCategory, AnalyticsCategory.id == Analytics.category_id)
             .where(Analytics.code.is_not(None))
             .order_by(AnalyticsCategory.sort, AnalyticsCategory.name, Analytics.sort, Analytics.name))
        out = [{"code": c, "analytics_id": aid, "analytics": name, "category": cat} for c, aid, name, cat in s.execute(q)]
    out.append({"code": naming.MISC, "analytics_id": None, "analytics": "Not decided yet", "category": None})
    return out


def code_set(db) -> set[str]:
    return {c["code"] for c in codes(db)}


def names_in_use(db) -> list[str]:
    """File names (not paths) of every video row, for picking the next free NN."""
    with db.session() as s:
        return [r.rsplit("/", 1)[-1] for r in s.scalars(select(Video.rel_path))]


def places(db) -> list[dict]:
    """WHERE values already used in conforming names, most used first, with the codes they appear with."""
    count: Counter[str] = Counter()
    whats: dict[str, set[str]] = {}
    for name in names_in_use(db):
        p = naming.parse(name)
        if p:
            count[p.where] += 1
            whats.setdefault(p.where, set()).add(p.what)
    return [{"where": w, "videos": n, "codes": sorted(whats[w])} for w, n in count.most_common()]
