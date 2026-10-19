"""Background work for the Maxar VHR tab: every GEGD request runs in a QgsTask.

The tab never blocks the GUI on GEGD. Each task here does its requests through
``gegd_net.fetch`` (QGIS's network access manager, so QGIS's proxy, SSL, cache and
the ``gegdpro`` auth config all apply), keeps its results as plain data on
itself, and NEVER touches a widget or the QgsProject: layers are built on the main
thread by the tab once the task has finished (threading rules 3 and 4).

Cancelling a task cancels its ``QgsFeedback``, which aborts the HTTP transfer in
flight and any retry wait, so Cancel and dock teardown take effect at once rather
than after a 30 s timeout. Every failure is kept as a redacted ``GegdError`` on
``.error``; nothing a task stores can carry the key (URLs never do, and
GegdError redacts its own text).

``gegd_auth.ensure_ready()`` must have run on the GUI thread before a task is
started: it unlocks the password store (any master-password prompt happens there,
never in a worker) and settles ``gegd_auth.mode()``, which ``fetch`` reads.
"""
from datetime import datetime, timedelta, timezone

from qgis.core import QgsFeedback, QgsTask

from . import gegd_api as api
from . import gegd_net
from . import gegd_rank as R
from .gegd_api import GegdError, redact

__all__ = ["SearchTask", "KeyTestTask", "ALASKA_BBOX", "KEY_TEST_COUNT", "vantor_optical"]

# The canary that tells "no coverage here" from "query broken": 50 km around the
# epicentre over the last year, count only.
CANARY_R_KM = 50.0
CANARY_DAYS = 365
# The nearest strip outside an empty side is asked for with a few spares, because
# the server's box is the padded square around the search circle: the nearest one
# in the box can still miss the circle itself, or be SAR / third-party / a legacy
# mosaic, and would then be no use to widen to.
NEAREST_COUNT = 10
# The hint and canary requests only sharpen a message, so they get one retry, not
# three: a struggling server should not hold back a table that is already in hand.
HINT_RETRIES = 1

# Lon-first, like every GEGD bbox: mainland Alaska plus the Aleutians east of the
# antimeridian. The key test asks for the newest strip in here.
ALASKA_BBOX = (-179.9, 51.0, -129.0, 71.6)
# The key test's page: enough newest records that one is a Vantor optical strip
# even when the very newest are third-party SAR, still one small request.
KEY_TEST_COUNT = 25


class _GegdTask(QgsTask):
    """Shared plumbing: a feedback that Cancel reaches, and error capture."""

    def __init__(self, description):
        super().__init__(description, QgsTask.CanCancel)
        self._feedback = QgsFeedback()
        self.error = None
        self.n_requests = 0

    def cancel(self):
        # Abort the transfer in flight too, not only the next one.
        try:
            self._feedback.cancel()
        except RuntimeError:
            pass
        super().cancel()

    def _get(self, url, service, retries=3):
        if self.isCanceled() or self._feedback.isCanceled():
            raise gegd_net.Cancelled(service)
        self.n_requests += 1
        return gegd_net.fetch(url, service, timeout_s=30, retries=retries,
                              feedback=self._feedback)

    def _get_catalog(self, build, clause):
        """``(reply, clause that worked)`` for the WFS URL ``build(clause)``.

        If the server cannot parse the Vivid/BASE_MOSAIC exclusion, retry ONCE per
        weaker form (NOT IN -> a chain of <> -> client-side only); every other
        failure is raised as it is (gegd_api.clause_fallback says which is which)."""
        while True:
            try:
                return self._get(build(clause), "wfs"), clause
            except GegdError as e:
                nxt = api.clause_fallback(e, clause)
                if nxt is None:
                    raise
                clause = nxt

    def run(self):
        try:
            self._work()
            return not self.isCanceled()
        except GegdError as e:
            self.error = e
        except Exception as e:             # a bug, not a server answer: say what it was
            self.error = GegdError(
                "network", f"The GEGD request failed unexpectedly: "
                f"{type(e).__name__}: {redact(str(e))}")
        return False

    def _work(self):
        raise NotImplementedError

    @property
    def cancelled(self):
        return bool(getattr(self.error, "cancelled", False)) or self.isCanceled()


class SearchTask(_GegdTask):
    """One event search: the strips around the epicentre, ranked, plus the extra
    evidence the empty-state messages need.

    Requests, in order:
      1. WFS GetFeature, one page of up to 5000, CRS:84 BBOX of the padded search
         circle, ``t0 <= acq < t1``, Vivid/BASE_MOSAIC excluded in
         ``product_clause`` form. A query error retries ONCE per weaker form
         (NOT IN -> a chain of <> -> client-side only) via gegd_api.clause_fallback;
         ``.product_clause`` says which form worked, so the tab can remember it.
      2. Only for a side with no usable strip (nothing, or only SAR / third-party /
         mosaic context rows) while the circle saw something: the nearest usable
         strip OUTSIDE the window on that side ("nearest earlier strip 2025-06-14
         (WV02, ONA 22°)"). Skipped before the archive floor, and after a window
         that already reaches "now".
      3. Only when nothing usable came back at all (Vivid alone counts as
         nothing): the canary hits query.
    Requests 2 and 3 only sharpen the empty-state message: if one fails, the
    search still succeeds, the message falls back to its plain wording, and the
    error is kept in ``.hint_errors`` for the log.

    Results, all plain data: ``.strips`` (every parsed record), ``.ranked``
    (gegd_rank.Ranked for ``filters``), ``.raw_features_json`` (the WFS body as
    text: no key, no pixels, safe to cache in search.json), ``.nearest_pre`` /
    ``.nearest_post`` (a Strip, gegd_rank.NOT_FOUND, or None when not asked or
    not answerable), ``.canary_hits`` (int or None), ``.hint_errors`` (list of
    GegdError), ``.error`` (GegdError or None)."""

    def __init__(self, host, lat, lon, r_km, origin_utc, t0, t1, filters=None,
                 product_clause="not_in"):
        super().__init__("Maxar/Vantor GEGD Pro search")
        self.host = host or api.HOST_DEFAULT
        self.lat, self.lon, self.r_km = float(lat), float(lon), float(r_km)
        self.origin_utc = R._utc(origin_utc)
        self.t0, self.t1 = R._utc(t0), R._utc(t1)
        self.filters = R.as_filters(filters)
        self.product_clause = product_clause if product_clause in api.PRODUCT_CLAUSES \
            else "not_in"
        self.strips = []
        self.ranked = None
        self.raw_features_json = None
        self.nearest_pre = None
        self.nearest_post = None
        self.canary_hits = None
        self.hint_errors = []

    def _work(self):
        bbox = R.event_bbox(self.lat, self.lon, self.r_km)
        reply, clause = self._get_catalog(
            lambda c: api.wfs_getfeature_url(self.host, bbox, self.t0, self.t1,
                                             product_clause=c),
            self.product_clause)
        self.product_clause = clause
        self.setProgress(40)
        self.strips = api.parse_features(reply.body)
        self.raw_features_json = bytes(reply.body).decode("utf-8", "replace")

        # A first ranking, only to see which side is empty (no requests). The
        # nearest-outside strip is asked for only where empty_state_message can use
        # it: a side with no usable strip (context rows never count: they are
        # never a before/after image, whatever the filters) while the circle saw
        # something. With nothing usable in the circle at all, the canary is what
        # tells the story.
        first = R.rank(self.strips, self.origin_utc, self.lat, self.lon, self.r_km,
                       self.filters)
        disc = R.aoi_disc(self.lat, self.lon, self.r_km)
        now = datetime.now(timezone.utc)
        listed = len(first.pre) + len(first.post)
        usable_pre = any(r.grade != "context" for r in first.pre)
        usable_post = any(r.grade != "context" for r in first.post)
        if listed and not usable_pre and self.t0 > api.ARCHIVE_FLOOR_UTC:
            self.nearest_pre = self._hint(
                lambda: self._nearest(bbox, self.t0, "pre", clause, disc))
        self.setProgress(60)
        if (listed and not usable_post and self.origin_utc < self.t1
                and self.t1 < now - timedelta(minutes=5)):
            self.nearest_post = self._hint(
                lambda: self._nearest(bbox, self.t1, "post", clause, disc))
        self.setProgress(80)
        if listed == 0 and first.n_outside == 0 and self.t1 > api.ARCHIVE_FLOOR_UTC:
            cb = R.event_bbox(self.lat, self.lon, CANARY_R_KM, pad=0.0)
            self.canary_hits = self._hint(lambda: api.parse_hits(self._get(
                api.wfs_hits_url(self.host, cb, now - timedelta(days=CANARY_DAYS), now,
                                 product_clause=clause), "wfs", retries=HINT_RETRIES).body))
        self.ranked = R.rank(self.strips, self.origin_utc, self.lat, self.lon, self.r_km,
                             self.filters, t0=self.t0, t1=self.t1,
                             nearest_pre=self.nearest_pre, nearest_post=self.nearest_post,
                             canary_hits=self.canary_hits)
        self.setProgress(100)

    def _hint(self, ask):
        """``ask()``, or None when that optional request failed (kept in
        .hint_errors). Cancel still cancels: Cancelled is re-raised."""
        try:
            return ask()
        except gegd_net.Cancelled:
            raise
        except GegdError as e:
            self.hint_errors.append(e)
            return None

    def _nearest(self, bbox, t, side, clause, disc):
        """The nearest usable strip outside the window on ``side``; NOT_FOUND when
        the server has nothing there at all; None when it has records but none is
        a usable strip touching the search circle (nothing honest to point at).

        Usable = gegd_rank.hint_candidate (never SAR, third-party, a legacy mosaic
        or Vivid), and on the before side not older than the archive floor: the
        hint must name a strip that widening the window would actually list."""
        body = self._get(api.wfs_nearest_url(self.host, bbox, t, side, product_clause=clause,
                                             count=NEAREST_COUNT), "wfs",
                         retries=HINT_RETRIES).body
        got = [s for s in api.parse_features(body) if not api.is_excluded(s)]
        if not got:
            return R.NOT_FOUND
        for s in got:
            if not R.hint_candidate(s):
                continue
            if side == "pre" and (s.acq_utc is None or s.acq_utc < api.ARCHIVE_FLOOR_UTC):
                continue
            cover, covers = R.aoi_cover(s.rings, disc)
            if cover > 0.0 or covers:
                return s
        return None


def vantor_optical(strip):
    """True for a strip Vantor's own optical sensors collected: what the key test
    names. Never a third-party record (Umbra, ICEYE, Capella, … come back as
    THIRD_PARTY, sometimes with no isThirdParty or sensorType), a record whose
    sensorType is not 'Optical' (radar, or missing), Vivid / BASE_MOSAIC, or a
    multi-date mosaic tile."""
    return (not api.is_excluded(strip)
            and not getattr(strip, "is_third_party", False)
            and (getattr(strip, "sensor_type", None) or "").strip().lower() == "optical"
            and not getattr(strip, "multi_date", False)
            and getattr(strip, "acq_utc", None) is not None)


class KeyTestTask(_GegdTask):
    """The connection box's Test: one WFS request, naming the newest Vantor
    optical strip over Alaska.

    One small page (``count=KEY_TEST_COUNT``), newest first, over the last
    ``days``: proves the key reaches the catalog and that the catalog answers,
    for the price of one request. The newest record is often not an image an
    analyst would recognise (a THIRD_PARTY SAR collect, say), so the strip named
    is picked client-side with ``vantor_optical`` rather than with a CQL term the
    server might refuse and Test would then blame on the key. ``.newest`` is that
    Strip, or None; ``.n_records`` says how many records the page held (0: the
    window held none). The window is long because no Daily Take is collected
    Dec–Jan this far north: a short one would read "no strip" in mid-winter for a
    key that works. It sends the Vivid exclusion in the form Search last got
    through (``product_clause``) and falls back the same way, so Test never
    blames the key for a CQL form the server stopped reading."""

    def __init__(self, host=api.HOST_DEFAULT, days=120, product_clause="not_in"):
        super().__init__("Maxar/Vantor GEGD Pro key test")
        self.host = host or api.HOST_DEFAULT
        self.days = int(days)
        self.product_clause = product_clause if product_clause in api.PRODUCT_CLAUSES \
            else "not_in"
        self.newest = None
        self.n_records = 0

    def _work(self):
        now = datetime.now(timezone.utc)
        reply, self.product_clause = self._get_catalog(
            lambda c: api.wfs_getfeature_url(self.host, ALASKA_BBOX,
                                             now - timedelta(days=self.days), now,
                                             count=KEY_TEST_COUNT, product_clause=c),
            self.product_clause)
        strips = api.parse_features(reply.body)
        self.n_records = len(strips)
        own = [s for s in strips if vantor_optical(s)]
        # The server sorts newest first; max() keeps that true whatever it did.
        self.newest = max(own, key=lambda s: s.acq_utc) if own else None
