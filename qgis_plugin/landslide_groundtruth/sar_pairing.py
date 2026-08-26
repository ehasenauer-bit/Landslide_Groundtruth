"""Seismic-time pair-bracket summary for the SAR tab.

The SAR search already splits scenes into pre/post around the event time and
``SarTab._pair_for`` stars the nearest post scene + nearest same-track pre. What
this module adds is the *human-readable bracket* that recommendation #1 of the
Alaska SAR report asked for: how many days before the event the reference scene
sits, how soon after the failure the first post scene looks at the slide, the
total pair span, and whether the pair really shares viewing geometry.

Kept deliberately dependency-free (stdlib ``datetime`` only) so it unit-tests
without QGIS/Qt — run ``python sar_pairing.py`` for the self-test.

Candidate dicts are the ones ``sar_imagery._candidate`` produces:
    {id, date (ISO), gap_days, orbit_state, relative_orbit, ...}
"""
import datetime as dt

__all__ = ["summarize", "parse_dt"]


def parse_dt(value):
    """ISO string (or datetime) -> naive-UTC datetime, or None. Tolerates a
    trailing 'Z', an offset, or a date-only string."""
    if value is None or isinstance(value, dt.datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for cut in (len(s), 19, 16, 10):          # full, seconds, minutes, date-only
        try:
            d = dt.datetime.fromisoformat(s[:cut])
            return d.replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            continue
    return None


def _same_geometry(a, b):
    """Same relative orbit (track) AND same pass direction -> comparable geometry."""
    if not a or not b:
        return False
    ro = a.get("relative_orbit")
    return ro is not None and ro == b.get("relative_orbit") \
        and a.get("orbit_state") == b.get("orbit_state")


def _geo_label(pre, post):
    c = post or pre or {}
    state = (c.get("orbit_state") or "").lower()
    orb = "ASC" if state.startswith("asc") else "DESC" if state.startswith("desc") else "?"
    track = c.get("relative_orbit")
    return orb + (f"·T{track}" if track is not None else "")


def _fmt(d):
    return d.strftime("%d %b") if d else "?"


def _days(delta):
    """Timedelta -> whole days, rounded to nearest (not truncated): a scene 3.7
    days before the event reads as −4 d, which is how an analyst thinks of it."""
    return round(delta.total_seconds() / 86400.0)


def _one_pair(pre, post, event):
    pd = parse_dt(pre.get("date")) if pre else None
    od = parse_dt(post.get("date")) if post else None
    geo = _geo_label(pre, post)
    if pd and od:
        pre_lat = _days(event - pd)
        post_lat = _days(od - event)
        span = _days(od - pd)
        tag = "same track ✓" if _same_geometry(pre, post) else "⚠ cross-orbit — geometry differs"
        return (f"{geo}: pre −{pre_lat} d ({_fmt(pd)}) · post +{post_lat} d "
                f"({_fmt(od)}) · {span} d span · {tag}")
    if od:
        return f"{geo}: post +{_days(od - event)} d ({_fmt(od)}) — no pre scene paired"
    if pd:
        return f"{geo}: pre −{_days(event - pd)} d ({_fmt(pd)}) — no post scene paired"
    return ""


def summarize(picks, event_time):
    """picks: list of (side, candidate) as returned by ``SarTab._default_picks``.
    event_time: ISO string or datetime (the seismic origin time).

    Returns a short multi-line plain-text summary of how the starred pair(s)
    bracket the event — one line per track in 'both geometries' mode — or ''
    when the event time or a usable pair is missing.
    """
    event = parse_dt(event_time)
    if event is None:
        return ""
    pres = [c for s, c in picks if s == "pre"]
    posts = [c for s, c in picks if s == "post"]
    if not pres and not posts:
        return ""

    lines, used = [], set()
    for post in posts:
        pre = next((c for c in pres if id(c) not in used and _same_geometry(c, post)), None)
        if pre is None:
            pre = next((c for c in pres if id(c) not in used), None)
        if pre is not None:
            used.add(id(pre))
        line = _one_pair(pre, post, event)
        if line:
            lines.append(line)
    for pre in pres:                      # any pre with no post partner
        if id(pre) not in used:
            line = _one_pair(pre, None, event)
            if line:
                lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    ev = "2023-09-13T09:30:00Z"        # Peters Dome, 13 Sep 2023
    def cand(date, orb, track, side, gap):
        return (side, dict(id=f"S1_{date}", date=date, orbit_state=orb,
                           relative_orbit=track, gap_days=gap))

    print("— single same-track pair (the Peters Dome-style 9/21 Sep bracket) —")
    picks = [cand("2023-09-09T16:29:28Z", "descending", 131, "pre", 4),
             cand("2023-09-21T16:29:30Z", "descending", 131, "post", 8)]
    out = summarize([c for c in picks], ev)
    print(out)
    assert "pre −4 d" in out and "post +8 d" in out and "12 d span" in out and "same track" in out

    print("\n— cross-orbit fallback (should warn) —")
    picks = [cand("2023-09-05T05:00:00Z", "ascending", 94, "pre", 8),
             cand("2023-09-15T16:29:30Z", "descending", 131, "post", 2)]
    out = summarize([c for c in picks], ev)
    print(out)
    assert "cross-orbit" in out

    print("\n— 'both geometries' (two same-track pairs) —")
    picks = [cand("2023-09-10T16:29:28Z", "descending", 131, "pre", 3),
             cand("2023-09-19T16:29:30Z", "descending", 131, "post", 6),
             cand("2023-09-08T05:00:00Z", "ascending", 94, "pre", 5),
             cand("2023-09-20T05:00:02Z", "ascending", 94, "post", 7)]
    out = summarize([c for c in picks], ev)
    print(out)
    assert out.count("\n") == 1 and "DESC·T131" in out and "ASC·T94" in out

    print("\n— no event time -> empty —")
    assert summarize(picks, None) == ""
    print("\nAll sar_pairing self-tests passed.")
