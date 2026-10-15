# -*- coding: utf-8 -*-
"""Hexagon Distance Map - diagnostic/safe pyRevit build.

Target: pyRevit / IronPython 2.7 / .NET Framework 4.8 / Revit 2022-2027.
R3.4 preserves source/destination preselection before face picking, isolates hard-crash
causes during detail-item placement, and uses Revit's own unit parser.
"""
from __future__ import division

import os
import math
import heapq
import datetime

from pyrevit import revit, DB, UI, forms, script

__title__ = "Hexagon\nDistance Map"
__author__ = "Aaron Rumple"

uidoc = revit.uidoc
doc = revit.doc
view = doc.ActiveView
output = script.get_output()

DEFAULT_RADIUS_TEXT = "500 mm"
DEFAULT_MAX_TEXT = "80000 mm"
TOLERANCE_FACTOR = 1.85
FACE_TOL_MM = 1.0
FAMILY_NAME = "DET_Hexagon"
TYPE_NAME = "DET_Hexagon"
RADIUS_PARAM = "Radius"
START_RGB = (125, 255, 2)
MID_RGB = (0, 67, 255)
END_RGB = (255, 0, 0)
SAFE_BATCH = 25
OVERRIDE_BATCH = 100

ALLOWED_VIEW_TYPES = (
    DB.ViewType.FloorPlan,
    DB.ViewType.CeilingPlan,
    DB.ViewType.Detail,
    DB.ViewType.DraftingView,
    DB.ViewType.AreaPlan,
)

# Crash-survivable log. Every risky call is bracketed by a flushed line.
LOG_PATH = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", r"C:\Temp")),
                        "PyPiPie_Hexagon_Distance_Map.log")


def log_line(message):
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        f = open(LOG_PATH, "a")
        try:
            f.write("{0} | {1}\n".format(stamp, message))
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        finally:
            f.close()
    except Exception:
        pass




def element_id_value(element_id):
    """Return an ElementId as a Python int across Revit API versions.

    Revit 2024+ exposes the 64-bit Value property. Older API versions use
    IntegerValue. Keep both so the same pyRevit tool runs in Revit 2022-2027.
    """
    if element_id is None:
        return -1
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return -1



def get_element_name(element):
    """Return a Revit Element name without relying on IronPython property binding."""
    if element is None:
        return ""

    # Most reliable cross-version/python-engine route for inherited Element.Name.
    try:
        value = DB.Element.Name.GetValue(element)
        if value:
            return str(value)
    except Exception:
        pass

    # Parameter fallbacks for ElementType / FamilySymbol names.
    for bip in (
        DB.BuiltInParameter.SYMBOL_NAME_PARAM,
        DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,
        DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME,
    ):
        try:
            param = element.get_Parameter(bip)
            if param:
                value = param.AsString()
                if value:
                    return value
        except Exception:
            pass

    # Last resort for API objects where direct Name binding does work.
    try:
        value = getattr(element, "Name")
        if value:
            return str(value)
    except Exception:
        pass
    return ""

def mm_to_internal(value):
    try:
        return DB.UnitUtils.ConvertToInternalUnits(float(value), DB.UnitTypeId.Millimeters)
    except Exception:
        return DB.UnitUtils.ConvertToInternalUnits(float(value), DB.DisplayUnitType.DUT_MILLIMETERS)


def internal_to_mm(value):
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(float(value), DB.UnitTypeId.Millimeters)
    except Exception:
        return DB.UnitUtils.ConvertFromInternalUnits(float(value), DB.DisplayUnitType.DUT_MILLIMETERS)


def parse_revit_length(text_value):
    """Return internal feet using Revit's project-unit parser."""
    if text_value is None:
        return None
    value = text_value.strip()
    if not value:
        return None

    units = doc.GetUnits()
    try:
        parsed = DB.UnitFormatUtils.TryParse(units, DB.SpecTypeId.Length, value)
        if isinstance(parsed, tuple):
            if parsed[0]:
                return float(parsed[1])
        elif parsed:
            return float(parsed)
    except Exception:
        pass

    # Legacy API fallback for older Revit versions.
    try:
        parsed = DB.UnitFormatUtils.TryParse(units, DB.UnitType.UT_Length, value)
        if isinstance(parsed, tuple) and parsed[0]:
            return float(parsed[1])
    except Exception:
        pass
    return None


def ask_positive_length(prompt, default_text):
    value = forms.ask_for_string(default=default_text,
                                 prompt=prompt,
                                 title=__title__.replace("\n", " "))
    if value is None:
        script.exit()
    number = parse_revit_length(value)
    if number is None or number <= 0.0:
        forms.alert(
            "Revit could not parse a positive length.\n\n"
            "Examples: 500 mm, 50 cm, 0.5 m, 18\", 1'-6\", 1 1/2\".\n"
            "A unitless value uses the project's current length units.",
            title=__title__.replace("\n", " "), exitscript=True)
    return number, value


def get_symbol():
    exact = None
    family_only = []
    for sym in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol):
        try:
            if get_element_name(sym.Family) != FAMILY_NAME:
                continue
            family_only.append(sym)
            if get_element_name(sym) == TYPE_NAME:
                exact = sym
                break
        except Exception:
            pass
    if exact:
        return exact
    if len(family_only) == 1:
        return family_only[0]
    return None


def element_location(el):
    """Return a representative XYZ for common Revit element kinds.

    Dynamo Element.GetLocation is permissive across several element types.
    Mirror that behavior as closely as practical without ProtoGeometry.
    """
    if el is None:
        return None

    try:
        loc = el.Location
    except Exception:
        loc = None

    if isinstance(loc, DB.LocationPoint):
        try:
            return loc.Point
        except Exception:
            pass

    if isinstance(loc, DB.LocationCurve):
        try:
            return loc.Curve.Evaluate(0.5, True)
        except Exception:
            pass

    # ReferencePoint and similar point-like API elements.
    try:
        pt = el.Position
        if isinstance(pt, DB.XYZ):
            return pt
    except Exception:
        pass

    # FamilyInstance / instance-like elements can expose a transform origin even
    # when IronPython does not bind Location as expected.
    try:
        tr = el.GetTransform()
        if tr is not None and isinstance(tr.Origin, DB.XYZ):
            return tr.Origin
    except Exception:
        pass

    # Prefer the active-view bounding box for view-specific/detail elements.
    try:
        bb = el.get_BoundingBox(view)
        if bb:
            return (bb.Min + bb.Max) * 0.5
    except Exception:
        pass

    # Then try the model bounding box.
    try:
        bb = el.get_BoundingBox(None)
        if bb:
            return (bb.Min + bb.Max) * 0.5
    except Exception:
        pass

    return None


def face_bbox(face):
    """Return XYZ bounds of a Face without relying on Mesh.NumVertices.

    Revit Mesh exposes triangles consistently across the supported API range.
    Some Revit/IronPython combinations do not expose NumVertices on Mesh, so
    walk every triangle and vertex instead.
    """
    mesh = face.Triangulate()
    if mesh is None:
        return None

    try:
        tri_count = int(mesh.NumTriangles)
    except Exception:
        tri_count = 0

    if tri_count <= 0:
        return None

    xmin = ymin = zmin = None
    xmax = ymax = zmax = None

    for i in range(tri_count):
        tri = mesh.get_Triangle(i)
        if tri is None:
            continue
        for j in range(3):
            pt = tri.get_Vertex(j)
            if pt is None:
                continue
            if xmin is None:
                xmin = xmax = pt.X
                ymin = ymax = pt.Y
                zmin = zmax = pt.Z
            else:
                xmin = min(xmin, pt.X)
                xmax = max(xmax, pt.X)
                ymin = min(ymin, pt.Y)
                ymax = max(ymax, pt.Y)
                zmin = min(zmin, pt.Z)
                zmax = max(zmax, pt.Z)

    if xmin is None:
        return None
    return xmin, ymin, zmin, xmax, ymax, zmax


def point_on_face(face, pt, tolerance):
    try:
        result = face.Project(pt)
        if result is None or result.Distance > tolerance:
            return False
        try:
            return bool(face.IsInside(result.UVPoint))
        except Exception:
            return True
    except Exception:
        return False


def generate_centres(face, radius_ft):
    bb = face_bbox(face)
    if not bb:
        return []
    xmin, ymin, zmin, xmax, ymax, zmax = bb
    z = (zmin + zmax) * 0.5
    dx = 1.5 * radius_ft
    dy = math.sqrt(3.0) * radius_ft
    half_dy = dy * 0.5
    ncols = int(math.ceil((xmax - xmin) / dx)) + 2
    nrows = int(math.ceil((ymax - ymin) / dy)) + 2
    tol = mm_to_internal(FACE_TOL_MM)
    centres = []
    for col in range(ncols):
        x = xmin + col * dx
        yoff = half_dy if col % 2 else 0.0
        for row in range(nrows):
            y = ymin + yoff + row * dy
            if not (xmin <= x <= xmax and ymin <= y <= ymax):
                continue
            p = DB.XYZ(x, y, z)
            if point_on_face(face, p, tol):
                centres.append(p)
    return centres


def project_to_view_plane(pt):
    """Project an XYZ onto the active view plane before view-based placement."""
    try:
        origin = view.Origin
        normal = view.ViewDirection.Normalize()
        v = pt - origin
        return pt - normal.Multiply(v.DotProduct(normal))
    except Exception:
        return pt


def dist2d(a, b):
    dx = a.X - b.X
    dy = a.Y - b.Y
    return math.sqrt(dx * dx + dy * dy)


def nearest_source_indices(centres, source_points):
    result = []
    for pt in source_points:
        best_i = -1
        best_d = float("inf")
        for i, c in enumerate(centres):
            d = dist2d(c, pt)
            if d < best_d:
                best_d = d
                best_i = i
        if best_i >= 0 and best_i not in result:
            result.append(best_i)
    return result


def build_adjacency(centres, radius_ft):
    edge_len = math.sqrt(3.0) * radius_ft
    threshold = edge_len * TOLERANCE_FACTOR + 1e-9
    cell = threshold
    buckets = {}
    for i, p in enumerate(centres):
        key = (int(math.floor(p.X / cell)), int(math.floor(p.Y / cell)))
        buckets.setdefault(key, []).append(i)
    adj = [[] for _ in centres]
    for i, p in enumerate(centres):
        kx = int(math.floor(p.X / cell))
        ky = int(math.floor(p.Y / cell))
        for bx in range(kx - 1, kx + 2):
            for by in range(ky - 1, ky + 2):
                for j in buckets.get((bx, by), []):
                    if j <= i:
                        continue
                    d = dist2d(p, centres[j])
                    if d <= threshold:
                        adj[i].append((j, d))
                        adj[j].append((i, d))
    return adj


def dijkstra(centres, source_points, radius_ft):
    n = len(centres)
    if n == 0:
        return []
    sources = nearest_source_indices(centres, source_points)
    if not sources:
        return [0.0] * n
    adj = build_adjacency(centres, radius_ft)
    distances = [float("inf")] * n
    pq = []
    for s in sources:
        distances[s] = 0.0
        heapq.heappush(pq, (0.0, s))
    while pq:
        current, u = heapq.heappop(pq)
        if current > distances[u]:
            continue
        for v, weight in adj[u]:
            nd = current + weight
            if nd < distances[v]:
                distances[v] = nd
                heapq.heappush(pq, (nd, v))
    finite = [d for d in distances if d != float("inf")]
    sentinel = max(finite) * 1.1 if finite else 0.0
    return [d if d != float("inf") else sentinel for d in distances]


def clamp_byte(v):
    return max(0, min(255, int(round(v))))


def lerp_rgb(a, b, t):
    t = max(0.0, min(1.0, t))
    return (
        clamp_byte(a[0] + (b[0] - a[0]) * t),
        clamp_byte(a[1] + (b[1] - a[1]) * t),
        clamp_byte(a[2] + (b[2] - a[2]) * t),
    )


def color_for_distance(distance_ft, min_ft, max_ft):
    span = max_ft - min_ft
    if distance_ft >= max_ft or span <= 0.0:
        rgb = END_RGB
    else:
        rgb = lerp_rgb(START_RGB, MID_RGB, (distance_ft - min_ft) / span)
    return DB.Color(rgb[0], rgb[1], rgb[2])


def make_override(color):
    ogs = DB.OverrideGraphicSettings()
    for method in ("SetProjectionLineColor", "SetCutLineColor",
                   "SetProjectionFillColor", "SetCutFillColor"):
        try:
            getattr(ogs, method)(color)
        except Exception:
            pass
    return ogs


def activate_symbol(symbol):
    if symbol.IsActive:
        return
    log_line("BEFORE symbol.Activate")
    tx = DB.Transaction(doc, "Activate DET_Hexagon")
    tx.Start()
    try:
        symbol.Activate()
        doc.Regenerate()
        tx.Commit()
        log_line("AFTER symbol.Activate COMMITTED")
    except Exception:
        try:
            tx.RollBack()
        except Exception:
            pass
        log_line("ERROR symbol.Activate")
        raise


def set_radius(inst, radius_ft):
    param = inst.LookupParameter(RADIUS_PARAM)
    if param is None:
        raise Exception("Radius parameter not found.")
    if param.IsReadOnly:
        raise Exception("Radius parameter is read-only.")
    if param.StorageType != DB.StorageType.Double:
        raise Exception("Radius parameter is not a Length/Double parameter.")
    param.Set(radius_ft)


def place_one(symbol, pt, radius_ft, write_radius, label):
    """One instance in one transaction, with durable logging around calls."""
    tx = DB.Transaction(doc, label)
    log_line("BEFORE tx.Start | {0}".format(label))
    tx.Start()
    try:
        p = project_to_view_plane(pt)
        log_line("BEFORE NewFamilyInstance | {0} | XYZ={1:.9f},{2:.9f},{3:.9f}".format(
            label, p.X, p.Y, p.Z))
        inst = doc.Create.NewFamilyInstance(p, symbol, view)
        log_line("AFTER NewFamilyInstance | {0} | Id={1}".format(label, element_id_value(inst.Id) if inst else -1))
        if inst is None:
            raise Exception("Revit returned no FamilyInstance.")
        if write_radius:
            log_line("BEFORE Radius.Set | {0} | Id={1} | ft={2:.9f}".format(
                label, element_id_value(inst.Id), radius_ft))
            set_radius(inst, radius_ft)
            log_line("AFTER Radius.Set | {0} | Id={1}".format(label, element_id_value(inst.Id)))
        log_line("BEFORE tx.Commit | {0}".format(label))
        status = tx.Commit()
        log_line("AFTER tx.Commit | {0} | {1}".format(label, status))
        if status != DB.TransactionStatus.Committed:
            raise Exception("Transaction status: {0}".format(status))
        return inst
    except Exception as ex:
        log_line("EXCEPTION | {0} | {1}".format(label, ex))
        try:
            if tx.GetStatus() == DB.TransactionStatus.Started:
                tx.RollBack()
        except Exception:
            pass
        raise


def safe_bulk_place(symbol, centres, distances, radius_ft, limit=None):
    total = len(centres) if limit is None else min(len(centres), limit)
    created = []
    created_dist = []
    failures = []

    with forms.ProgressBar(title="Placing hexagons {value}/{max_value}",
                           cancellable=True, step=1) as pb:
        for start in range(0, total, SAFE_BATCH):
            if pb.cancelled:
                break
            end = min(start + SAFE_BATCH, total)
            tx = DB.Transaction(doc, "Place Hexagons {0}-{1}".format(start + 1, end))
            log_line("BEFORE batch tx.Start | {0}-{1}".format(start + 1, end))
            tx.Start()
            batch = []
            batch_dist = []
            try:
                for i in range(start, end):
                    p = project_to_view_plane(centres[i])
                    log_line("BEFORE NewFamilyInstance | index={0} | XYZ={1:.9f},{2:.9f},{3:.9f}".format(
                        i, p.X, p.Y, p.Z))
                    inst = doc.Create.NewFamilyInstance(p, symbol, view)
                    log_line("AFTER NewFamilyInstance | index={0} | Id={1}".format(
                        i, element_id_value(inst.Id) if inst else -1))
                    if inst is None:
                        raise Exception("Revit returned no FamilyInstance at index {0}".format(i))
                    log_line("BEFORE Radius.Set | index={0} | Id={1}".format(i, element_id_value(inst.Id)))
                    set_radius(inst, radius_ft)
                    log_line("AFTER Radius.Set | index={0} | Id={1}".format(i, element_id_value(inst.Id)))
                    batch.append(inst)
                    batch_dist.append(distances[i])
                    pb.update_progress(i + 1, total)
                log_line("BEFORE batch tx.Commit | {0}-{1}".format(start + 1, end))
                status = tx.Commit()
                log_line("AFTER batch tx.Commit | {0}-{1} | {2}".format(start + 1, end, status))
                if status != DB.TransactionStatus.Committed:
                    raise Exception("Batch transaction status: {0}".format(status))
                created.extend(batch)
                created_dist.extend(batch_dist)
            except Exception as ex:
                log_line("EXCEPTION batch | {0}-{1} | {2}".format(start + 1, end, ex))
                try:
                    if tx.GetStatus() == DB.TransactionStatus.Started:
                        tx.RollBack()
                except Exception:
                    pass
                failures.append(["{0}-{1}".format(start + 1, end), str(ex)])
                break
    return created, created_dist, failures


def apply_overrides(created, distances, max_distance_ft):
    if not created:
        return []
    failures = []
    min_ft = min(distances)
    total = len(created)
    with forms.ProgressBar(title="Coloring hexagons {value}/{max_value}",
                           cancellable=True, step=1) as pb:
        for start in range(0, total, OVERRIDE_BATCH):
            if pb.cancelled:
                break
            end = min(start + OVERRIDE_BATCH, total)
            tx = DB.Transaction(doc, "Color Hexagons {0}-{1}".format(start + 1, end))
            log_line("BEFORE override tx.Start | {0}-{1}".format(start + 1, end))
            tx.Start()
            try:
                for i in range(start, end):
                    color = color_for_distance(distances[i], min_ft, max_distance_ft)
                    log_line("BEFORE SetElementOverrides | index={0} | Id={1}".format(
                        i, element_id_value(created[i].Id)))
                    view.SetElementOverrides(created[i].Id, make_override(color))
                    log_line("AFTER SetElementOverrides | index={0}".format(i))
                    pb.update_progress(i + 1, total)
                status = tx.Commit()
                log_line("AFTER override tx.Commit | {0}-{1} | {2}".format(start + 1, end, status))
            except Exception as ex:
                log_line("EXCEPTION override batch | {0}-{1} | {2}".format(start + 1, end, ex))
                try:
                    if tx.GetStatus() == DB.TransactionStatus.Started:
                        tx.RollBack()
                except Exception:
                    pass
                failures.append(["{0}-{1}".format(start + 1, end), str(ex)])
                break
    return failures


# ---------------- Main ----------------
log_line("=" * 72)
log_line("START R3.3 | Revit={0} | ViewId={1} | ViewType={2}".format(
    getattr(doc.Application, "VersionNumber", "?"),
    element_id_value(view.Id) if view else -1,
    view.ViewType if view else "None"))

if view is None or view.ViewType not in ALLOWED_VIEW_TYPES:
    forms.alert("Run this tool from a Floor Plan, RCP, Detail, Drafting, or Area Plan view.", exitscript=True)

mode = forms.CommandSwitchWindow.show(
    [
        "1 - Compute only (NO Revit placement)",
        "2 - Place ONE hex, no Radius write",
        "3 - Place ONE hex + Radius write",
        "4 - Place first 10 hexes + Radius",
        "5 - Full map (safe batches + color)",
        "6 - Show previous crash log",
    ],
    message="Choose a diagnostic stage. Start at 1 and move down only after the previous stage succeeds.",
    title="Hexagon Distance Map - R3.5 Diagnostic")

if mode is None:
    script.exit()

if mode.startswith("6"):
    try:
        if os.path.exists(LOG_PATH):
            data = open(LOG_PATH, "r").read()
            output.print_md("## Hexagon Distance Map Crash Log")
            output.print_md("`{0}`".format(LOG_PATH))
            output.print_code(data[-12000:])
        else:
            forms.alert("No diagnostic log exists yet.\n\n{0}".format(LOG_PATH))
    except Exception as ex:
        forms.alert("Could not read the log:\n\n{0}".format(ex))
    script.exit()

radius_ft, radius_text = ask_positive_length("Hexagon radius (Revit length):", DEFAULT_RADIUS_TEXT)
max_distance_ft, max_distance_text = ask_positive_length("Maximum gradient distance (Revit length):", DEFAULT_MAX_TEXT)

symbol = get_symbol()
if symbol is None:
    forms.alert("Could not find detail-item family/type '{0}'.".format(FAMILY_NAME), exitscript=True)

try:
    placement_type = symbol.Family.FamilyPlacementType
except Exception:
    placement_type = "Unknown"

log_line("SYMBOL | Family={0} | Type={1} | PlacementType={2} | Active={3}".format(
    get_element_name(symbol.Family), get_element_name(symbol), placement_type, symbol.IsActive))

# Capture the user's source/destination preselection BEFORE PickObject.
# PickObject can alter the UI selection state, but the Dynamo graph's intended
# workflow is: preselect destination points -> start tool -> pick face.
preselected_ids = []
try:
    preselected_ids = list(uidoc.Selection.GetElementIds())
except Exception:
    preselected_ids = []

log_line("PRESELECTION | count={0} | ids={1}".format(
    len(preselected_ids),
    ",".join([str(element_id_value(x)) for x in preselected_ids])))

try:
    face_ref = uidoc.Selection.PickObject(UI.Selection.ObjectType.Face,
                                          "Pick the face to cover with the hexagon map")
except Exception:
    script.exit()

owner = doc.GetElement(face_ref.ElementId)
face = owner.GetGeometryObjectFromReference(face_ref)
if face is None:
    forms.alert("The selected reference did not resolve to a Revit face.", exitscript=True)

# Prefer the preselection captured before the face pick. If nothing was
# preselected, fall back to an explicit post-face multi-selection.
source_ids = [eid for eid in preselected_ids if eid != face_ref.ElementId]
selection_source = "preselection"

if not source_ids:
    selection_source = "interactive selection"
    try:
        refs = uidoc.Selection.PickObjects(
            UI.Selection.ObjectType.Element,
            "Select source/destination elements for the distance map, then click Finish")
        source_ids = [r.ElementId for r in refs if r.ElementId != face_ref.ElementId]
    except Exception:
        script.exit()

if not source_ids:
    forms.alert(
        "No source/destination elements were selected.\n\n"
        "Preselect the destination/source point elements before starting the tool, "
        "or select them when prompted.",
        exitscript=True)

source_points = []
skipped_source_ids = []
for eid in source_ids:
    el = doc.GetElement(eid)
    pt = element_location(el) if el else None
    if pt is not None:
        source_points.append(pt)
        log_line("SOURCE OK | id={0} | type={1} | xyz={2},{3},{4}".format(
            element_id_value(eid), el.GetType().FullName if el else "None",
            pt.X, pt.Y, pt.Z))
    else:
        skipped_source_ids.append(eid)
        log_line("SOURCE SKIP | id={0} | type={1}".format(
            element_id_value(eid), el.GetType().FullName if el else "None"))

if not source_points:
    ids_text = ", ".join([str(element_id_value(x)) for x in source_ids])
    forms.alert(
        "The selected source/destination elements do not expose usable locations.\n\n"
        "Selection source: {0}\n"
        "ElementIds: {1}\n\n"
        "The diagnostic log records each selected element type.".format(
            selection_source, ids_text),
        exitscript=True)

log_line("BEFORE generate_centres")
centres = generate_centres(face, radius_ft)
log_line("AFTER generate_centres | count={0}".format(len(centres)))
if not centres:
    forms.alert("No hexagon centers fell on the selected face.", exitscript=True)

log_line("BEFORE dijkstra")
distances_ft = dijkstra(centres, source_points, radius_ft)
log_line("AFTER dijkstra | count={0}".format(len(distances_ft)))

output.print_md("## Hexagon Distance Map - R3.5 Diagnostic")
output.print_md("**Mode:** {0}  ".format(mode))
output.print_md("**Hex centers computed:** {0}  ".format(len(centres)))
output.print_md("**Sources:** {0}  ".format(len(source_points)))
output.print_md("**Source selection:** {0}  ".format(selection_source))
output.print_md("**Radius:** {0}  ".format(radius_text))
output.print_md("**Gradient limit:** {0}  ".format(max_distance_text))
output.print_md("**Family placement type:** {0}  ".format(placement_type))
output.print_md("**Diagnostic log:** `{0}`".format(LOG_PATH))

if mode.startswith("1"):
    log_line("END compute-only success")
    forms.alert("Compute-only stage succeeded.\n\nHex centers: {0}\nNo Revit elements were created.\n\nNext test: option 2.".format(len(centres)),
                title="Hexagon Distance Map - R3")
    script.exit()

activate_symbol(symbol)

if mode.startswith("2"):
    inst = place_one(symbol, centres[0], radius_ft, False, "Diagnostic - One Hex No Radius")
    output.print_md("\n**Placed one hex:** {0}".format(output.linkify(inst.Id)))
    log_line("END option 2 success")
    forms.alert("One hex placed successfully WITHOUT writing Radius.\n\nNext test: option 3.",
                title="Hexagon Distance Map - R3")
    script.exit()

if mode.startswith("3"):
    inst = place_one(symbol, centres[0], radius_ft, True, "Diagnostic - One Hex With Radius")
    output.print_md("\n**Placed one hex + Radius:** {0}".format(output.linkify(inst.Id)))
    log_line("END option 3 success")
    forms.alert("One hex placed successfully WITH the Radius parameter.\n\nNext test: option 4.",
                title="Hexagon Distance Map - R3")
    script.exit()

if mode.startswith("4"):
    created, created_dist, failures = safe_bulk_place(symbol, centres, distances_ft, radius_ft, limit=10)
    output.print_md("\n**Placed:** {0} / {1}".format(len(created), min(10, len(centres))))
    if failures:
        output.print_table(table_data=failures, columns=["Batch", "Reason"])
    log_line("END option 4 | created={0} | failures={1}".format(len(created), len(failures)))
    forms.alert("10-hex diagnostic finished.\n\nCreated: {0}\nFailures: {1}\n\nIf Revit remained stable, test option 5.".format(
        len(created), len(failures)), title="Hexagon Distance Map - R3")
    script.exit()

# Full mode: explicit confirmation with generated count before any creation.
if len(centres) > 2000:
    proceed = forms.alert(
        "This face will create {0:,} detail items.\n\nThat is a large number of view-based family instances and may heavily load Revit.\n\nContinue?".format(len(centres)),
        title="Hexagon Distance Map - R3",
        yes=True, no=True)
    if not proceed:
        script.exit()

created, created_dist, failures = safe_bulk_place(symbol, centres, distances_ft, radius_ft)
override_failures = []
if created and not failures:
    override_failures = apply_overrides(created, created_dist, max_distance_ft)

output.print_md("\n### Full-map result")
output.print_md("**Created:** {0}  ".format(len(created)))
output.print_md("**Placement failures:** {0}  ".format(len(failures)))
output.print_md("**Override failures:** {0}".format(len(override_failures)))
if failures:
    output.print_table(table_data=failures, columns=["Placement batch", "Reason"])
if override_failures:
    output.print_table(table_data=override_failures, columns=["Override batch", "Reason"])
log_line("END full mode | created={0} | placement_failures={1} | override_failures={2}".format(
    len(created), len(failures), len(override_failures)))
