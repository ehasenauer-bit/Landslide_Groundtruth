"""Route loaded layers into named groups in the QGIS Layer Tree, so each tab's
output lands in a tidy, recognizable folder instead of piling up loose at the
project root.

The folder-name convention (built by each tab) is

    <source> <pre>/<post> [<radius>] <product>

e.g. "Planet 7-20/7-21 20km HONC", "SAR 7-20/7-21 Log-ratio",
"S2 7-20/7-21 20km NDVI". The optional <radius> is the search radius the run
used (radius_tag()); it lets two runs over the same dates but different AOI
sizes sit in their own folders instead of colliding.
Same scenes re-rendered with a different tone/product get a different <product>,
so they land in their own folder side by side instead of overwriting each other.

Every tab calls add_to_group() where it used to call QgsProject.addMapLayer(),
and remove_layer() where it used to call removeMapLayer() — the latter prunes a
folder once its last layer leaves, so cleared previews don't strand empty groups.

Accumulating outputs — the S2/Landsat "Run" and the SAR "change" compute, which
keep piling layers on rather than replacing them — instead open a FRESH folder per
run via new_group(): "S2 7-20/7-21", then "S2 7-20/7-21 (2)" if that one already
exists, so a new event/calculation never lands in a previous run's folder. Their
products/metrics go in subgroup()s inside that per-run folder.
"""

from qgis.core import QgsProject, QgsLayerTreeGroup


def _md(date):
    """'2024-07-20' -> '7-20'. Returns '' for blank/unparseable input."""
    parts = (date or "")[:10].split("-")
    if len(parts) == 3 and parts[1] and parts[2]:
        try:
            return f"{int(parts[1])}-{int(parts[2])}"
        except ValueError:
            return ""
    return ""


def date_pair(pre, post):
    """('2024-07-20', '2024-07-21') -> '7-20/7-21'; a missing side is dropped."""
    a, b = _md(pre), _md(post)
    if a and b:
        return f"{a}/{b}"
    return a or b or ""


def radius_tag(km):
    """Format a search radius for a group name: 20.0 -> '20km', 2.5 -> '2.5km'.

    Returns '' for a missing/zero/unparseable radius so name() simply drops it
    (a group without a known radius keeps the old, radius-less name)."""
    try:
        v = float(km)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    # drop a trailing '.0' so whole-km radii read '20km', not '20.0km'
    return f"{v:.1f}".rstrip("0").rstrip(".") + "km"


def name(*parts):
    """Join the non-empty parts of a group name with single spaces."""
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def group_node(group_name, project=None):
    """The named group directly under the tree root, created at the top if absent."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(group_name)
    if group is None:
        group = root.insertGroup(0, group_name)
    return group


def add_to_group(layer, group_name, project=None, top=True):
    """Register `layer` and place it inside `group_name` (created if needed).

    Drop-in for QgsProject.addMapLayer(layer): pass the same layer you would have
    added to the root. A blank group_name keeps the old root behaviour. Newest on
    top by default, so a fresh render overlays the one it supersedes."""
    project = project or QgsProject.instance()
    if not group_name:
        return project.addMapLayer(layer)
    project.addMapLayer(layer, False)          # False = don't add to the tree root
    group = group_node(group_name, project)
    return group.insertLayer(0, layer) if top else group.addLayer(layer)


def remove_layer(layer, project=None):
    """Remove `layer` and prune its containing group if that empties it.

    Drop-in for QgsProject.removeMapLayer(layer.id()); tolerant of a layer whose
    underlying C++ object has already been deleted (returns quietly)."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    try:
        lid = layer.id()
    except (RuntimeError, AttributeError):
        return
    node = root.findLayer(lid)
    parent = node.parent() if node is not None else None
    try:
        project.removeMapLayer(lid)
    except (RuntimeError, AttributeError):
        return
    if parent is not None and parent is not root and not parent.children():
        (parent.parent() or root).removeChildNode(parent)


def _unique_name(base, root):
    """`base`, or the first free `base (2)` / `base (3)` … not already a top-level
    group. Keeps a new run's folder from colliding with a previous run's."""
    if root.findGroup(base) is None:
        return base
    n = 2
    while root.findGroup(f"{base} ({n})") is not None:
        n += 1
    return f"{base} ({n})"


def new_group(base_name, project=None):
    """Open a FRESH top-level folder for one run — named `base_name`, or `base_name
    (2)`… if that is taken — and return its node for add_to()/subgroup(). Use this
    for accumulating outputs so each run gets its own folder instead of merging into
    a previous one; use add_to_group() for replace-in-place previews."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    return root.insertGroup(0, _unique_name(base_name, root))


def subgroup(parent, name):
    """Find-or-create the child folder `name` directly under the `parent` node."""
    for child in parent.children():
        if isinstance(child, QgsLayerTreeGroup) and child.name() == name:
            return child
    return parent.insertGroup(0, name)


def add_to(layer, group, project=None, top=True):
    """Register `layer` and drop it into an existing group NODE (from new_group /
    subgroup). The node-holding sibling of add_to_group()."""
    project = project or QgsProject.instance()
    project.addMapLayer(layer, False)
    return group.insertLayer(0, layer) if top else group.addLayer(layer)
