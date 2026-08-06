"""Medial-axis centerline of a scar polygon, for measuring slide length.

Why not the bounding box: a landslide is rarely straight. It leaves a source
scar, tracks down a gully and spreads at the deposit, so the longest straight
chord across the outline cuts the corner and understates the distance material
actually travelled. What you want is the polygon's own spine, following every
bend, measured tip to tip.

How it works — the standard Voronoi approximation of the medial axis:

  1. resample the outline to roughly even vertex spacing (simplify away detail
     finer than the target spacing, then densify so no segment exceeds it), so
     the skeleton isn't biased by a few densely-digitized stretches;
  2. build the Voronoi diagram of those boundary points and keep only the edges
     lying wholly INSIDE the polygon — those edges are equidistant from two
     stretches of boundary, which is exactly the medial-axis condition;
  3. treat the surviving edges as a weighted graph and take its longest
     shortest-path — the graph diameter, which for a skeleton is the tip-to-tip
     spine. Getting this right needs a little care: the usual two-pass sweep
     (farthest node from an arbitrary start, then farthest from that) is exact
     only on a TREE, and a scar with an unfailed island inside it has a hole,
     whose skeleton is a cycle — there the sweep can settle on the cycle's two
     antipodes and report roughly half the true length. So instead the graph is
     collapsed to its junctions and tips (every run of degree-2 nodes becomes
     one weighted edge), the short spurious branches the Voronoi skeleton grows
     toward every convex corner are pruned, and Dijkstra runs from every
     surviving tip. On the collapsed graph that is cheap, and it is exact
     whether or not the skeleton has cycles;
  4. simplify the result at the sampling spacing (zig-zag below that spacing is
     sampling noise, and it would only inflate the length), then extend both
     ends out to the polygon boundary, because the medial axis stops short of
     the tips it points at.

Everything here works in the geometry's OWN coordinate system and assumes it is
metric (the caller reprojects to local UTM first), so lengths come out in
metres and the Voronoi tolerance/spacing are metres too.

`longest_chord` is the degenerate-case fallback: slivers and tiny polygons can
yield a skeleton with no usable interior edges, and a labelled straight line
beats no answer.
"""
import heapq
import math
from collections import defaultdict, deque

from qgis.core import QgsGeometry, QgsPointXY

# Boundary vertices to aim for before building the Voronoi diagram. More
# vertices = a finer skeleton but a bigger graph; a few hundred resolves every
# bend a hand-digitized scar outline actually has.
TARGET_VERTICES = 800

# Floor on the resampling spacing (m), so a very small polygon doesn't get
# resampled down to numerically-degenerate steps.
MIN_STEP_M = 0.05

# Graph-node quantisation (m). Voronoi edges that meet should share an exact
# coordinate, but rounding to the millimetre makes the graph robust to any
# floating-point drift in the GEOS output.
QUANT_M = 0.001

# A skeleton branch shorter than this many resampling steps is a Voronoi hair
# pointing at a convex corner, not a limb of the slide. Pruning them is what
# keeps the tip-to-tip search cheap (far fewer tips to search from) as well as
# keeping the drawn centerline clean.
SPUR_STEPS = 3.0
PRUNE_PASSES = 6

# Ceiling on how many tips the diameter search starts from. Only reached on a
# pathologically hairy skeleton; the tips with the longest terminal branches are
# kept, which are the ones a real spine could plausibly end at.
MAX_SOURCES = 400


def centerline(poly):
    """(QgsGeometry LineString, method) for a polygon in a metric CRS.

    `method` is "medial axis" or "straight major axis" (the fallback), so the
    caller can tell the user which number they are looking at. Returns
    (None, reason) if even the fallback is impossible."""
    poly = _largest_part(poly)
    if poly is None or poly.isEmpty():
        return None, "the selected geometry has no polygon part"

    area = poly.area()
    perim = poly.length()          # for a polygon, length() is the perimeter
    if area <= 0 or perim <= 0:
        return None, "the selected polygon has zero area"

    step = max(perim / TARGET_VERTICES, MIN_STEP_M)
    pts = _resample_boundary(poly, step, _simplify_tol(step, area, perim))
    if len(pts) < 4:
        line = longest_chord(poly)
        return line, "straight major axis"

    edges = _interior_voronoi_edges(poly, pts)
    if not edges:
        line = longest_chord(poly)
        return line, "straight major axis"

    path = _longest_path(edges, spur_min=SPUR_STEPS * step)
    if path is None or len(path) < 2:
        line = longest_chord(poly)
        return line, "straight major axis"

    line = QgsGeometry.fromPolylineXY(path)
    line = line.simplify(step) or line
    # the medial axis retracts from convex corners, so it stops short of the
    # tips it points at; reach out to the boundary along each end's heading,
    # bounded so a misdirected end can't shoot across the whole polygon
    line = _extend_ends(line, poly, reach=math.sqrt(area))
    return line, "medial axis"


def longest_chord(poly):
    """Straight line between the two farthest-apart boundary vertices.

    The fallback when the skeleton degenerates (slivers, near-triangles). Cuts
    corners on a curving slide, hence only a fallback — never silently: the
    caller labels the measurement with the method that produced it."""
    poly = _largest_part(poly)
    if poly is None or poly.isEmpty():
        return None
    area, perim = poly.area(), poly.length()
    if perim <= 0:
        return None
    step = max(perim / 200.0, MIN_STEP_M)
    pts = _resample_boundary(poly, step, _simplify_tol(step, area, perim))
    if len(pts) < 2:
        return None
    best, pair = -1.0, None
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            d = (a.x() - b.x()) ** 2 + (a.y() - b.y()) ** 2
            if d > best:
                best, pair = d, (a, b)
    return QgsGeometry.fromPolylineXY([pair[0], pair[1]]) if pair else None


# ---------- geometry plumbing ----------
def _largest_part(geom):
    """The biggest polygon part, so a multipart scar measures its main body
    rather than joining across a gap to an outlying fragment."""
    if geom is None or geom.isEmpty():
        return None
    g = QgsGeometry(geom)
    if g.isMultipart():
        parts = [p for p in g.asGeometryCollection() if not p.isEmpty()]
        if not parts:
            return None
        g = max(parts, key=lambda p: p.area())
    return g


def _simplify_tol(step, area, perim):
    """Tolerance for the pre-simplification.

    A quarter of the resampling step normally, but never a meaningful fraction
    of the polygon's own WIDTH: area/perimeter is a width proxy (half the width
    for a long ribbon), and a tolerance approaching it flattens a narrow slide
    against itself, collapsing the skeleton and halving the measured length.
    Only bites on extreme aspect ratios, where it is the difference between a
    right answer and a badly wrong one."""
    tol = step / 4.0
    if perim > 0 and area > 0:
        tol = min(tol, 0.25 * area / perim)
    return max(tol, 0.0)


def _resample_boundary(poly, step, simplify_tol):
    """Boundary vertices at roughly even `step` spacing, deduplicated.

    Simplify first, then densify: simplifying alone would drop real bends, and
    densifying alone would leave a hand-digitized outline's dense stretches
    dense — and the Voronoi skeleton follows vertex DENSITY, so uneven spacing
    biases it toward the over-digitized side."""
    g = (poly.simplify(simplify_tol) if simplify_tol > 0 else poly) or poly
    g = g.densifyByDistance(step) or g
    seen, pts = set(), []
    for v in g.vertices():
        key = (round(v.x() / QUANT_M), round(v.y() / QUANT_M))
        if key in seen:
            continue
        seen.add(key)
        pts.append(QgsPointXY(v.x(), v.y()))
    return pts


def _interior_voronoi_edges(poly, pts):
    """[(QgsPointXY, QgsPointXY)] Voronoi edges lying wholly inside the polygon.

    The extent is padded well beyond the outline so no cell is clipped short
    inside it, and containment is tested through a prepared geometry engine
    because this runs over thousands of edges."""
    mp = QgsGeometry.fromMultiPointXY(pts)
    box = poly.boundingBox()
    pad = max(box.width(), box.height()) or 1.0
    box.grow(pad)
    try:
        vor = mp.voronoiDiagram(QgsGeometry.fromRect(box), 0.0, True)
    except Exception:
        return []
    if vor is None or vor.isEmpty():
        return []

    engine = None
    try:
        engine = QgsGeometry.createGeometryEngine(poly.constGet())
        engine.prepareGeometry()
    except Exception:
        engine = None

    edges = []
    for part in vor.asGeometryCollection():
        if engine is not None:
            inside = engine.contains(part.constGet())
        else:
            inside = poly.contains(part)
        if not inside:
            continue
        pl = part.asPolyline()
        for a, b in zip(pl, pl[1:]):
            edges.append((a, b))
    return edges


# ---------- longest path through the skeleton ----------
def _build_graph(edges):
    """(adjacency, node -> QgsPointXY) keyed by quantised coordinate."""
    adj = defaultdict(list)
    coords = {}
    for a, b in edges:
        ka = (round(a.x() / QUANT_M), round(a.y() / QUANT_M))
        kb = (round(b.x() / QUANT_M), round(b.y() / QUANT_M))
        if ka == kb:
            continue
        w = math.hypot(b.x() - a.x(), b.y() - a.y())
        adj[ka].append((kb, w))
        adj[kb].append((ka, w))
        coords[ka], coords[kb] = a, b
    return adj, coords


def _component(adj, start):
    seen, queue = {start}, deque([start])
    while queue:
        n = queue.popleft()
        for m, _w in adj[n]:
            if m not in seen:
                seen.add(m)
                queue.append(m)
    return seen


def _node_distances(adj, source, nodes):
    """Shortest-path distances from `source` over the raw (uncontracted) graph."""
    dist = {source: 0.0}
    pq = [(0.0, source)]
    while pq:
        d, n = heapq.heappop(pq)
        if d > dist.get(n, math.inf):
            continue
        for m, w in adj[n]:
            if m not in nodes:
                continue
            nd = d + w
            if nd < dist.get(m, math.inf):
                dist[m] = nd
                heapq.heappush(pq, (nd, m))
    return dist


def _contract(adj, nodes, coords):
    """Collapse runs of degree-2 nodes into single weighted edges.

    {node: [(other, length, [QgsPointXY...] from node to other)]}, a multigraph
    because the two sides of a cycle (a hole in the scar) are two separate
    chains between the same pair of junctions. Every chain is recorded from BOTH
    ends, which is what lets the path be rebuilt in traversal order later.

    This is the step that makes an exhaustive tip-to-tip search affordable: a
    skeleton of thousands of vertices collapses to its junctions and tips."""
    keep = {n for n in nodes if len(adj[n]) != 2}
    if not keep:
        # An unbroken loop of degree-2 nodes — no junctions, no tips. Breaking
        # it at ONE node would contract the whole loop into a pair of self-
        # edges and lose every distance in it, so break it at two roughly
        # antipodal nodes: the loop then becomes two parallel chains, and the
        # distance between their ends is half the loop.
        start = next(iter(nodes))
        dist = _node_distances(adj, start, nodes)
        keep = {start, max(dist, key=dist.get)}
    cadj = defaultdict(list)
    for u in keep:
        for first, w0 in adj[u]:
            if first not in nodes:
                continue
            pts = [coords[u], coords[first]]
            prev, cur, total = u, first, w0
            while cur not in keep:
                step = None
                for m, w in adj[cur]:
                    if m != prev and m in nodes:
                        step = (m, w)
                        break
                if step is None:
                    break               # dead end; keep what we walked
                prev, cur = cur, step[0]
                total += step[1]
                pts.append(coords[cur])
            cadj[u].append((cur, total, pts))
    return dict(cadj)


def _prune_spurs(cadj, spur_min):
    """Drop tip branches shorter than `spur_min` (the Voronoi hairs).

    Pruning is an optimisation, not the thing that makes the answer right —
    the tip-to-tip search below takes the longest path whether or not the hairs
    are still attached, so an unpruned hair only costs one more Dijkstra run
    while a wrongly pruned tip costs LENGTH. The rule is therefore deliberately
    conservative: drop a short tip branch only when its junction carries at
    least two OTHER branches that are strictly longer, i.e. the spine clearly
    passes through and this branch is not part of it. Testing on length alone
    would nibble the real ends of the spine away, one pass per iteration.

    Iterated, because removing one layer of hairs can expose another. Never
    prunes below two nodes — on a skeleton that is ALL hair there is nothing
    better to fall back to, and a short answer beats none."""
    cadj = {u: list(e) for u, e in cadj.items()}
    for _ in range(PRUNE_PASSES):
        drop = set()
        for u, e in cadj.items():
            if len(e) != 1:
                continue
            far, length, _pts = e[0]
            if far == u or length >= spur_min:
                continue
            others = [w for v, w, _p in cadj.get(far, ()) if v != u]
            if sum(1 for w in others if w > length) >= 2:
                drop.add(u)
        if not drop or len(cadj) - len(drop) < 2:
            break
        for u in drop:
            del cadj[u]
        for u in list(cadj):
            kept = [e for e in cadj[u] if e[0] not in drop]
            if kept:
                cadj[u] = kept
            else:
                del cadj[u]             # isolated by the pruning
    return cadj


def _cdijkstra(cadj, source):
    """(dist, prev) over the contracted multigraph; prev[m] = (from, chain)."""
    dist = {source: 0.0}
    prev = {}
    pq = [(0.0, source)]
    while pq:
        d, n = heapq.heappop(pq)
        if d > dist.get(n, math.inf):
            continue
        for m, w, pts in cadj.get(n, ()):
            nd = d + w
            if nd < dist.get(m, math.inf):
                dist[m] = nd
                prev[m] = (n, pts)
                heapq.heappush(pq, (nd, m))
    return dist, prev


def _longest_path(edges, spur_min=0.0):
    """[QgsPointXY] along the skeleton's diameter, or None.

    Only the largest connected component is searched, so a stray island of
    Voronoi edges can't win by accident. Within it the graph is contracted and
    de-haired, then Dijkstra runs from every tip and the best pair wins — exact
    regardless of cycles, unlike the usual two-pass sweep (see module
    docstring).

    One documented limit: contraction only keeps junctions and tips, so in a
    skeleton with NO tips at all (a loop — an annular scar) an endpoint that
    happens to sit mid-chain isn't a candidate, and the result can fall short of
    the true diameter by up to the length of the chain it sits in. That needs a
    hole big enough to split the skeleton AND a stretch of the loop long enough
    to contract, and it costs a fraction of a bend — not worth keeping every
    interior vertex searchable for."""
    adj, coords = _build_graph(edges)
    if len(adj) < 2:
        return None

    unvisited = set(adj)
    biggest = set()
    while unvisited:
        comp = _component(adj, next(iter(unvisited)))
        unvisited -= comp
        if len(comp) > len(biggest):
            biggest = comp
    if len(biggest) < 2:
        return None

    cadj = _prune_spurs(_contract(adj, biggest, coords), spur_min)
    tips = [u for u, e in cadj.items() if len(e) == 1]
    if tips:
        # longest terminal branch first, so the cap below keeps the tips a real
        # spine could plausibly end at
        sources = sorted(tips, key=lambda u: cadj[u][0][1], reverse=True)
    else:
        # a closed loop with no tips at all — the skeleton of an annular scar,
        # one with an unfailed island large enough to reach both ends. Search
        # from every junction instead: half the loop is a truer spine than the
        # straight-chord fallback, which would cut across the hole and leave
        # the polygon entirely.
        sources = list(cadj)
    if len(sources) > MAX_SOURCES:
        sources = sources[:MAX_SOURCES]

    best_len, best = 0.0, None
    for src in sources:
        dist, prev = _cdijkstra(cadj, src)
        if len(dist) < 2:
            continue
        far = max(dist, key=dist.get)
        if dist[far] > best_len:
            best_len, best = dist[far], (far, prev)
    if best is None:
        return None

    far, prev = best
    chains, n = [], far
    while n in prev:
        source_node, pts = prev[n]
        chains.append(pts)
        n = source_node
    chains.reverse()
    path = []
    for pts in chains:
        path.extend(pts if not path else pts[1:])
    return path if len(path) >= 2 else None


# ---------- reach the tips ----------
def _extend_ends(line, poly, reach):
    """Extend both ends of `line` along their heading out to the polygon
    boundary, so the measurement spans tip to tip rather than stopping where
    the medial axis retracts. An end whose boundary hit is farther than `reach`
    is left alone — that heading is pointing across the polygon, not out of a
    tip, and extending it would inflate the length."""
    pts = line.asPolyline()
    if len(pts) < 2:
        return line
    boundary = _boundary(poly)
    if boundary is None:
        return line

    head = _boundary_hit(pts[1], pts[0], boundary, reach)
    tail = _boundary_hit(pts[-2], pts[-1], boundary, reach)
    out = ([head] if head else []) + pts + ([tail] if tail else [])
    return QgsGeometry.fromPolylineXY(out)


def _boundary_hit(inner, end, boundary, reach):
    """First boundary crossing of the ray inner -> end, past `end` and within
    `reach`; None if there isn't one."""
    dx, dy = end.x() - inner.x(), end.y() - inner.y()
    norm = math.hypot(dx, dy)
    if norm <= 0:
        return None
    far = QgsPointXY(end.x() + dx / norm * reach, end.y() + dy / norm * reach)
    hit = QgsGeometry.fromPolylineXY([end, far]).intersection(boundary)
    if hit is None or hit.isEmpty():
        return None
    best, best_d = None, math.inf
    for part in hit.asGeometryCollection() or [hit]:
        for v in part.vertices():
            d = math.hypot(v.x() - end.x(), v.y() - end.y())
            if d < best_d:
                best, best_d = QgsPointXY(v.x(), v.y()), d
    if best is None or best_d > reach:
        return None
    return best


def _boundary(poly):
    """The polygon's rings as one line geometry (exterior + any holes)."""
    rings = poly.asPolygon()
    parts = [QgsGeometry.fromPolylineXY(r) for r in rings if len(r) > 1]
    if not parts:
        return None
    out = parts[0]
    for p in parts[1:]:
        out = out.combine(p)
    return out
