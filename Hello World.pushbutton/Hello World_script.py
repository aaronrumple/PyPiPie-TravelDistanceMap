# -*- coding: utf-8 -*-
from Autodesk.Revit.DB import *
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import PickBoxStyle

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
view = doc.ActiveView

TOL = 0.001
PARALLEL_TOL = 0.98
MAX_TRIM_DISTANCE = 50.0  # feet

try:
    picked_box = uidoc.Selection.PickBox(
        PickBoxStyle.Directional,
        "Window wall ends to trim back to nearest unbound wall edge"
    )
except:
    raise SystemExit

p1 = picked_box.Min
p2 = picked_box.Max

min_x = min(p1.X, p2.X)
max_x = max(p1.X, p2.X)
min_y = min(p1.Y, p2.Y)
max_y = max(p1.Y, p2.Y)

def point_in_window(pt):
    return min_x <= pt.X <= max_x and min_y <= pt.Y <= max_y

def get_wall_line(wall):
    loc = wall.Location
    if isinstance(loc, LocationCurve) and isinstance(loc.Curve, Line):
        return loc.Curve
    return None

def vec2(v):
    return XYZ(v.X, v.Y, 0)

def norm2(v):
    return vec2(v).Normalize()

def cross2(a, b):
    return a.X * b.Y - a.Y * b.X

def wall_dir(line):
    return norm2(line.GetEndPoint(1) - line.GetEndPoint(0))

def is_parallel(l1, l2):
    return abs(wall_dir(l1).DotProduct(wall_dir(l2))) > PARALLEL_TOL

def wall_width(wall):
    try:
        return wall.Width
    except:
        return 0.0

def wall_end_ray(line, end_index):
    p0 = line.GetEndPoint(0)
    p1 = line.GetEndPoint(1)

    if end_index == 0:
        return p0, norm2(p1 - p0)
    else:
        return p1, norm2(p0 - p1)

def unbound_wall_edge(line, offset):
    """
    Returns a point and direction for an infinite wall edge line.
    The edge is offset from the wall centerline by +/- half wall width.
    """
    d = wall_dir(line)
    n = XYZ(-d.Y, d.X, 0)

    p0 = line.GetEndPoint(0)

    edge_p = XYZ(
        p0.X + n.X * offset,
        p0.Y + n.Y * offset,
        p0.Z
    )

    return edge_p, d

def intersect_unbound_lines_2d(p, r, q, s):
    """
    Intersects:
    p + t*r
    q + u*s

    Both are treated as unbound lines.
    Returns t and intersection point along first line.
    """
    denom = cross2(r, s)
    if abs(denom) < TOL:
        return None

    qmp = q - p
    t = cross2(qmp, s) / denom

    pt = XYZ(
        p.X + r.X * t,
        p.Y + r.Y * t,
        p.Z
    )

    return t, pt

def nearest_unbound_wall_edge_trim_point(source_wall, end_index, target_wall):
    src_line = get_wall_line(source_wall)
    tgt_line = get_wall_line(target_wall)

    if not src_line or not tgt_line:
        return None

    if is_parallel(src_line, tgt_line):
        return None

    ray_p, ray_d = wall_end_ray(src_line, end_index)

    half_width = wall_width(target_wall) / 2.0

    best_pt = None
    best_t = None

    # Use both infinite physical wall edge lines.
    for offset in [half_width, -half_width]:
        edge_p, edge_d = unbound_wall_edge(tgt_line, offset)

        result = intersect_unbound_lines_2d(ray_p, ray_d, edge_p, edge_d)
        if not result:
            continue

        t, pt = result

        # Only trim toward the inside direction of this wall end.
        if t < -TOL:
            continue

        if t > MAX_TRIM_DISTANCE:
            continue

        if best_t is None or t < best_t:
            best_t = t
            best_pt = pt

    return best_pt

walls = list(
    FilteredElementCollector(doc, view.Id)
    .OfClass(Wall)
    .WhereElementIsNotElementType()
)

changed_joins = 0
trimmed_ends = 0
touched_walls = set()

t = Transaction(doc, "Trim Wall Ends to Nearest Unbound Wall Edge")
t.Start()

try:
    for wall in walls:
        line = get_wall_line(wall)
        if not line:
            continue

        start_pt = line.GetEndPoint(0)
        end_pt = line.GetEndPoint(1)

        new_start = start_pt
        new_end = end_pt
        update_wall = False

        for end_index, wall_end in enumerate([start_pt, end_pt]):
            if not point_in_window(wall_end):
                continue

            touched_walls.add(wall.Id.IntegerValue)

            try:
                if WallUtils.IsWallJoinAllowedAtEnd(wall, end_index):
                    WallUtils.DisallowWallJoinAtEnd(wall, end_index)
                    changed_joins += 1
            except:
                pass

            best_pt = None
            best_dist = None

            for target_wall in walls:
                if target_wall.Id == wall.Id:
                    continue

                pt = nearest_unbound_wall_edge_trim_point(wall, end_index, target_wall)
                if not pt:
                    continue

                dist = wall_end.DistanceTo(pt)

                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_pt = pt

            if best_pt:
                if end_index == 0:
                    new_start = best_pt
                else:
                    new_end = best_pt

                update_wall = True
                trimmed_ends += 1

        if update_wall and new_start.DistanceTo(new_end) > TOL:
            wall.Location.Curve = Line.CreateBound(new_start, new_end)

    t.Commit()

    TaskDialog.Show(
        "Complete",
        "Disallowed joins at {} endpoint(s).\nTrimmed {} endpoint(s) to nearest unbound wall edge.\nTouched {} wall(s).".format(
            changed_joins,
            trimmed_ends,
            len(touched_walls)
        )
    )

except Exception as e:
    t.RollBack()
    TaskDialog.Show("Error", str(e))