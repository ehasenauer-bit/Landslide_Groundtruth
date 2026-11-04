"""Paste a whole "lat, lon" pair into either coordinate box, on every tab.

Coordinates travel as a pair everywhere the analyst gets them — a detection
record, a spreadsheet row, a map site's right-click copy — so making them paste
the latitude, go back, and paste the longitude is friction for nothing. Every
Latitude/Longitude pair in the dock calls link_pair(), and a pair pasted into
EITHER box is split across both.

Two things are deliberately NOT split:

  * A pair being TYPED. Splitting "59.9, -1" the moment it parses would fill
    Longitude with -1 and leave the rest of the typing landing in Latitude. A
    typed pair is split when editing finishes (Enter, or leaving the box); a
    paste — any edit that puts in more than one character at once — is split
    straight away.
  * Anything that is not unambiguously two coordinates: three numbers,
    degrees-minutes-seconds, a value out of range. Those are left exactly as
    entered for the tab's own validation to explain.

Order follows the pasted text, lat first, with two exceptions that cannot be
misread: hemisphere letters ("60.03 N, 153.06 W") say which number is which,
and a first number beyond ±90 cannot be a latitude, so lon-first text (WKT
"POINT(-149.82 59.91)", GeoJSON "[-149.82, 59.91]") is swapped rather than
refused.
"""

import re

# A decimal number with optional sign, degree mark and hemisphere letter on
# either side. A letter only counts as a hemisphere when it stands alone, so the
# "e" closing "Latitude 60.5" is not read as East.
_COORD = re.compile(
    r"(?<![A-Za-z])([NSEWnsew])?\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*°?\s*"
    r"([NSEWnsew](?![A-Za-z]))?")

_TIP = ("Paste a whole “lat, lon” pair into either box to fill both — "
        "e.g. 59.906992, -149.823317.")


def parse_pair(text):
    """(lat, lon) from text holding exactly one coordinate pair, else None."""
    t = (text or "").replace("−", "-").strip()    # typographic minus
    if not t:
        return None
    # A number straight after a degree mark is minutes: "60° 30' N" is ONE
    # coordinate in degrees-minutes, not a pair.
    if re.search(r"°\s*\d", t):
        return None
    hits = _COORD.findall(t)
    # Everything else in the text must be punctuation or labels, never a third
    # number.
    if len(hits) != 2 or len(re.findall(r"\d+(?:\.\d*)?|\.\d+", t)) != 2:
        return None
    vals, axes = [], []
    for lead, num, trail in hits:
        hemi = (lead or trail).upper()
        v = float(num)
        if hemi in ("S", "W"):
            v = -abs(v)
        vals.append(v)
        axes.append("lat" if hemi in ("N", "S") else
                    "lon" if hemi in ("E", "W") else "")
    # One letter but not the other ("60 30 N") or the same axis twice
    # ("60 N, 61 N") is not a pair anyone meant.
    if any(axes) and (not all(axes) or axes[0] == axes[1]):
        return None
    a, b = vals
    if axes == ["lon", "lat"] or (not any(axes) and abs(a) > 90 >= abs(b)):
        a, b = b, a
    if not (-90.0 <= a <= 90.0 and -180.0 <= b <= 180.0):
        return None
    return a, b


def _inserted(old, new):
    """How many characters an edit put in: what `new` holds outside the prefix
    and suffix it shares with `old`. Typing is 1, deleting 0, a paste more."""
    n = min(len(old), len(new))
    p = 0
    while p < n and old[p] == new[p]:
        p += 1
    s = 0
    while s < n - p and old[-1 - s] == new[-1 - s]:
        s += 1
    return len(new) - p - s


def link_pair(lat_edit, lon_edit, on_split=None):
    """Make a pair pasted into either box fill both.

    `on_split(lat, lon)` is called after a split, for a tab that logs it."""
    prev = {}

    def apply(text):
        pair = parse_pair(text)
        if pair is None:
            return
        lat, lon = pair
        # setText never emits textEdited, so this cannot re-enter
        lat_edit.setText(f"{lat:.6f}")
        lon_edit.setText(f"{lon:.6f}")
        if on_split is not None:
            on_split(lat, lon)

    def remember(box, text):
        prev[box] = text

    def edited(box, text):
        # textEdited arrives before textChanged, so prev still holds the text
        # as it was before this edit
        if _inserted(prev.get(box, ""), text) > 1:
            apply(text)

    for box in (lat_edit, lon_edit):
        prev[box] = box.text()
        box.textChanged.connect(lambda t, b=box: remember(b, t))
        box.textEdited.connect(lambda t, b=box: edited(b, t))
        box.editingFinished.connect(lambda b=box: apply(b.text()))
        if not box.toolTip():
            box.setToolTip(_TIP)
