from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsSpatialIndex,
    QgsRectangle,
    QgsLineSymbol,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingOutputVectorLayer,
    QgsProcessingOutputNumber,
)

MULTIPLE_LAYER_TYPE = getattr(
    QgsProcessing,
    "TypeVectorLine",
    getattr(
        QgsProcessing,
        "TypeVectorAny",
        QgsProcessing.TypeVector,
    ),
)
from qgis.PyQt.QtCore import QVariant

import math
import re
from dataclasses import dataclass, field


# =============================================================================
# Cartographic Multi-Route Layout
#
# Automatic cartographic layout of overlapping routes. / Automatisk kartografisk layout af overlappende ruter.
#
# Version: 8.3
#
# Version history / Versionshistorik
# 7.x  Development
# 7.4  Stable cartographic algorithm
# 8.0  Processing Tool architecture
# 8.2  Cartographic Multi-Route Layout
# 8.3  Topology-first corridor equivalence (corrected_routes diagnostics)
# 8.4  assemble_route_path driven directly by per-point corridor
#      proximity, not by each route's own classify_route_sets runs
# 8.5  Spatial grid index for per-point corridor matching - fixes a
#      real stall on real route data (was scanning each corridor's
#      full geometry per route point; confirmed ~16x faster on a
#      6-route/44km real GPX dataset)
# 9.0  Replaced bottom-up corridor detection/matching with a real
#      topology graph: nodes at junctions, edges tagged with route
#      membership, each route an explicit path through the graph
#      decided once at insertion time - never re-derived by search.
#      classify_route_sets/materialize_corridors/merge_corridors/
#      resolve_corridor_equivalence/build_corridor_route_index/
#      assemble_route_path all replaced by build_route_network() and
#      assemble_route_path_from_graph(). Confirmed on real 6-route GPX
#      data: total pipeline time 0.91s (was ~50s pre-grid, ~3.1s
#      post-grid), and the route pair that still failed to separate at
#      a junction after every prior fix this cycle (8.3-8.5) now shows
#      a clean 20m lane separation.
#
# =============================================================================


VERSION = "9.0"

ENGINE_FEEDBACK = None


def debug(*args, feedback=None):
    """Push logtekst til QGIS feedback eller til standard output."""
    text = " ".join(str(arg) for arg in args)
    active_feedback = feedback if feedback is not None else ENGINE_FEEDBACK
    if active_feedback is not None:
        active_feedback.pushInfo(text)
    else:
        print(text)


# ==================================================
# PARAMETERMODEL
#
# Kun værdier i DEFAULT_PARAMETERS skal normalt ændres.
# Resten af motoren læser samme validerede parameterobjekt.
#
# Parametergrupper:
# - InputOutputParameters: output names and QGIS project metadata / outputnavne og QGIS-projektmetadata
# - CartographyParameters: physical distance/width on print, plus centerline/offset smoothing / fysisk afstand/bredde på print, samt centerlinje-/offset-udjævning
# - CorridorParameters: corridor detection and stabilization / corridor-detektion og stabilisering
# - StyleParameters: fixed route palette / fast rutepalette
#
# Alpha2 ændrer ikke bevidst geometrilogikken fra alpha1/V7.4.4.
# ==================================================


@dataclass(frozen=True)
class InputOutputParameters:
    output_group_name: str = "Routes / Ruter"
    corridor_result_name: str = "CMRL Corridors"
    result_name: str = "CMRL Automatic Lanes"
    manual_result_name: str = "CMRL Route Layout"


@dataclass(frozen=True)
class CartographyParameters:
    # Fysisk lane-afstand på færdigt print.
    output_lane_spacing_mm: float = 1.00

    # Fysisk rutelinjebredde på færdigt print.
    lane_width_mm: float = 0.50

    # Produktionsskala for materialiseret manual-lag.
    manual_target_scale: float = 20000.0

    # Vindue (map units) for arc-længde-udjævning af en corridors
    # centerlinje, før noget lane overhovedet offsettes fra den. 0
    # deaktiverer det. Se moving_average_smooth_points().
    centerline_smoothing_window: float = 120.0

    # Vindue (map units) for at udtone offset-værdien der hvor en rute
    # skifter lane_index (fri strækning <-> corridor, eller mellem to
    # corridorer). 0 deaktiverer det (hårdt spring). Se
    # smooth_offset_transitions().
    offset_transition_taper_distance: float = 120.0


@dataclass(frozen=True)
class CorridorParameters:
    equivalence_distance: float = 20.0
    equivalence_min_coverage: float = 0.80

    # Klassifikationsopløsning langs ruterne.
    sample_distance: float = 20.0

    # Maksimal afstand for at anse to ruter som samme corridor.
    match_distance: float = 20.0

    # Retningsforskel i grader. Modsat digitaliseringsretning accepteres.
    angle_tolerance_degrees: float = 35.0

    # Route-set ændring skal være stabil over mindst denne længde.
    min_stable_length: float = 80.0

    # Maksimalt antal stabiliseringspasses.
    stability_passes: int = 6

    # Mindste outputcorridor.
    min_corridor_length: float = 10.0

    # Maksimal reel afstand mellem en rutes optagne start- og slutpunkt,
    # der stadig behandles som én sammenhængende lukning. Bruges både til
    # at samle en løkkes corridor-buer på tværs af start/slut-sømmen og
    # til at afgøre om ruten selv skal lukkes i materialize_route_layers.
    # Samme tolerance begge steder undgår at de to trin er uenige om
    # hvorvidt en rute er lukket. / Maximum real-world gap between a
    # route's recorded start and end point that is still treated as one
    # continuous closure. Used both to merge a loop's corridor arcs across
    # its start/end seam and to decide whether the route itself should be
    # closed in materialize_route_layers, so the two steps can't disagree
    # about whether a route is closed.
    loop_closure_tolerance: float = 5.0

    @property
    def angle_tolerance_radians(self):
        return math.radians(self.angle_tolerance_degrees)

    @property
    def index_search_margin(self):
        return self.match_distance + self.sample_distance


@dataclass(frozen=True)
class StyleParameters:
    route_colors: tuple = (
        "#00c8a0",
        "#e6007e",
        "#ff6b35",
        "#7b2cbf",
        "#38b000",
        "#ff9f1c",
        "#3a0ca3",
        "#0077b6",
        "#d00000",
        "#6c757d",
    )


@dataclass(frozen=True)
class EngineParameters:
    io: InputOutputParameters = field(default_factory=InputOutputParameters)
    cartography: CartographyParameters = field(default_factory=CartographyParameters)
    corridor: CorridorParameters = field(default_factory=CorridorParameters)
    style: StyleParameters = field(default_factory=StyleParameters)

    def validate(self):
        errors = []

        positive_values = (
            ("output_lane_spacing_mm", self.cartography.output_lane_spacing_mm),
            ("lane_width_mm", self.cartography.lane_width_mm),
            ("manual_target_scale", self.cartography.manual_target_scale),
            ("equivalence_distance", self.corridor.equivalence_distance),
            ("sample_distance", self.corridor.sample_distance),
            ("match_distance", self.corridor.match_distance),
            ("min_stable_length", self.corridor.min_stable_length),
            ("min_corridor_length", self.corridor.min_corridor_length),
            ("loop_closure_tolerance", self.corridor.loop_closure_tolerance),
        )

        for name, value in positive_values:
            if value <= 0:
                errors.append("{} skal være > 0".format(name))

        if self.cartography.centerline_smoothing_window < 0:
            errors.append("centerline_smoothing_window skal være >= 0")

        if self.cartography.offset_transition_taper_distance < 0:
            errors.append("offset_transition_taper_distance skal være >= 0")

        if not 0 < self.corridor.equivalence_min_coverage <= 1:
            errors.append("equivalence_min_coverage skal være > 0 og <= 1")

        if not 0 < self.corridor.angle_tolerance_degrees <= 180:
            errors.append("angle_tolerance_degrees skal være > 0 og <= 180")

        if self.corridor.stability_passes < 1:
            errors.append("stability_passes skal være >= 1")

        if not self.style.route_colors:
            errors.append("route_colors må ikke være tom")

        if errors:
            raise ValueError(
                "Ugyldige engine-parametre:\n- " + "\n- ".join(errors)
            )

        return self


# ==================================================
# STANDARDPARAMETRE
# ==================================================
# Standardkonfiguration. run_engine(parameters) kan modtage en anden valideret konfiguration.
# Processing Tool-GUI'en kan senere bygge et EngineParameters-objekt
# og sende det til samme motor.
DEFAULT_PARAMETERS = EngineParameters().validate()


# ==================================================
# ENGINE PARAMETER BINDING
# ==================================================
# Geometrikernen bruger fortsat de historiske konstantnavne internt.
# Alpha3 samler bindingen ét sted, så run_engine(parameters) er den
# offentlige API. En senere engine-refactor kan fjerne disse aliases
# uden at ændre Processing Tool-kontrakten.

def _bind_engine_parameters(parameters):
    """Bind et valideret EngineParameters-objekt til geometrikernens aliases."""
    parameters = parameters.validate()

    global GROUP_NAME, CORRIDOR_RESULT_NAME, RESULT_NAME, MANUAL_RESULT_NAME
    global OUTPUT_LANE_SPACING_MM
    global LANE_WIDTH_MM, MANUAL_TARGET_SCALE, CENTERLINE_SMOOTHING_WINDOW
    global OFFSET_TRANSITION_TAPER_DISTANCE
    global CORRIDOR_EQUIVALENCE_DISTANCE
    global CORRIDOR_EQUIVALENCE_MIN_COVERAGE, ROUTE_COLORS
    global SAMPLE_DISTANCE, MATCH_DISTANCE, ANGLE_TOLERANCE
    global MIN_STABLE_LENGTH, STABILITY_PASSES, MIN_CORRIDOR_LENGTH
    global INDEX_SEARCH_MARGIN
    global LOOP_CLOSURE_TOLERANCE
    global NODE_MERGE_TOLERANCE, MIN_EDGE_SPLIT_LENGTH

    GROUP_NAME = parameters.io.output_group_name
    CORRIDOR_RESULT_NAME = parameters.io.corridor_result_name
    RESULT_NAME = parameters.io.result_name
    MANUAL_RESULT_NAME = parameters.io.manual_result_name

    OUTPUT_LANE_SPACING_MM = parameters.cartography.output_lane_spacing_mm
    LANE_WIDTH_MM = parameters.cartography.lane_width_mm
    MANUAL_TARGET_SCALE = parameters.cartography.manual_target_scale
    CENTERLINE_SMOOTHING_WINDOW = parameters.cartography.centerline_smoothing_window
    OFFSET_TRANSITION_TAPER_DISTANCE = parameters.cartography.offset_transition_taper_distance

    CORRIDOR_EQUIVALENCE_DISTANCE = parameters.corridor.equivalence_distance
    CORRIDOR_EQUIVALENCE_MIN_COVERAGE = parameters.corridor.equivalence_min_coverage

    ROUTE_COLORS = list(parameters.style.route_colors)

    SAMPLE_DISTANCE = parameters.corridor.sample_distance
    MATCH_DISTANCE = parameters.corridor.match_distance
    ANGLE_TOLERANCE = parameters.corridor.angle_tolerance_radians
    MIN_STABLE_LENGTH = parameters.corridor.min_stable_length
    STABILITY_PASSES = parameters.corridor.stability_passes
    MIN_CORRIDOR_LENGTH = parameters.corridor.min_corridor_length
    INDEX_SEARCH_MARGIN = parameters.corridor.index_search_margin
    LOOP_CLOSURE_TOLERANCE = parameters.corridor.loop_closure_tolerance

    # New for the graph-based topology (route network) rewrite: no
    # dedicated user-facing parameters yet, deliberately aliased to
    # existing tolerances that already mean almost the same thing -
    # MATCH_DISTANCE for "same physical junction" node consolidation,
    # MIN_CORRIDOR_LENGTH for "don't create a sliver edge fragment" -
    # can become independent parameters later if real data ever shows
    # they need to diverge.
    NODE_MERGE_TOLERANCE = parameters.corridor.match_distance
    MIN_EDGE_SPLIT_LENGTH = parameters.corridor.min_corridor_length

    return parameters


_bind_engine_parameters(DEFAULT_PARAMETERS)


# HJÆLPEFUNKTIONER
# ==================================================

def transform_geometry(geometry, source_crs, project_crs, project):

    transform = QgsCoordinateTransform(
        source_crs,
        project_crs,
        project
    )

    geom = QgsGeometry(geometry)
    geom.transform(transform)

    return geom


def extract_lines(geometry):

    if geometry.isNull() or geometry.isEmpty():
        return []

    if geometry.isMultipart():
        parts = geometry.asMultiPolyline()
    else:
        parts = [geometry.asPolyline()]

    result = []

    for part in parts:

        points = [
            QgsPointXY(point)
            for point in part
        ]

        if len(points) >= 2:
            result.append(points)

    return result


def point_distance(point1, point2):

    return math.hypot(
        point2.x() - point1.x(),
        point2.y() - point1.y()
    )


def cumulative_distances(points):

    distances = [0.0]
    total = 0.0

    for index in range(1, len(points)):

        total += point_distance(
            points[index - 1],
            points[index]
        )

        distances.append(total)

    return distances


def interpolate_point(points, distances, position):

    if position <= 0.0:
        return QgsPointXY(points[0])

    if position >= distances[-1]:
        return QgsPointXY(points[-1])

    low = 0
    high = len(distances) - 1

    while low + 1 < high:

        middle = (low + high) // 2

        if distances[middle] <= position:
            low = middle
        else:
            high = middle

    segment_length = (
        distances[high] - distances[low]
    )

    if segment_length <= 0.0:
        return QgsPointXY(points[low])

    fraction = (
        position - distances[low]
    ) / segment_length

    return QgsPointXY(
        points[low].x()
        + (
            points[high].x()
            - points[low].x()
        ) * fraction,

        points[low].y()
        + (
            points[high].y()
            - points[low].y()
        ) * fraction
    )


def resample_line(points, spacing):

    distances = cumulative_distances(points)
    total_length = distances[-1]

    if total_length <= 0.0:
        return []

    positions = [0.0]

    position = spacing

    while position < total_length:
        positions.append(position)
        position += spacing

    if total_length - positions[-1] > 0.01:
        positions.append(total_length)

    sampled = [
        interpolate_point(
            points,
            distances,
            position
        )
        for position in positions
    ]

    return sampled


def segment_angle(point1, point2):

    return math.atan2(
        point2.y() - point1.y(),
        point2.x() - point1.x()
    )


def angle_difference(angle1, angle2):

    difference = abs(angle1 - angle2)

    while difference > math.pi:
        difference = abs(
            difference - 2.0 * math.pi
        )

    # Parallelitet, ikke digitaliseringsretning.
    return min(
        difference,
        abs(math.pi - difference)
    )


def run_length(run, segment_lengths):

    return sum(
        segment_lengths[index]
        for index in range(
            run["start"],
            run["end"] + 1
        )
    )


def build_runs(values):

    if not values:
        return []

    runs = []

    start = 0
    current = values[0]

    for index in range(1, len(values)):

        if values[index] == current:
            continue

        runs.append(
            {
                "start": start,
                "end": index - 1,
                "value": current
            }
        )

        start = index
        current = values[index]

    runs.append(
        {
            "start": start,
            "end": len(values) - 1,
            "value": current
        }
    )

    return runs


def set_distance(set1, set2):

    return len(
        set(set1).symmetric_difference(set(set2))
    )


def choose_replacement(
    runs,
    run_index,
    segment_lengths
):

    run = runs[run_index]

    previous_run = (
        runs[run_index - 1]
        if run_index > 0
        else None
    )

    next_run = (
        runs[run_index + 1]
        if run_index + 1 < len(runs)
        else None
    )

    # A-B-A er det klareste støjmønster.
    if (
        previous_run is not None
        and next_run is not None
        and previous_run["value"] == next_run["value"]
    ):
        return previous_run["value"]

    candidates = []

    if previous_run is not None:

        candidates.append(
            (
                set_distance(
                    run["value"],
                    previous_run["value"]
                ),
                -run_length(
                    previous_run,
                    segment_lengths
                ),
                previous_run["value"]
            )
        )

    if next_run is not None:

        candidates.append(
            (
                set_distance(
                    run["value"],
                    next_run["value"]
                ),
                -run_length(
                    next_run,
                    segment_lengths
                ),
                next_run["value"]
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2]
        )
    )

    return candidates[0][2]


def stabilize_route_sets(
    values,
    segment_lengths
):

    values = list(values)
    repair_count = 0

    for pass_no in range(STABILITY_PASSES):

        runs = build_runs(values)
        changed = False

        for run_index, run in enumerate(runs):

            length = run_length(
                run,
                segment_lengths
            )

            if length >= MIN_STABLE_LENGTH:
                continue

            replacement = choose_replacement(
                runs,
                run_index,
                segment_lengths
            )

            if (
                replacement is None
                or replacement == run["value"]
            ):
                continue

            for index in range(
                run["start"],
                run["end"] + 1
            ):
                values[index] = replacement

            repair_count += 1
            changed = True

        if not changed:
            break

    return values, repair_count


def offset_polyline_points(points, offset_distance):
    # Constant-offset convenience wrapper - see
    # offset_polyline_points_varying() for the actual implementation and
    # its rationale (avoiding GEOS' offsetCurve() self-intersection).

    return offset_polyline_points_varying(
        points,
        [offset_distance] * len(points)
    )


def offset_polyline_points_varying(points, offset_distances):
    # ==================================================
    # PER-VERTEX PERPENDICULAR OFFSET / PUNKTVIS VINKELRET OFFSET
    #
    # GEOS' offsetCurve() runs a full buffer operation and can fold into a
    # self-intersecting loop wherever the offset distance exceeds the
    # local turning radius - worst on the outer lanes with the largest
    # offset, at exactly the sharp bends a route network naturally has.
    # Lane position here is already fully determined by topology (which
    # corridor, which lane_index) before this is ever called; the offset
    # itself only needs to place each point sideways by a known distance,
    # not resolve arbitrary self-intersections. A direct, predictable
    # per-vertex offset - using the averaged normal of the two adjoining
    # segments at each interior vertex, the single segment's normal at
    # each end - can't produce that kind of loop. A genuinely sharp bend
    # can still pinch the offset line close to itself, but that's a mild,
    # local degradation instead of a spike.
    #
    # offset_distances is one value per point rather than a single
    # constant, since a route built as one continuous path (see
    # assemble_route_path()) can change lane_index mid-path wherever it
    # moves between corridors with a different number of routes -
    # smooth_offset_transitions() is what makes that change gradual
    # rather than an abrupt step.
    # ==================================================

    point_count = len(points)

    if point_count < 2:
        return list(points)

    segment_normals = []

    for index in range(point_count - 1):

        dx = points[index + 1].x() - points[index].x()
        dy = points[index + 1].y() - points[index].y()
        length = math.hypot(dx, dy)

        if length < 0.000001:
            segment_normals.append((0.0, 0.0))
        else:
            segment_normals.append((-dy / length, dx / length))

    offset_points = []

    for index in range(point_count):

        if index == 0:
            nx, ny = segment_normals[0]
        elif index == point_count - 1:
            nx, ny = segment_normals[-1]
        else:
            n1x, n1y = segment_normals[index - 1]
            n2x, n2y = segment_normals[index]
            sx, sy = n1x + n2x, n1y + n2y
            norm = math.hypot(sx, sy)

            if norm < 0.000001:
                # Near-180-degree reversal: an averaged miter normal would
                # blow up. Fall back to the outgoing segment's own normal
                # rather than extending a spike toward infinity.
                nx, ny = n2x, n2y
            else:
                nx, ny = sx / norm, sy / norm

        offset_distance = offset_distances[index]

        offset_points.append(
            QgsPointXY(
                points[index].x() + nx * offset_distance,
                points[index].y() + ny * offset_distance
            )
        )

    return offset_points


def moving_average_smooth_points(points, window_distance):
    # ==================================================
    # ARC-LÆNGDE-UDJÆVNING / ARC-LENGTH MOVING AVERAGE
    #
    # A corridor centerline is a literal, unmodified copy of one route's
    # own recorded GPS points - real-world/receiver jitter that's
    # invisible at zero offset becomes visibly amplified once any lane is
    # offset perpendicular to it. This is cartographic noise, not shape:
    # it needs removing at the source, once, before it ever becomes an
    # input to offsetting - not smoothed away downstream on every
    # resulting lane independently.
    #
    # A weighted moving average along arc length was chosen over Chaikin
    # corner-cutting: Chaikin converges to a curve that stays close to
    # the *original* noisy control points (it rounds corners, it doesn't
    # damp point-to-point positional noise - tested directly, repeated
    # iterations plateaued at under 30% noise reduction while exploding
    # point count). A true moving average damps noise amplitude
    # predictably with window size, the way any low-pass filter does.
    # Triangular weighting (full weight at the centre, fading to zero at
    # the window edge) avoids the abrupt in/out-of-window jump a plain
    # box average would have at every step.
    #
    # Implemented directly in Python rather than via QGIS's own smooth()
    # so its exact behaviour is fully known and testable - the same
    # reasoning that replaced GEOS' offsetCurve() with
    # offset_polyline_points() above, and the same reasoning that made
    # the earlier simplify()-based attempt this session hard to predict.
    #
    # Endpoints are kept exactly fixed (not smoothed): later steps need
    # to trust that a corridor's start/end point precisely matches where
    # route-to-corridor matching found it.
    # ==================================================

    point_count = len(points)

    if point_count < 3 or window_distance <= 0:
        return list(points)

    distances = cumulative_distances(points)
    half_window = window_distance / 2.0

    smoothed = [QgsPointXY(points[0])]

    low_index = 0
    high_index = 0

    for index in range(1, point_count - 1):

        center_distance = distances[index]

        while distances[low_index] < center_distance - half_window:
            low_index += 1

        if high_index < low_index:
            high_index = low_index

        while (
            high_index < point_count - 1
            and distances[high_index + 1] <= center_distance + half_window
        ):
            high_index += 1

        sum_x = 0.0
        sum_y = 0.0
        sum_weight = 0.0

        for sample_index in range(low_index, high_index + 1):

            offset = abs(distances[sample_index] - center_distance)
            weight = max(0.0, 1.0 - offset / half_window)

            sum_x += points[sample_index].x() * weight
            sum_y += points[sample_index].y() * weight
            sum_weight += weight

        if sum_weight > 0.0:
            smoothed.append(
                QgsPointXY(sum_x / sum_weight, sum_y / sum_weight)
            )
        else:
            smoothed.append(QgsPointXY(points[index]))

    smoothed.append(QgsPointXY(points[-1]))

    return smoothed


def smooth_offset_transitions(distances, offset_values, taper_distance):
    # ==================================================
    # UDTONING AF OFFSET-OVERGANGE / SMOOTH OFFSET TRANSITIONS
    #
    # assemble_route_path() gives each point a target offset with a hard
    # step wherever the route crosses from one corridor (or a free
    # stretch) into another with a different lane_index. Applying that
    # directly would kink the line exactly at each such crossing. This
    # runs the exact same arc-length weighted-average technique as
    # moving_average_smooth_points() - just over the 1-D offset value at
    # each point instead of its 2-D position - so a step becomes a
    # gradual ramp. Multiple transitions close together blend together
    # naturally through the same windowing, with no special-casing
    # needed for that case versus a single isolated transition.
    # ==================================================

    count = len(offset_values)

    if count < 3 or taper_distance <= 0:
        return list(offset_values)

    half_window = taper_distance / 2.0

    smoothed = []
    low_index = 0
    high_index = 0

    for index in range(count):

        center_distance = distances[index]

        while distances[low_index] < center_distance - half_window:
            low_index += 1

        if high_index < low_index:
            high_index = low_index

        while (
            high_index < count - 1
            and distances[high_index + 1] <= center_distance + half_window
        ):
            high_index += 1

        sum_value = 0.0
        sum_weight = 0.0

        for sample_index in range(low_index, high_index + 1):

            offset = abs(distances[sample_index] - center_distance)
            weight = max(0.0, 1.0 - offset / half_window)

            sum_value += offset_values[sample_index] * weight
            sum_weight += weight

        if sum_weight > 0.0:
            smoothed.append(sum_value / sum_weight)
        else:
            smoothed.append(offset_values[index])

    return smoothed




def setup_project():
    # ==================================================
    # PROJEKT
    # ==================================================

    project = QgsProject.instance()
    root = project.layerTreeRoot()

    group = (
        root.findGroup(GROUP_NAME)
        or root.findGroup(GROUP_NAME.lower())
    )

    if group is None:
        group = root.addGroup(GROUP_NAME)

    project_crs = project.crs()

    if project_crs.isGeographic():
        raise Exception(
            "Project CRS must be meter-based / Projektets CRS skal være meterbaseret"
        )

    debug("")
    debug("========================================")
    debug("Cartographic Multi-Route Layout v8.2")
    debug("VERSION", VERSION)
    debug("========================================")

    return project, root, group, project_crs


def discover_routes(layers):
    # ==================================================
    # FIND RUTER
    # ==================================================

    routes = []

    for index, layer in enumerate(layers, start=1):

        if layer is None:
            continue

        layer_name = layer.name().strip()
        route_no = None
        route_name = layer_name

        prefix_match = re.match(
            r"^\D*(\d+)(?:[_\s-]+(.+))?$",
            layer_name
        )

        if prefix_match:
            route_no = int(prefix_match.group(1))
            route_name = prefix_match.group(2) or layer_name
            route_name = route_name.strip()
        else:
            route_no = index

        routes.append(
            {
                "number": route_no,
                "name": route_name,
                "layer": layer
            }
        )

    routes.sort(key=lambda item: item["number"])

    if not routes:
        raise Exception("No routes found / Ingen ruter fundet")

    # route_no is used as the join key for corridors, lanes, and manual
    # materialization throughout the engine. Two layers resolving to the
    # same number would silently merge into one route downstream instead
    # of raising an error, so duplicates must be rejected here.
    numbers_seen = {}
    duplicate_messages = []

    for route in routes:
        existing_layer_name = numbers_seen.get(route["number"])

        if existing_layer_name is not None:
            duplicate_messages.append(
                "{} ({} / {})".format(
                    route["number"],
                    existing_layer_name,
                    route["layer"].name()
                )
            )
        else:
            numbers_seen[route["number"]] = route["layer"].name()

    if duplicate_messages:
        raise Exception(
            "Duplicate route numbers / Dobbelte rutenumre: "
            + ", ".join(duplicate_messages)
        )


    debug("")
    debug("FOUND ROUTES / FUNDNE RUTER")
    debug("----------------------------------------")

    for route in routes:
        debug(
            "Route", route["number"], "loaded / Rute", route["number"], "indlæst", route["name"]
        )


    # ==================================================

    return routes


def load_routes(routes, project_crs, project):
    # ==================================================
    # LÆS RUTER OG BYG ROUTE LINES
    # ==================================================

    debug("")
    debug("READING AND RESAMPLING ROUTES / LÆSER OG RESAMPLER RUTER")
    debug("----------------------------------------")

    route_lines = []

    for route in routes:

        layer = route["layer"]

        for feature in layer.getFeatures():

            if not feature.hasGeometry():
                continue

            geometry = transform_geometry(
                feature.geometry(),
                layer.crs(),
                project_crs,
                project,
            )

            for points in extract_lines(geometry):

                sampled = resample_line(
                    points,
                    SAMPLE_DISTANCE
                )

                if len(sampled) < 2:
                    continue

                route_lines.append(
                    {
                        "route_no": route["number"],
                        "route_name": route["name"],
                        "points": sampled,
                    }
                )

        debug(
            "Route", route["number"], "loaded / Rute", route["number"], "indlæst"
        )

    debug("")
    debug("Route lines / Rutelinjer:", len(route_lines))



    return route_lines


def build_route_graph():
    # ==================================================
    # RUTE-GRAF / ROUTE GRAPH
    #
    # The single source of truth for topology: nodes at junctions,
    # edges tagged with which routes travel them. Built once via
    # insert_route_into_graph() for every route, then consolidated via
    # resolve_node_clusters()/resolve_route_paths() below - after that,
    # a route's path through the graph is known by construction, never
    # re-derived by searching the finished graph at render time. This
    # replaces the old bottom-up design (each route independently
    # detecting proximity to others, candidate corridors materialized
    # after the fact, every route re-matching itself against them at
    # render time) that produced a recurring pattern of junction bugs,
    # since junctions were never explicit - only approximated by
    # distance/angle thresholds applied repeatedly at different stages.
    # ==================================================

    return {
        "nodes": {},
        "edges": {},
        "live_edge_ids": set(),
        "superseded_by": {},
        "next_node_id": 0,
        "next_edge_id": 0,
    }


def create_node(graph, point):

    node_id = graph["next_node_id"]
    graph["next_node_id"] += 1
    graph["nodes"][node_id] = QgsPointXY(point)

    return node_id


def create_edge(graph, points, routes, node_a, node_b, created_by_route):

    edge_id = graph["next_edge_id"]
    graph["next_edge_id"] += 1

    points = [QgsPointXY(point) for point in points]

    graph["edges"][edge_id] = {
        "points": points,
        "distances": cumulative_distances(points),
        "node_a": node_a,
        "node_b": node_b,
        "routes": set(routes),
        "created_by_route": created_by_route,
    }

    graph["live_edge_ids"].add(edge_id)

    return edge_id


def build_live_edge_grid(graph, cell_size):
    # ==================================================
    # GITTER OVER LEVENDE KANTER / GRID OVER LIVE EDGES
    #
    # Same technique as build_segment_grid() below, generalized to index
    # every currently-live edge's segments together in one grid (keyed
    # by (edge_id, local_segment_index) pairs) - a not-yet-inserted
    # route can match against ANY live edge, so unlike the old
    # per-route candidate pre-filtering by route membership, there is no
    # narrower candidate set to start from here: membership is exactly
    # what insertion is deciding.
    # ==================================================

    grid = {}

    for edge_id in graph["live_edge_ids"]:

        points = graph["edges"][edge_id]["points"]

        for index in range(len(points) - 1):

            a = points[index]
            b = points[index + 1]

            min_cell_x = int(math.floor(min(a.x(), b.x()) / cell_size))
            max_cell_x = int(math.floor(max(a.x(), b.x()) / cell_size))
            min_cell_y = int(math.floor(min(a.y(), b.y()) / cell_size))
            max_cell_y = int(math.floor(max(a.y(), b.y()) / cell_size))

            for cell_x in range(min_cell_x, max_cell_x + 1):
                for cell_y in range(min_cell_y, max_cell_y + 1):
                    grid.setdefault((cell_x, cell_y), []).append(
                        (edge_id, index)
                    )

    return grid


def match_route_segments_to_edges(points, graph, edge_grid):
    # ==================================================
    # MATCH RUTE-SEGMENTER TIL LEVENDE KANTER / MATCH ROUTE SEGMENTS TO LIVE EDGES
    #
    # Per-segment classification against the CURRENT graph, not other
    # routes directly - this is what the old bottom-up per-route
    # detection used to do, replaced here by matching against the
    # single growing network instead. Angle tolerance (not just
    # distance) matters: two routes merely crossing at a point, not
    # running alongside each other, must not be classified as sharing
    # an edge - the same reasoning the old per-route detection used.
    # is_beyond_polyline_end() guards against nearest_position_on_
    # segment()'s clamped projection silently extending an edge's
    # effective capture zone past its own real end (the same clamping
    # bug fixed earlier this session for per-point corridor matching -
    # it applies identically here).
    # ==================================================

    matched_edge_id = []

    for index in range(len(points) - 1):

        point1 = points[index]
        point2 = points[index + 1]
        angle = segment_angle(point1, point2)

        candidates = set()
        candidates.update(nearby_segment_indices(point1, edge_grid, MATCH_DISTANCE))
        candidates.update(nearby_segment_indices(point2, edge_grid, MATCH_DISTANCE))

        best_edge_id = None
        best_distance = None

        for edge_id, local_index in candidates:

            if edge_id not in graph["live_edge_ids"]:
                continue

            edge = graph["edges"][edge_id]
            edge_points = edge["points"]
            a = edge_points[local_index]
            b = edge_points[local_index + 1]

            if angle_difference(angle, segment_angle(a, b)) > ANGLE_TOLERANCE:
                continue

            distance_at_a = edge["distances"][local_index]

            position1, _, distance1 = nearest_position_on_segment(
                point1, a, b, distance_at_a
            )
            position2, _, distance2 = nearest_position_on_segment(
                point2, a, b, distance_at_a
            )

            distance = max(distance1, distance2)

            if distance > MATCH_DISTANCE:
                continue

            total_length = edge["distances"][-1]

            if (
                is_beyond_polyline_end(point1, edge_points, position1, total_length)
                or is_beyond_polyline_end(point2, edge_points, position2, total_length)
            ):
                continue

            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_edge_id = edge_id

        matched_edge_id.append(best_edge_id)

    return matched_edge_id

def smooth_edge_centerlines(graph):
    # ==================================================
    # UDJÆVN KANT-CENTERLINJER / SMOOTH EDGE CENTERLINES
    #
    # Every lane offset from an edge shares this one geometry, so
    # smoothing it once here - after the graph is fully built and
    # consolidated, right before rendering - replaces smoothing (or
    # patching around) GPS/receiver noise independently on every lane
    # downstream. Distances are recomputed since smoothing shifts point
    # positions; moving_average_smooth_points() leaves endpoints
    # unchanged, so a smoothed edge's ends still coincide exactly with
    # its node's stored coordinate.
    # ==================================================

    if CENTERLINE_SMOOTHING_WINDOW <= 0:
        return

    for edge_id in graph["live_edge_ids"]:

        edge = graph["edges"][edge_id]
        points = edge["points"]

        if len(points) < 3:
            continue

        smoothed_points = moving_average_smooth_points(
            points, CENTERLINE_SMOOTHING_WINDOW
        )

        edge["points"] = smoothed_points
        edge["distances"] = cumulative_distances(smoothed_points)

def nearest_position_on_segment(point, a, b, distance_at_a):
    # ==================================================
    # NÆRMESTE POSITION PÅ ET ENKELT SEGMENT / NEAREST POSITION ON ONE SEGMENT
    #
    # The per-segment projection math shared by nearest_position_on_polyline()
    # (exhaustive search over every segment) and the grid-accelerated lookup
    # in match_route_points_to_corridors() below (only the handful of
    # segments near the query point) - one implementation, two search
    # strategies over it.
    # ==================================================

    dx = b.x() - a.x()
    dy = b.y() - a.y()
    segment_length_squared = dx * dx + dy * dy

    if segment_length_squared < 0.000001:
        t = 0.0
    else:
        t = (
            (point.x() - a.x()) * dx
            + (point.y() - a.y()) * dy
        ) / segment_length_squared
        t = max(0.0, min(1.0, t))

    projected_x = a.x() + t * dx
    projected_y = a.y() + t * dy

    distance = math.hypot(
        point.x() - projected_x,
        point.y() - projected_y
    )

    segment_length = math.hypot(dx, dy)
    position = distance_at_a + t * segment_length

    return position, QgsPointXY(projected_x, projected_y), distance


def nearest_position_on_polyline(point, polyline_points, polyline_distances):
    # ==================================================
    # NÆRMESTE POSITION PÅ POLYLINJE / NEAREST POSITION ON POLYLINE
    #
    # For a query point, finds the closest point on a reference polyline
    # and returns it as an arc-length position along that polyline (plus
    # the projected XY point and the distance), rather than just a vertex
    # index - needed to locate exactly where a route enters/exits a
    # corridor even when that's partway along a segment, not at a vertex.
    #
    # Exhaustive over every segment - only used by match_run_to_corridor(),
    # which calls this at most twice per run (its start/end), not once per
    # route point, so the O(corridor segments) cost here is never the
    # bottleneck it would be if reused for per-point corridor membership
    # (see match_route_points_to_corridors() below for that case instead).
    # ==================================================

    best_distance = None
    best_position = None
    best_point = None

    for index in range(len(polyline_points) - 1):

        position, projected_point, distance = nearest_position_on_segment(
            point,
            polyline_points[index],
            polyline_points[index + 1],
            polyline_distances[index]
        )

        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_position = position
            best_point = projected_point

    return best_position, best_point, best_distance


def extract_polyline_subrange(points, distances, position_a, position_b):
    # ==================================================
    # UDTRÆK DELSTRÆKNING / EXTRACT POLYLINE SUB-RANGE
    #
    # Returns the portion of a polyline between two arc-length positions,
    # oriented so the result starts at position_a and ends at position_b
    # (reversed if position_a is further along than position_b) - a route
    # that only travels part of a shared corridor, and possibly in the
    # opposite direction to how the corridor itself is oriented, still
    # gets exactly its own stretch, correctly facing its own travel
    # direction.
    # ==================================================

    low = min(position_a, position_b)
    high = max(position_a, position_b)

    sub_points = [interpolate_point(points, distances, low)]

    for index, distance in enumerate(distances):
        if low < distance < high:
            sub_points.append(QgsPointXY(points[index]))

    sub_points.append(interpolate_point(points, distances, high))

    deduplicated = [sub_points[0]]

    for point in sub_points[1:]:
        if point_distance(deduplicated[-1], point) > 0.001:
            deduplicated.append(point)

    if position_a > position_b:
        deduplicated.reverse()

    return deduplicated


def build_segment_grid(points, cell_size):
    # ==================================================
    # SEGMENT-GITTER / SEGMENT GRID
    #
    # A plain dict-based spatial hash over a corridor's own segments,
    # keyed by (cell_x, cell_y) - built once per corridor so
    # match_route_points_to_corridors() below can find the handful of
    # segments near a query point directly, instead of the full linear
    # scan nearest_position_on_polyline() does over every segment.
    #
    # This is deliberately a hand-written grid, not QgsSpatialIndex: it
    # needs to behave the same, and be genuinely fast, in both this
    # engine's own pure-Python code AND in a plain test harness without
    # a real GEOS-backed QGIS underneath it - a real R-tree from QGIS
    # would be just as fast in production but its speed could not be
    # verified outside QGIS itself.
    #
    # Each segment is registered under every cell its own (unexpanded)
    # bounding box touches, not just the cell of one endpoint - so a
    # segment longer than one cell is still found from any of the cells
    # it passes through.
    # ==================================================

    grid = {}

    for index in range(len(points) - 1):

        a = points[index]
        b = points[index + 1]

        min_cell_x = int(math.floor(min(a.x(), b.x()) / cell_size))
        max_cell_x = int(math.floor(max(a.x(), b.x()) / cell_size))
        min_cell_y = int(math.floor(min(a.y(), b.y()) / cell_size))
        max_cell_y = int(math.floor(max(a.y(), b.y()) / cell_size))

        for cell_x in range(min_cell_x, max_cell_x + 1):
            for cell_y in range(min_cell_y, max_cell_y + 1):
                grid.setdefault((cell_x, cell_y), []).append(index)

    return grid


def nearby_segment_indices(point, grid, cell_size):
    # ==================================================
    # NÆRLIGGENDE SEGMENTER / NEARBY SEGMENTS
    #
    # The query radius (MATCH_DISTANCE) is <= cell_size by construction
    # (see build_corridor_route_index()), so the 3x3 block of cells
    # centered on the point's own cell always covers every cell a match
    # within that radius could fall in - a point sitting right at a
    # cell's edge can only reach one cell further in any direction.
    # ==================================================

    center_x = int(math.floor(point.x() / cell_size))
    center_y = int(math.floor(point.y() / cell_size))

    candidates = set()

    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            candidates.update(
                grid.get((center_x + dx, center_y + dy), ())
            )

    return candidates


def split_edge_at_positions(graph, edge_id, split_positions):
    # ==================================================
    # DEL KANT VED POSITIONER / SPLIT EDGE AT POSITIONS
    #
    # split_positions are arc-length positions strictly inside (0, total
    # length) - MIN_EDGE_SPLIT_LENGTH snapping is already applied by the
    # caller (resolve_on_edge_run), so every position here represents a
    # genuine new junction, never a near-zero sliver. Registers the
    # fragment chain in superseded_by so any route that already recorded
    # a step against edge_id can be resolved to the correct fragment(s)
    # in one generic final pass (resolve_route_paths) - never a special
    # case at split time.
    # ==================================================

    edge = graph["edges"][edge_id]
    points = edge["points"]
    distances = edge["distances"]
    total_length = distances[-1]
    routes = edge["routes"]
    created_by_route = edge["created_by_route"]
    node_a = edge["node_a"]
    node_b = edge["node_b"]

    boundaries = [0.0] + sorted(split_positions) + [total_length]

    fragment_ids = []
    fragment_ranges = []
    previous_node = node_a

    for index in range(len(boundaries) - 1):

        start_position = boundaries[index]
        end_position = boundaries[index + 1]

        if index == len(boundaries) - 2:
            end_node = node_b
        else:
            end_node = create_node(
                graph,
                interpolate_point(points, distances, end_position)
            )

        fragment_points = extract_polyline_subrange(
            points, distances, start_position, end_position
        )

        fragment_id = create_edge(
            graph,
            fragment_points,
            set(routes),
            previous_node,
            end_node,
            created_by_route
        )

        fragment_ids.append(fragment_id)
        fragment_ranges.append((start_position, end_position))
        previous_node = end_node

    graph["superseded_by"][edge_id] = fragment_ids
    graph["live_edge_ids"].discard(edge_id)

    return fragment_ids, fragment_ranges


def resolve_on_edge_run(graph, edge_id, run_start_point, run_end_point):
    # ==================================================
    # LØS RUN MOD EKSISTERENDE KANT / RESOLVE RUN AGAINST EXISTING EDGE
    #
    # Determines exactly which (possibly newly split) fragment of
    # edge_id this run corresponds to, and this run's direction of
    # travel along it - the same projection match_run_to_corridor() used
    # to do, but now DECIDING the graph's structure (whether a split is
    # needed) instead of just reading a reference to offset from.
    # Exhaustive nearest_position_on_polyline() is fine here - called at
    # most twice per run, not once per point, so its O(edge points) cost
    # is never the bottleneck the per-point case was.
    # ==================================================

    edge = graph["edges"][edge_id]
    points = edge["points"]
    distances = edge["distances"]
    total_length = distances[-1]

    position_start, _, _ = nearest_position_on_polyline(
        run_start_point, points, distances
    )
    position_end, _, _ = nearest_position_on_polyline(
        run_end_point, points, distances
    )

    direction = 1 if position_start <= position_end else -1
    low_position = min(position_start, position_end)
    high_position = max(position_start, position_end)

    split_positions = []

    if low_position > MIN_EDGE_SPLIT_LENGTH:
        split_positions.append(low_position)

    if high_position < total_length - MIN_EDGE_SPLIT_LENGTH:
        split_positions.append(high_position)

    if not split_positions:

        fragment_id = edge_id
        fragment_node_a = edge["node_a"]
        fragment_node_b = edge["node_b"]

    else:

        fragment_ids, fragment_ranges = split_edge_at_positions(
            graph, edge_id, split_positions
        )

        midpoint = (low_position + high_position) / 2.0
        fragment_id = fragment_ids[0]

        for candidate_id, (range_start, range_end) in zip(fragment_ids, fragment_ranges):
            if range_start <= midpoint <= range_end:
                fragment_id = candidate_id
                break

        fragment = graph["edges"][fragment_id]
        fragment_node_a = fragment["node_a"]
        fragment_node_b = fragment["node_b"]

    if direction == 1:
        entry_node = fragment_node_a
        exit_node = fragment_node_b
    else:
        entry_node = fragment_node_b
        exit_node = fragment_node_a

    return entry_node, exit_node, fragment_id, direction


def insert_route_into_graph(graph, route_line, edge_grid):
    # ==================================================
    # INDSÆT RUTE I GRAF / INSERT ROUTE INTO GRAPH
    #
    # Classifies this route's own segments against the graph as it
    # stands at the start of this call (edge_grid is a snapshot),
    # stabilizes that per-segment signal exactly like the old
    # classify_route_sets() did (a genuinely noisy GPS-trace-scale
    # signal - reusing stabilize_route_sets()'s repair pass here is the
    # same situation it was designed for, unlike the old per-point
    # corridor-matching case where reusing it was proven wrong), then
    # walks the resulting runs in order, creating/extending/splitting
    # edges and recording this route's path through the graph as it
    # goes. previous_exit_node is carried from one run to the next so
    # consecutive off-network edges this SAME route creates connect at
    # one shared node by construction - no tolerance check needed for
    # that specific case, since it's the same physical point by
    # construction. Any other node coincidence (this route's own
    # boundary vs. a DIFFERENT route's independently-created node at the
    # same physical junction) is deliberately NOT reconciled here - that
    # is resolve_node_clusters()'s job, once, by geometry, after every
    # route has been inserted.
    # ==================================================

    route_no = route_line["route_no"]
    points = route_line["points"]

    matched_edge_id = match_route_segments_to_edges(points, graph, edge_grid)

    segment_lengths = [
        point_distance(points[index], points[index + 1])
        for index in range(len(points) - 1)
    ]

    raw_values = [
        (matched_edge_id[index],) if matched_edge_id[index] is not None else ()
        for index in range(len(matched_edge_id))
    ]

    stable_values, _ = stabilize_route_sets(raw_values, segment_lengths)
    runs = build_runs(stable_values)

    path_steps = []
    previous_exit_node = None

    for run in runs:

        run_points = points[run["start"]: run["end"] + 2]

        if len(run_points) < 2:
            continue

        if not run["value"]:

            start_node = (
                previous_exit_node
                if previous_exit_node is not None
                else create_node(graph, run_points[0])
            )
            end_node = create_node(graph, run_points[-1])

            new_edge_id = create_edge(
                graph, run_points, {route_no}, start_node, end_node, route_no
            )

            path_steps.append((new_edge_id, 1))
            previous_exit_node = end_node

            continue

        edge_id = run["value"][0]

        if edge_id not in graph["live_edge_ids"]:
            # Already split by an EARLIER run within this SAME route's
            # own insertion - only possible if this route's own path
            # revisits the same original edge twice (self-overlap
            # territory, explicitly out of scope for v1). Falling back
            # to a fresh off-network edge avoids resolving against a
            # stale, already-superseded edge, which could otherwise
            # corrupt the graph with an orphaned double-split. This is a
            # documented limitation, not a silent bug: both visits still
            # render (at whatever lane each independently resolves to),
            # they just won't share fragment identity with each other.
            start_node = (
                previous_exit_node
                if previous_exit_node is not None
                else create_node(graph, run_points[0])
            )
            end_node = create_node(graph, run_points[-1])

            new_edge_id = create_edge(
                graph, run_points, {route_no}, start_node, end_node, route_no
            )

            path_steps.append((new_edge_id, 1))
            previous_exit_node = end_node

            continue

        entry_node, exit_node, fragment_id, direction = resolve_on_edge_run(
            graph, edge_id, run_points[0], run_points[-1]
        )

        graph["edges"][fragment_id]["routes"].add(route_no)
        path_steps.append((fragment_id, direction))
        previous_exit_node = exit_node

    return path_steps


def resolve_node_clusters(graph):
    # ==================================================
    # SAMMENFLET NODE-KLYNGER / CONSOLIDATE NODE CLUSTERS
    #
    # Nodes are created freely during insertion (a fresh node per
    # off-network edge endpoint, or per split point), so the SAME
    # physical junction can end up as several distinct node objects -
    # one route's own approach point may differ by a few meters from
    # another route's independently-projected split point. Consolidated
    # here, once, by geometry - the same union-find-by-distance
    # principle already trusted for corridor equivalence - rather than
    # trying to greedily reuse nodes during insertion, which can drift
    # (two nodes each within tolerance of a shared approach point, but
    # not of each other).
    # ==================================================

    node_ids = list(graph["nodes"].keys())
    parent = {node_id: node_id for node_id in node_ids}

    def find(node_id):
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(node_id_1, node_id_2):
        root1 = find(node_id_1)
        root2 = find(node_id_2)
        if root1 == root2:
            return
        if root1 < root2:
            parent[root2] = root1
        else:
            parent[root1] = root2

    cell_size = NODE_MERGE_TOLERANCE
    node_grid = {}

    for node_id in node_ids:
        point = graph["nodes"][node_id]
        cell = (
            int(math.floor(point.x() / cell_size)),
            int(math.floor(point.y() / cell_size))
        )
        node_grid.setdefault(cell, []).append(node_id)

    merge_count = 0

    for node_id in node_ids:

        point = graph["nodes"][node_id]
        cell_x = int(math.floor(point.x() / cell_size))
        cell_y = int(math.floor(point.y() / cell_size))

        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other_id in node_grid.get((cell_x + dx, cell_y + dy), ()):

                    if other_id <= node_id:
                        continue

                    other_point = graph["nodes"][other_id]

                    if point_distance(point, other_point) <= NODE_MERGE_TOLERANCE:
                        union(node_id, other_id)
                        merge_count += 1

    canonical_by_node = {node_id: find(node_id) for node_id in node_ids}

    for edge in graph["edges"].values():
        edge["node_a"] = canonical_by_node[edge["node_a"]]
        edge["node_b"] = canonical_by_node[edge["node_b"]]

    canonical_ids = set(canonical_by_node.values())
    graph["nodes"] = {
        node_id: graph["nodes"][node_id] for node_id in canonical_ids
    }

    return merge_count


def resolve_route_paths(graph, route_path_steps):
    # ==================================================
    # LØS RUTE-STIER / RESOLVE ROUTE PATHS
    #
    # Each route's steps were recorded live during its own insertion,
    # and may reference an edge_id that a LATER route's insertion went
    # on to split. superseded_by chains are followed here, once, for
    # every route, after all insertions are done - this is the only
    # place a route's final path is derived from anything other than
    # what its own insertion directly produced.
    #
    # Direction matters through the chain: superseded_by lists a split
    # edge's fragments in its own original node_a->node_b order, so a
    # step recorded with direction -1 must walk that list REVERSED, with
    # each fragment's own direction flipped too - getting this backwards
    # would silently reverse a fragment's point order without reversing
    # its direction flag, producing a plausible-looking kink instead of
    # a crash. Fragment ids are always new/increasing, so this
    # recursion is a DAG by construction - no cycle guard needed.
    #
    # A route revisiting the same edge in its own, separate, later run
    # needs no special case here: this function only ever reads edge_id
    # values and the superseded_by map, never which route or which
    # insertion pass created either.
    # ==================================================

    def resolve_step(edge_id, direction):

        if edge_id in graph["live_edge_ids"]:
            return [(edge_id, direction)], 0

        fragment_ids = graph["superseded_by"][edge_id]
        ordered_ids = fragment_ids if direction == 1 else list(reversed(fragment_ids))

        resolved = []
        max_depth = 0

        for fragment_id in ordered_ids:
            sub_steps, sub_depth = resolve_step(fragment_id, direction)
            resolved.extend(sub_steps)
            max_depth = max(max_depth, sub_depth + 1)

        return resolved, max_depth

    resolved_paths = {}
    max_chain_depth = 0

    for route_no, steps in route_path_steps.items():

        resolved_steps = []

        for edge_id, direction in steps:
            sub_steps, depth = resolve_step(edge_id, direction)
            resolved_steps.extend(sub_steps)
            max_chain_depth = max(max_chain_depth, depth)

        resolved_paths[route_no] = resolved_steps

    return resolved_paths, max_chain_depth


def build_route_network(route_lines):
    # ==================================================
    # BYG RUTE-NETVÆRK / BUILD ROUTE NETWORK
    #
    # Top-level orchestrator: inserts every route into one growing graph
    # in a fixed, deterministic order - ascending route_no, independent
    # of input layer order, so unchanged input always gives an identical
    # result (sequential incremental matching is not fully order-
    # independent in pathological cases, unlike the old whole-geometry
    # corridor-equivalence check, so this determinism is deliberate, not
    # incidental) - then runs the two consolidation passes and smooths
    # every final edge's centerline once. After this,
    # assemble_route_path_from_graph() only ever reads the graph - it
    # never searches it.
    # ==================================================

    debug("")
    debug("BUILDING ROUTE NETWORK / BYGGER RUTE-NETVÆRK")
    debug("----------------------------------------")

    graph = build_route_graph()
    route_path_steps = {}

    for route_line in sorted(route_lines, key=lambda line: line["route_no"]):

        edge_grid = build_live_edge_grid(graph, MATCH_DISTANCE)
        route_path_steps[route_line["route_no"]] = insert_route_into_graph(
            graph, route_line, edge_grid
        )

    node_cluster_merges = resolve_node_clusters(graph)
    resolved_paths, max_chain_depth = resolve_route_paths(graph, route_path_steps)
    smooth_edge_centerlines(graph)

    stats = {
        "node_count": len(graph["nodes"]),
        "edge_count": len(graph["live_edge_ids"]),
        "split_count": len(graph["superseded_by"]),
        "max_supersede_chain_depth": max_chain_depth,
        "node_cluster_merges": node_cluster_merges,
    }

    debug("Nodes / Noder:", stats["node_count"])
    debug("Live edges / Levende kanter:", stats["edge_count"])
    debug("Edge splits / Kant-opdelinger:", stats["split_count"])
    debug("Max supersede-chain depth / Maks supersede-kæde-dybde:", stats["max_supersede_chain_depth"])
    debug("Node cluster merges / Node-klynge-sammenlægninger:", stats["node_cluster_merges"])

    return graph, resolved_paths, stats

def point_within_bounding_box(point, bounding_box):

    return (
        bounding_box.xMinimum() <= point.x() <= bounding_box.xMaximum()
        and bounding_box.yMinimum() <= point.y() <= bounding_box.yMaximum()
    )


def is_beyond_polyline_end(point, polyline_points, position, total_length):
    # ==================================================
    # UD OVER POLYLINJENS ENDE / BEYOND THE POLYLINE'S END
    #
    # nearest_position_on_polyline() clamps its projection to each
    # segment, so a point sitting just past a corridor's real endpoint -
    # still travelling in the same direction, exactly the case right
    # where a route is genuinely leaving a shared stretch - reports a
    # small "distance to the line" via that endpoint, easily within
    # MATCH_DISTANCE. That is correct for match_run_to_corridor()'s use
    # (finding where an already-confirmed run enters/exits its corridor),
    # but wrong for deciding per-point membership here: it would extend
    # every corridor's effective capture zone by MATCH_DISTANCE past each
    # of its own ends, silently pulling a route's solo continuation back
    # onto a corridor it has already left. Checking the sign of the
    # projection onto the boundary segment's own direction (not clamped)
    # tells the two cases apart.
    # ==================================================

    epsilon = 0.000001

    if position <= epsilon:
        a, b = polyline_points[0], polyline_points[1]
        dx, dy = b.x() - a.x(), b.y() - a.y()
        dot = (point.x() - a.x()) * dx + (point.y() - a.y()) * dy
        return dot < 0.0

    if position >= total_length - epsilon:
        a, b = polyline_points[-2], polyline_points[-1]
        dx, dy = b.x() - a.x(), b.y() - a.y()
        dot = (point.x() - b.x()) * dx + (point.y() - b.y()) * dy
        return dot > 0.0

    return False


def assemble_route_path_from_graph(graph, route_no, resolved_path):
    # ==================================================
    # SAMMENSÆT RUTE-STRÆKNING FRA GRAF / ASSEMBLE ROUTE PATH FROM GRAPH
    #
    # The route's path through the network is already known (resolved
    # once, in resolve_route_paths()) - this just stitches each
    # fragment's (possibly reversed) points and computes its lane offset
    # from route membership alone, never from travel direction:
    # offset_polyline_points_varying()'s normal-from-tangent already
    # cancels a reversed travel direction on its own (confirmed by
    # test_opposite_direction.py) - adding a direction-based sign
    # correction here would double-flip it.
    #
    # Unlike the old assemble_route_path(), there is no "corridor not
    # found, fall back to raw points" branch: every stretch of every
    # route is represented by exactly one graph edge by construction, so
    # there is nothing left to fall back to.
    # ==================================================

    assembled_points = []
    assembled_offsets = []

    for edge_id, direction in resolved_path:

        edge = graph["edges"][edge_id]
        edge_points = edge["points"]

        piece_points = (
            [QgsPointXY(point) for point in edge_points]
            if direction == 1
            else [QgsPointXY(point) for point in reversed(edge_points)]
        )

        if len(piece_points) < 2:
            continue

        route_numbers = sorted(edge["routes"])
        lane_center = (len(route_numbers) - 1) / 2.0
        lane_index = route_numbers.index(route_no) - lane_center

        physical_offset = (
            lane_index
            * MANUAL_TARGET_SCALE
            * OUTPUT_LANE_SPACING_MM
            / 1000.0
        )

        piece_offsets = [physical_offset] * len(piece_points)

        if (
            assembled_points
            and point_distance(assembled_points[-1], piece_points[0]) < 0.001
        ):
            # Coincident boundary point: drop the outgoing piece's copy
            # rather than the incoming one, so the new piece's lane
            # assignment governs the shared vertex, not the old one's.
            assembled_points.pop()
            assembled_offsets.pop()

        assembled_points.extend(piece_points)
        assembled_offsets.extend(piece_offsets)

    return assembled_points, assembled_offsets

def create_output_layers(project, project_crs):
    # ==================================================
    # FJERN GAMLE RESULTATER
    # ==================================================

    for result_name in (
        CORRIDOR_RESULT_NAME,
        RESULT_NAME,
        MANUAL_RESULT_NAME
    ):
        for layer in project.mapLayersByName(
            result_name
        ):
            project.removeMapLayer(layer.id())


    # ==================================================
    # CORRIDOR-LAG (diagnostisk - viser den udjævnede topologi)
    # ==================================================

    corridor_result = QgsVectorLayer(
        "LineString?crs=" + project_crs.authid(),
        CORRIDOR_RESULT_NAME,
        "memory"
    )

    corridor_provider = corridor_result.dataProvider()

    corridor_provider.addAttributes(
        [
            QgsField("corridor_id", QVariant.Int),
            QgsField("routes", QVariant.String),
            QgsField("route_count", QVariant.Int),
            QgsField("source_route", QVariant.Int),
            QgsField("length_m", QVariant.Double),
            QgsField("corrected_routes", QVariant.String),
            QgsField("canonical_id", QVariant.Int),
            QgsField("created_by_route", QVariant.Int),
        ]
    )

    corridor_result.updateFields()


    # ==================================================
    # RUTE-LAG (Automatic Lanes og Route Layout deler nu én
    # konstruktion - materialize_route_layers() - og dermed samme skema.
    # ==================================================

    route_fields = [
        QgsField("route_no", QVariant.Int),
        QgsField("name", QVariant.String),
        QgsField("part_count", QVariant.Int),
        QgsField("target_scale", QVariant.Double),
        QgsField("length_m", QVariant.Double),
    ]

    result = QgsVectorLayer(
        "MultiLineString?crs=" + project_crs.authid(),
        RESULT_NAME,
        "memory"
    )

    provider = result.dataProvider()
    provider.addAttributes(list(route_fields))
    result.updateFields()

    manual_result = QgsVectorLayer(
        "MultiLineString?crs=" + project_crs.authid(),
        MANUAL_RESULT_NAME,
        "memory"
    )

    manual_provider = manual_result.dataProvider()
    manual_provider.addAttributes(list(route_fields))
    manual_result.updateFields()



    return (
        corridor_result,
        corridor_provider,
        result,
        provider,
        manual_result,
        manual_provider,
    )


def write_corridor_diagnostics(graph, corridor_result, corridor_provider):
    # ==================================================
    # SKRIV CORRIDOR-DIAGNOSTIK / WRITE CORRIDOR DIAGNOSTICS
    #
    # The "CMRL Corridors" layer is a diagnostic view of the final route
    # network graph - one row per live edge, no lane offsets computed
    # here (lanes are built once per route directly from this same
    # graph, in materialize_route_layers()).
    #
    # One graph edge = one authoritative route-set now, so "routes" and
    # "corrected_routes" are identical by construction - kept as two
    # fields for compatibility with any saved QGIS styles/filters that
    # already reference the old (raw-candidate vs. corrected-union)
    # schema from before this rewrite. "created_by_route" is genuinely
    # new information this design provides for free: which route's own
    # insertion first produced this edge's geometry - useful when
    # eyeballing "why does this edge's shape look like route 7's noisy
    # trace".
    # ==================================================

    debug("")
    debug("WRITING CORRIDOR DIAGNOSTICS / SKRIVER CORRIDOR-DIAGNOSTIK")
    debug("----------------------------------------")

    corridor_features = []

    for corridor_id, edge_id in enumerate(sorted(graph["live_edge_ids"]), start=1):

        edge = graph["edges"][edge_id]
        route_numbers = sorted(edge["routes"])
        routes_text = ",".join(str(route_no) for route_no in route_numbers)

        corridor_feature = QgsFeature(corridor_result.fields())

        corridor_feature["corridor_id"] = corridor_id
        corridor_feature["routes"] = routes_text
        corridor_feature["route_count"] = len(route_numbers)
        corridor_feature["source_route"] = min(route_numbers)
        corridor_feature["length_m"] = edge["distances"][-1]
        corridor_feature["corrected_routes"] = routes_text
        corridor_feature["canonical_id"] = corridor_id
        corridor_feature["created_by_route"] = edge["created_by_route"]
        corridor_feature.setGeometry(QgsGeometry.fromPolylineXY(edge["points"]))

        corridor_features.append(corridor_feature)

    corridor_provider.addFeatures(corridor_features)
    corridor_result.updateExtents()

    debug("Corridor features / Corridor-features:", len(corridor_features))

    return corridor_features


def materialize_route_layers(
    graph,
    resolved_paths,
    routes,
    result,
    provider,
    manual_result,
    manual_provider
):
    # ==================================================
    # MATERIALISER RUTELAG FRA GRAF / MATERIALIZE ROUTE LAYERS FROM GRAPH
    #
    # One continuous path per route, built directly from its resolved
    # graph path (assemble_route_path_from_graph), tapered at lane_index
    # changes (smooth_offset_transitions) and offset once
    # (offset_polyline_points_varying) - no per-corridor offsetting, no
    # junction snapping, no point-by-point nearest-reference search
    # afterwards. Automatic Lanes and Route Layout are populated from the
    # same geometry: the former is the untouched automatic result, the
    # latter is the same starting point intended for hand-editing.
    # ==================================================

    debug("")
    debug("MATERIALIZING ROUTE LAYERS FROM GRAPH / MATERIALISERER RUTELAG FRA GRAF")
    debug("----------------------------------------")

    route_name_by_no = {
        route["number"]: route["name"]
        for route in routes
    }

    parts_by_route = {}
    closed_gap_repairs = 0

    for route_no, resolved_path in resolved_paths.items():

        points, offsets = assemble_route_path_from_graph(
            graph, route_no, resolved_path
        )

        if len(points) < 2:
            continue

        # Fragment boundaries within a route's own path can leave a
        # small kink where two edges of slightly different smoothing
        # history meet. One more arc-length smoothing pass over the
        # fully assembled path - the same primitive used on edge
        # centerlines - removes it without a seam-specific special case.
        points = moving_average_smooth_points(points, CENTERLINE_SMOOTHING_WINDOW)

        distances = cumulative_distances(points)

        tapered_offsets = smooth_offset_transitions(
            distances,
            offsets,
            OFFSET_TRANSITION_TAPER_DISTANCE
        )

        final_points = offset_polyline_points_varying(
            points,
            tapered_offsets
        )

        # A genuinely closed original route (loop) should still close
        # after offsetting, but each end may have picked up a different
        # lane_index taper right at the seam. Close a small residual gap
        # directly rather than leaving a visible break.
        original_gap = point_distance(points[0], points[-1])

        if original_gap < LOOP_CLOSURE_TOLERANCE:

            final_gap = point_distance(final_points[0], final_points[-1])

            if 0 < final_gap < LOOP_CLOSURE_TOLERANCE * 2:
                final_points[-1] = QgsPointXY(final_points[0])
                closed_gap_repairs += 1

        parts_by_route.setdefault(route_no, []).append(final_points)

    debug(
        "Closed-route gaps repaired / Lukkede rutehuller repareret:",
        closed_gap_repairs
    )

    route_features = []
    manual_features = []

    for route_no in sorted(parts_by_route):

        part_point_lists = [
            part_points
            for part_points in parts_by_route[route_no]
            if len(part_points) >= 2
        ]

        if not part_point_lists:
            continue

        part_geometries = [
            QgsGeometry.fromPolylineXY(part_points)
            for part_points in part_point_lists
        ]

        combined_geometry = QgsGeometry.collectGeometry(part_geometries)
        length_m = sum(geometry.length() for geometry in part_geometries)
        name = route_name_by_no.get(
            route_no,
            "Rute {}".format(route_no)
        )

        route_feature = QgsFeature(result.fields())
        route_feature["route_no"] = route_no
        route_feature["name"] = name
        route_feature["part_count"] = len(part_geometries)
        route_feature["target_scale"] = MANUAL_TARGET_SCALE
        route_feature["length_m"] = length_m
        route_feature.setGeometry(QgsGeometry(combined_geometry))
        route_features.append(route_feature)

        manual_feature = QgsFeature(manual_result.fields())
        manual_feature["route_no"] = route_no
        manual_feature["name"] = name
        manual_feature["part_count"] = len(part_geometries)
        manual_feature["target_scale"] = MANUAL_TARGET_SCALE
        manual_feature["length_m"] = length_m
        manual_feature.setGeometry(QgsGeometry(combined_geometry))
        manual_features.append(manual_feature)

        debug(
            "Route", route_no,
            "->", len(part_geometries), "part(s)",
            "| length_m", round(length_m, 1)
        )

    provider.addFeatures(route_features)
    result.updateExtents()

    manual_provider.addFeatures(manual_features)
    manual_result.updateExtents()

    debug("")
    debug("Route features / Rutefeatures:", len(route_features))



    return route_features, manual_features


def apply_renderers(lane_features, manual_features, manual_result, result):
    # ==================================================
    # KARTOGRAFISK RENDERER
    #
    # lane_features har allerede deres endelige geometri fra
    # materialize_route_layers(), så begge lag tegnes som en almindelig
    # per-rute farvet linjesymbol - ingen geometry-generator/
    # offset_curve() ved render-tid.
    # ==================================================


    def build_route_colored_renderer(features):

        categories = []

        route_numbers_in_result = sorted(
            {
                int(feature["route_no"])
                for feature in features
            }
        )

        for route_no in route_numbers_in_result:

            color = ROUTE_COLORS[
                (route_no - 1) % len(ROUTE_COLORS)
            ]

            symbol = QgsLineSymbol.createSimple(
                {
                    "color": color,
                    "width": str(LANE_WIDTH_MM),
                    "width_unit": "MM",
                    "capstyle": "round",
                    "joinstyle": "round"
                }
            )

            categories.append(
                QgsRendererCategory(
                    route_no,
                    symbol,
                    "Rute {}".format(route_no)
                )
            )

        return QgsCategorizedSymbolRenderer(
            "route_no",
            categories
        )


    result.setRenderer(
        build_route_colored_renderer(lane_features)
    )

    manual_result.setRenderer(
        build_route_colored_renderer(manual_features)
    )




def add_layers_to_project(project, root, group, corridor_result, result, manual_result):
    # ==================================================
    # TILFØJ LAG
    # ==================================================

    project.addMapLayer(
        corridor_result,
        False
    )

    project.addMapLayer(
        result,
        False
    )

    project.addMapLayer(
        manual_result,
        False
    )

    group.insertLayer(
        0,
        manual_result
    )

    group.insertLayer(
        1,
        result
    )

    group.insertLayer(
        2,
        corridor_result
    )

    corridor_node = root.findLayer(
        corridor_result.id()
    )

    if corridor_node is not None:
        corridor_node.setItemVisibilityChecked(
            False
        )

    automatic_node = root.findLayer(
        result.id()
    )

    if automatic_node is not None:
        automatic_node.setItemVisibilityChecked(
            False
        )

    manual_result.triggerRepaint()
    result.triggerRepaint()




def report_results(corridor_result, manual_result, network_stats, result, route_lines):
    # ==================================================
    # FINALIZE / AFSLUT
    # ==================================================

    debug("")
    debug("========================================")
    debug("DONE / FÆRDIG")
    debug("========================================")
    debug("Version:", VERSION)
    debug("Parameter model: EngineParameters / DEFAULT_PARAMETERS")
    debug("Corridor result / Corridor-resultat:", CORRIDOR_RESULT_NAME)
    debug("Automatic lane result / Automatisk lane-resultat:", RESULT_NAME)
    debug("Manual production layer / Manuelt produktionslag:", MANUAL_RESULT_NAME)
    debug("Route lines / Rutelinjer:", len(route_lines))
    debug("Network nodes / Netværksnoder:", network_stats["node_count"])
    debug("Network edges / Netværkskanter:", network_stats["edge_count"])
    debug("Edge splits / Kant-opdelinger:", network_stats["split_count"])
    debug("Max supersede-chain depth / Maks supersede-kæde-dybde:", network_stats["max_supersede_chain_depth"])
    debug("Node cluster merges / Node-klynge-sammenlægninger:", network_stats["node_cluster_merges"])
    debug(
        "Final corridors / Endelige korridorer:",
        corridor_result.featureCount()
    )
    debug(
        "Materialized automatic lanes / Materialiserede automatiske lanes:",
        result.featureCount()
    )
    debug(
        "Manual route features / Manuelle rutefeatures:",
        manual_result.featureCount()
    )
    debug("Lane spacing mm / Laneafstand mm:", OUTPUT_LANE_SPACING_MM)
    debug("Line width mm / Linjebredde mm:", LANE_WIDTH_MM)
    debug("Manual target scale / Manuel målestok:", MANUAL_TARGET_SCALE)
    debug("Centerline smoothing window / Centerlinje-udjævningsvindue:", CENTERLINE_SMOOTHING_WINDOW)
    debug("Offset transition taper distance / Offset-overgangs udtoningsafstand:", OFFSET_TRANSITION_TAPER_DISTANCE)
    debug("")
    debug(
        "CMRL Automatic Lanes and CMRL Route Layout are both built directly "
        "from the route network graph - one continuous, offset-once feature "
        "per route, no per-corridor stitching, no per-point runtime search. "
        "/ CMRL Automatic Lanes og CMRL Route Layout er begge bygget direkte "
        "fra rute-netværksgrafen - én sammenhængende, én-gangs-offsettet "
        "feature pr. rute, ingen sammensyning mellem corridorer, ingen "
        "runtime-søgning pr. punkt."
    )
    debug(
        "CMRL Route Layout starts identical to Automatic Lanes and is "
        "intended for manual cartographic editing with the Vertex Tool. / "
        "CMRL Route Layout starter identisk med Automatic Lanes og er "
        "beregnet til manuel kartografisk efterredigering med Vertex Tool."
    )
    debug(
        "The automatic lane layer and corridor layer are added hidden as diagnostic reference layers. / Det automatiske lane-lag og corridor-laget er tilføjet skjult som diagnostiske reference-lag."
    )
    debug("")


@dataclass
class EngineRunResult:
    """Struktureret resultat fra run_engine()."""

    parameters: EngineParameters
    corridor_layer: object
    automatic_lane_layer: object
    manual_lane_layer: object
    route_count: int
    node_count: int
    edge_count: int
    split_count: int
    max_supersede_chain_depth: int
    node_cluster_merges: int


def run_engine(parameters=None, route_layers=None, feedback=None):
    """
    Offentlig engine-API.

    parameters:
        EngineParameters eller None. None bruger DEFAULT_PARAMETERS.
    route_layers:
        Liste af valgte QGIS-lag til ruteflow-beregning.
    feedback:
        Valgfrit QGIS processing feedback-objekt.

    Returnerer:
        EngineRunResult med outputlag og centrale kørselsstatistikker.

    Funktionen er den kontrakt, som et QGIS Processing Tool senere skal kalde.
    """
    global ENGINE_FEEDBACK
    previous_feedback = ENGINE_FEEDBACK
    ENGINE_FEEDBACK = feedback
    try:
        parameters = _bind_engine_parameters(
            DEFAULT_PARAMETERS if parameters is None else parameters
        )

        project, root, group, project_crs = setup_project()

        routes = discover_routes(route_layers or [])
        route_lines = load_routes(routes, project_crs, project)

        graph, resolved_paths, network_stats = build_route_network(route_lines)

        corridor_result, corridor_provider, result, provider, manual_result, manual_provider = create_output_layers(
            project, project_crs
        )
        write_corridor_diagnostics(
            graph,
            corridor_result,
            corridor_provider,
        )
        lane_features, manual_features = materialize_route_layers(
            graph,
            resolved_paths,
            routes,
            result,
            provider,
            manual_result,
            manual_provider,
        )
        apply_renderers(lane_features, manual_features, manual_result, result)
        add_layers_to_project(
            project,
            root,
            group,
            corridor_result,
            result,
            manual_result,
        )
        report_results(
            corridor_result,
            manual_result,
            network_stats,
            result,
            route_lines,
        )

        return EngineRunResult(
            parameters=parameters,
            corridor_layer=corridor_result,
            automatic_lane_layer=result,
            manual_lane_layer=manual_result,
            route_count=len(route_lines),
            node_count=network_stats["node_count"],
            edge_count=network_stats["edge_count"],
            split_count=network_stats["split_count"],
            max_supersede_chain_depth=network_stats["max_supersede_chain_depth"],
            node_cluster_merges=network_stats["node_cluster_merges"],
        )
    finally:
        ENGINE_FEEDBACK = previous_feedback


def main():
    """Thin launcher for manual testing from the QGIS Python editor. / Tynd launcher til manuel test fra QGIS Python-editoren."""
    return run_engine(DEFAULT_PARAMETERS)


class CartographicRouteLayoutAlgorithm(QgsProcessingAlgorithm):
    """
    Første Processing-wrapper omkring V8-engine API'et.

    Alpha4 eksponerer de vigtigste kartografiske og mapping-relaterede
    parametre. Motoren kaldes udelukkende gennem run_engine(parameters).
    """

    LAYER_LIST = "LAYER_LIST"
    LANE_SPACING_MM = "LANE_SPACING_MM"
    LANE_WIDTH_MM = "LANE_WIDTH_MM"
    TARGET_SCALE = "TARGET_SCALE"
    CENTERLINE_SMOOTHING_WINDOW = "CENTERLINE_SMOOTHING_WINDOW"
    OFFSET_TRANSITION_TAPER_DISTANCE = "OFFSET_TRANSITION_TAPER_DISTANCE"
    ADD_DIAGNOSTIC_LAYERS = "ADD_DIAGNOSTIC_LAYERS"

    OUT_MANUAL = "OUT_MANUAL"
    OUT_AUTOMATIC = "OUT_AUTOMATIC"
    OUT_CORRIDORS = "OUT_CORRIDORS"
    OUT_ROUTE_COUNT = "OUT_ROUTE_COUNT"

    def name(self):
        return "cartographic_route_layout"

    def displayName(self):
        return "Cartographic route layout / Kartografisk rute-layout"

    def group(self):
        return "Cartography"

    def groupId(self):
        return "cartography"

    def shortHelpString(self):
        return (
            "Calculates cartographically offset cycle routes from selected route layers. / Beregner kartografisk forskudte cykelruter fra valgte rutelag. "
            "The V8 engine builds corridors, lane order, preferred order, and a manual edit layer. / V8-engine bygger korridorer, lane-order, preferred-order og et manuelt efterredigeringslag."
        )

    def createInstance(self):
        return CartographicRouteLayoutAlgorithm()

    def initAlgorithm(self, config=None):
        p = DEFAULT_PARAMETERS

        layer_param = QgsProcessingParameterMultipleLayers(
            self.LAYER_LIST,
            "Route layers / Rutelag",
            layerType=MULTIPLE_LAYER_TYPE,
        )
        layer_param.setDescription(
            "Select one or more line route layers. / Vælg ét eller flere rutelag."
        )
        self.addParameter(layer_param)

        param = QgsProcessingParameterNumber(
            self.LANE_SPACING_MM,
            "Lane spacing (mm) / Lane afstand (mm)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.cartography.output_lane_spacing_mm,
            minValue=0.01,
        )
        param.setDescription(
            "Lane spacing on map in millimeters. / Lane-afstand på kortet i millimeter."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 2}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.LANE_WIDTH_MM,
            "Line width (mm) / Linjebredde (mm)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.cartography.lane_width_mm,
            minValue=0.01,
        )
        param.setDescription(
            "Width of the output lane lines in millimeters. / Bredde på output-linjerne i millimeter."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 2}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.TARGET_SCALE,
            "Target scale / Målestok",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.cartography.manual_target_scale,
            minValue=1.0,
        )
        param.setDescription(
            "Target map scale for manual route materialization. / Målestok for manuel rutematerialisering."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.CENTERLINE_SMOOTHING_WINDOW,
            "Centerline smoothing window (m) / Centerlinje-udjævningsvindue (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.cartography.centerline_smoothing_window,
            minValue=0.0,
        )
        param.setDescription(
            "Arc-length window for smoothing a corridor's centerline before any lane "
            "is offset from it. 0 disables it. / Arc-længde-vindue for udjævning af "
            "en corridors centerlinje før nogen lane offsettes fra den. 0 deaktiverer det."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.OFFSET_TRANSITION_TAPER_DISTANCE,
            "Offset transition taper (m) / Offset-overgangs udtoning (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.cartography.offset_transition_taper_distance,
            minValue=0.0,
        )
        param.setDescription(
            "Window for tapering a route's offset where its lane_index changes "
            "(entering/leaving a shared corridor, or between two corridors). 0 "
            "disables it (a hard step). / Vindue for udtoning af en rutes offset "
            "hvor dens lane_index ændres. 0 deaktiverer det (hårdt spring)."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        self.addParameter(QgsProcessingParameterBoolean(
            self.ADD_DIAGNOSTIC_LAYERS,
            "Add diagnostic layers / Tilføj diagnostiske lag",
            defaultValue=True,
        ))

        self.addOutput(QgsProcessingOutputVectorLayer(
            self.OUT_MANUAL, "Manual edit layer / Manuelt efterredigeringslag"
        ))
        self.addOutput(QgsProcessingOutputVectorLayer(
            self.OUT_AUTOMATIC, "Automatic lane layer / Automatisk lane-lag"
        ))
        self.addOutput(QgsProcessingOutputVectorLayer(
            self.OUT_CORRIDORS, "Corridor layer / Korridorlag"
        ))
        self.addOutput(QgsProcessingOutputNumber(
            self.OUT_ROUTE_COUNT, "Route count / Antal ruter"
        ))

    def processAlgorithm(self, parameters, context, feedback):
        if feedback.isCanceled():
            return {}

        defaults = DEFAULT_PARAMETERS

        def get_double(name, default_value):
            value = self.parameterAsDouble(
                parameters, name, context
            )
            return default_value if value is None else value

        layers = self.parameterAsLayerList(
            parameters, self.LAYER_LIST, context
        )

        engine_parameters = EngineParameters(
            io=InputOutputParameters(
                output_group_name=defaults.io.output_group_name,
                corridor_result_name=defaults.io.corridor_result_name,
                result_name=defaults.io.result_name,
                manual_result_name=defaults.io.manual_result_name,
            ),
            cartography=CartographyParameters(
                output_lane_spacing_mm=get_double(
                    self.LANE_SPACING_MM,
                    defaults.cartography.output_lane_spacing_mm,
                ),
                lane_width_mm=get_double(
                    self.LANE_WIDTH_MM,
                    defaults.cartography.lane_width_mm,
                ),
                manual_target_scale=get_double(
                    self.TARGET_SCALE,
                    defaults.cartography.manual_target_scale,
                ),
                centerline_smoothing_window=get_double(
                    self.CENTERLINE_SMOOTHING_WINDOW,
                    defaults.cartography.centerline_smoothing_window,
                ),
                offset_transition_taper_distance=get_double(
                    self.OFFSET_TRANSITION_TAPER_DISTANCE,
                    defaults.cartography.offset_transition_taper_distance,
                ),
            ),
            corridor=defaults.corridor,
            style=defaults.style,
        ).validate()

        feedback.pushInfo("Running V8 engine... / Kører V8 engine...")
        try:
            engine_result = run_engine(
                engine_parameters,
                route_layers=layers,
                feedback=feedback,
            )
        except Exception as exc:
            raise QgsProcessingException(str(exc)) from exc

        if feedback.isCanceled():
            return {}

        add_diagnostics = self.parameterAsBool(
            parameters, self.ADD_DIAGNOSTIC_LAYERS, context
        )
        if not add_diagnostics:
            project = QgsProject.instance()
            for layer in (
                engine_result.automatic_lane_layer,
                engine_result.corridor_layer,
            ):
                if layer is not None and project.mapLayer(layer.id()) is not None:
                    project.removeMapLayer(layer.id())

        feedback.pushInfo(
            f"Done: {engine_result.route_count} routes. / Færdig: {engine_result.route_count} ruter."
        )

        return {
            self.OUT_MANUAL: engine_result.manual_lane_layer.id(),
            self.OUT_AUTOMATIC: engine_result.automatic_lane_layer.id(),
            self.OUT_CORRIDORS: engine_result.corridor_layer.id(),
            self.OUT_ROUTE_COUNT: engine_result.route_count,
        }


# Når filen køres direkte i QGIS' script-editor, bevares den gamle regressionstest.
# Når QGIS Processing loader filen som et script, skal algoritmeklassen registreres
# uden at motoren automatisk køres.
if __name__ == "__main__":
    ENGINE_RESULT = main()
