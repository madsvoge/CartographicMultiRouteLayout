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
#
# =============================================================================


VERSION = "8.5"

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


def route_set_key(route_set):

    return tuple(sorted(route_set))


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


def corridor_geometry(
    sampled_points,
    start_segment,
    end_segment
):

    points = sampled_points[
        start_segment:
        end_segment + 2
    ]

    if len(points) < 2:
        return QgsGeometry()

    return QgsGeometry.fromPolylineXY(points)


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


def build_segment_index(route_lines, project_crs):
    # ==================================================
    # SEGMENT SPATIAL INDEX - PERFORMANCE BRANCH
    # ==================================================
    debug("")
    debug("BUILDING SEGMENT INDEX / BYGGER SEGMENT-INDEX")
    debug("----------------------------------------")
    segment_index_layer = QgsVectorLayer("LineString?crs=" + project_crs.authid(), "_temporary_segment_index", "memory")
    segment_index_provider = segment_index_layer.dataProvider()
    segment_index_provider.addAttributes([QgsField("segment_id", QVariant.Int)])
    segment_index_layer.updateFields()
    segment_records = {}
    segment_features = []
    next_segment_id = 0
    for route_line in route_lines:
        points = route_line["points"]
        for segment_index in range(len(points) - 1):
            point1 = points[segment_index]
            point2 = points[segment_index + 1]
            geometry = QgsGeometry.fromPolylineXY([point1, point2])
            segment_records[next_segment_id] = {
                "route_no": route_line["route_no"],
                "geometry": geometry,
                "angle": segment_angle(point1, point2),
            }
            feature = QgsFeature(segment_index_layer.fields())
            feature["segment_id"] = next_segment_id
            feature.setGeometry(geometry)
            segment_features.append(feature)
            next_segment_id += 1
    segment_index_provider.addFeatures(segment_features)
    fid_to_segment_id = {feature.id(): int(feature["segment_id"]) for feature in segment_index_layer.getFeatures()}
    segment_spatial_index = QgsSpatialIndex(segment_index_layer.getFeatures())
    debug("Index segments / Indekssegmenter:", len(segment_records))


    return segment_records, fid_to_segment_id, segment_spatial_index


def classify_route_sets(route_lines, segment_records, fid_to_segment_id, segment_spatial_index):
    # ==================================================
    # KLASSIFICER ROUTE-SET - GROV KANDIDAT-OPDAGELSE
    #
    # This only has to find WHERE a route is plausibly travelling
    # alongside others, not get the exact membership right - two routes
    # sharing a real stretch can each independently detect a different
    # (possibly incomplete) subset of who else is there, and that is no
    # longer a correctness problem. materialize_corridors() below still
    # only builds one corridor candidate per detected run, from whichever
    # route happens to be its lowest-numbered member; when the same
    # physical stretch produces more than one such candidate (because two
    # routes disagreed about who else was on it), resolve_corridor_
    # equivalence() is what reconciles that - by geometry, at the
    # corridor level, taking the union of every equivalent candidate's
    # routes as the single corrected source of truth. That is a much
    # safer place to reconcile than here: a corridor is already a long,
    # continuous, stable run, so merging by whole-corridor coverage can't
    # bridge two routes that only ever pass near a shared third route at
    # different, unrelated physical locations - which per-segment
    # reconciliation (tried and reverted) could and did.
    # ==================================================
    debug("")
    debug("CLASSIFYING CORRIDORS / KLASSIFICERER KORRIDORER")
    debug("----------------------------------------")

    classified_lines = []
    raw_change_count = 0
    stable_change_count = 0
    stability_repair_count = 0

    for line_counter, route_line in enumerate(route_lines, start=1):

        points = route_line["points"]
        segment_lengths = []
        raw_route_sets = []

        for segment_index in range(len(points) - 1):

            point1 = points[segment_index]
            point2 = points[segment_index + 1]
            segment_lengths.append(point_distance(point1, point2))

            segment_geometry = QgsGeometry.fromPolylineXY([point1, point2])
            angle = segment_angle(point1, point2)
            search_rectangle = segment_geometry.boundingBox()
            search_rectangle.grow(INDEX_SEARCH_MARGIN)
            candidate_fids = segment_spatial_index.intersects(search_rectangle)

            matched_routes = {route_line["route_no"]}

            for candidate_fid in candidate_fids:

                candidate_segment_id = fid_to_segment_id.get(candidate_fid)

                if candidate_segment_id is None:
                    continue

                candidate = segment_records.get(candidate_segment_id)

                if candidate is None or candidate["route_no"] == route_line["route_no"]:
                    continue

                if segment_geometry.distance(candidate["geometry"]) > MATCH_DISTANCE:
                    continue

                if angle_difference(angle, candidate["angle"]) > ANGLE_TOLERANCE:
                    continue

                matched_routes.add(candidate["route_no"])

            raw_route_sets.append(route_set_key(matched_routes))

        raw_runs = build_runs(raw_route_sets)
        raw_change_count += max(0, len(raw_runs) - 1)

        stable_route_sets, repairs = stabilize_route_sets(raw_route_sets, segment_lengths)
        stability_repair_count += repairs

        stable_runs = build_runs(stable_route_sets)
        stable_change_count += max(0, len(stable_runs) - 1)

        classified_lines.append({
            "line": route_line,
            "segment_lengths": segment_lengths,
            "route_sets": stable_route_sets,
            "runs": stable_runs
        })

        debug(
            "Route-set / Rute-sæt:",
            route_line["route_no"],
            "segments", len(stable_route_sets),
            "stable_route_sets", stable_route_sets,
            "runs", [list(run) for run in stable_runs]
        )

        if line_counter % 10 == 0:
            debug(line_counter, "/", len(route_lines))
    debug("")
    debug("Raw route-set transitions / Rå route-set skift:", raw_change_count)
    debug("Stable route-set transitions / Stabile route-set skift:", stable_change_count)
    debug("Noise runs repaired / Støj-runs rettet:", stability_repair_count)



    return classified_lines, raw_change_count, stable_change_count, stability_repair_count


def materialize_corridors(classified_lines):
    # ==================================================
    # MATERIALIZE CORRIDORS / MATERIALISER KORRIDORER
    #
    # A corridor is emitted only from the lowest route in its route set.
    # This selects one original route as the centerline without union,
    # snapping, or geometric post-processing.
    #
    # En korridor udskrives kun fra den laveste rute i
    # dens route-set. Dermed vælges én original rute som
    # centerline uden union, snapping eller geometrisk
    # efterreparation.
    # ==================================================

    debug("")
    debug("MATERIALIZING CORRIDORS / MATERIALISERER KORRIDORER")
    debug("----------------------------------------")

    corridors = []
    skipped_non_owner = 0
    skipped_short = 0

    for classified in classified_lines:

        route_line = classified["line"]
        sampled_points = route_line["points"]

        for run in classified["runs"]:

            routes_key = run["value"]

            if not routes_key:
                continue

            owner_route = min(routes_key)

            if route_line["route_no"] != owner_route:
                skipped_non_owner += 1
                continue

            geometry = corridor_geometry(
                sampled_points,
                run["start"],
                run["end"]
            )

            if (
                geometry.isNull()
                or geometry.isEmpty()
                or geometry.length()
                < MIN_CORRIDOR_LENGTH
            ):
                skipped_short += 1
                continue

            corridors.append(
                {
                    "geometry": geometry,
                    "routes": routes_key,
                    "route_count": len(routes_key),
                    "source_route": route_line["route_no"],
                    "start_seg": run["start"],
                    "end_seg": run["end"]
                }
            )


    debug("Raw corridors / Rå korridorer:", len(corridors))
    debug("Skipped non-owner runs / Ikke-owner runs sprunget over:", skipped_non_owner)
    debug("Skipped short corridors / Korte korridorer sprunget over:", skipped_short)

    for corridor_id, corridor in enumerate(corridors):
        first_point = None
        last_point = None

        if corridor["geometry"].isMultipart():
            parts = corridor["geometry"].asMultiPolyline()
            if parts and parts[0]:
                first_point = QgsPointXY(parts[0][0])
                last_point = QgsPointXY(parts[0][-1])
        else:
            points = corridor["geometry"].asPolyline()
            if points:
                first_point = QgsPointXY(points[0])
                last_point = QgsPointXY(points[-1])

        closed_geom = (
            first_point is not None
            and last_point is not None
            and point_distance(first_point, last_point) < 0.001
        )

        debug(
            "CORRIDOR",
            corridor_id,
            "source_route",
            corridor["source_route"],
            "routes",
            sorted(corridor["routes"]),
            "route_count",
            corridor["route_count"],
            "length_m",
            round(corridor["geometry"].length(), 2),
            "closed_geom",
            closed_geom,
            "start_seg",
            corridor["start_seg"],
            "end_seg",
            corridor["end_seg"]
        )


    return corridors


def merge_corridors(corridors):
    # ==================================================
    # SAMMENKÆD DIREKTE NABO-RUNS
    #
    # Kun inden for samme source route og samme route-set.
    # Kandidatsøgningen er stadig begrænset til den gruppe -
    # aldrig et generelt nærmeste-nabo-gæt på tværs af corridors.
    # Men to runs af samme route+route-set må gerne have et reelt,
    # begrænset endepunktsgab mellem sig (typisk GPS-/digitaliserings-
    # støj ved en løkkes start/slut-søm), ikke kun floating point-støj.
    # LOOP_CLOSURE_TOLERANCE styrer hvor stort det gab må være, og er
    # den samme tolerance som afgør om en rute regnes som lukket i
    # materialize_route_layers, så de to trin ikke er uenige.
    # ==================================================

    debug("")
    debug("MERGING DIRECT CORRIDOR RUNS / SAMMENKÆDER DIREKTE KORRIDOR-RUNS")
    debug("----------------------------------------")

    merged_corridors = []
    joined_count = 0

    corridors_sorted = sorted(
        corridors,
        key=lambda item: (
            item["source_route"],
            item["routes"],
            item["geometry"].boundingBox().xMinimum(),
            item["geometry"].boundingBox().yMinimum()
        )
    )


    def geometry_points(geometry):

        if geometry.isMultipart():
            parts = geometry.asMultiPolyline()

            if not parts:
                return []

            points = max(
                parts,
                key=lambda part: len(part)
            )
        else:
            points = geometry.asPolyline()

        return [
            QgsPointXY(point)
            for point in points
        ]


    unused = set(range(len(corridors_sorted)))

    # Kandidater grupperes kun efter (source_route, routes_key). Selve
    # matchet afgøres af en reel afstandssammenligning inden for gruppen,
    # ikke af et hash-baseret koordinatgitter.
    corridor_points = {}
    candidates_by_group = {}

    for corridor_id, corridor in enumerate(corridors_sorted):

        points = geometry_points(corridor["geometry"])
        corridor_points[corridor_id] = points

        if len(points) < 2:
            continue

        group_key = (corridor["source_route"], corridor["routes"])
        candidates_by_group.setdefault(group_key, []).append(corridor_id)


    def find_join_candidate(group_key, point):

        best_id = None
        best_endpoint_index = None
        best_distance = None

        for candidate_id in candidates_by_group.get(group_key, []):

            if candidate_id not in unused:
                continue

            candidate_points = corridor_points[candidate_id]

            if len(candidate_points) < 2:
                continue

            for endpoint_index, candidate_point in (
                (0, candidate_points[0]),
                (1, candidate_points[-1])
            ):

                distance = point_distance(point, candidate_point)

                if distance > LOOP_CLOSURE_TOLERANCE:
                    continue

                if (
                    best_distance is None
                    or distance < best_distance
                    or (
                        distance == best_distance
                        and candidate_id < best_id
                    )
                ):
                    best_distance = distance
                    best_id = candidate_id
                    best_endpoint_index = endpoint_index

        return best_id, best_endpoint_index


    def midpoint(point1, point2):

        return QgsPointXY(
            (point1.x() + point2.x()) / 2.0,
            (point1.y() + point2.y()) / 2.0
        )


    while unused:

        current_id = min(unused)
        unused.remove(current_id)

        current = corridors_sorted[current_id]

        chain_points = list(corridor_points[current_id])

        source_route = current["source_route"]
        routes_key = current["routes"]
        group_key = (source_route, routes_key)

        extended = True

        while extended:

            extended = False

            # Forsøg først ved slutningen.
            candidate_id, endpoint_index = find_join_candidate(
                group_key,
                chain_points[-1]
            )

            if candidate_id is not None:

                candidate_points = list(corridor_points[candidate_id])

                if endpoint_index == 1:
                    candidate_points.reverse()

                # Luk et eventuelt reelt gab i sømmen med ét fælles punkt
                # i stedet for at lade et lille knæk stå.
                chain_points[-1] = midpoint(
                    chain_points[-1],
                    candidate_points[0]
                )

                chain_points.extend(candidate_points[1:])

                unused.remove(candidate_id)
                joined_count += 1
                extended = True
                continue

            # Forsøg derefter ved starten.
            candidate_id, endpoint_index = find_join_candidate(
                group_key,
                chain_points[0]
            )

            if candidate_id is not None:

                candidate_points = list(corridor_points[candidate_id])

                if endpoint_index == 0:
                    candidate_points.reverse()

                chain_points[0] = midpoint(
                    candidate_points[-1],
                    chain_points[0]
                )

                chain_points = (
                    candidate_points[:-1]
                    + chain_points
                )

                unused.remove(candidate_id)
                joined_count += 1
                extended = True

        merged_geometry = QgsGeometry.fromPolylineXY(
            chain_points
        )

        merged_corridors.append(
            {
                "geometry": merged_geometry,
                "routes": routes_key,
                "route_count": len(routes_key),
                "source_route": source_route
            }
        )


    debug("Merges / Sammenkædninger:", joined_count)
    debug("Final corridors / Endelige korridorer:", len(merged_corridors))



    return merged_corridors, joined_count


def resolve_corridor_equivalence(merged_corridors):
    # ==================================================
    # KORRIDOR-EQUIVALENS - TOPOLOGY FIRST
    #
    # This is the single source of truth for "which routes actually
    # share this physical stretch" - not classify_route_sets(). Two
    # routes' own independent proximity search can each detect a
    # different (possibly incomplete) subset of who else is on a shared
    # stretch, since each is only guessing from noisy GPS traces; that
    # disagreement showed up as materialize_corridors() emitting more
    # than one corridor candidate for what is really the same physical
    # road, one per route whose own detection made it the run's owner.
    #
    # Equivalence is judged on geometry alone here - NOT on whether the
    # two candidates' route-sets already agree, which would beg the
    # question. Two whole corridor candidates are the same physical
    # stretch if they run close together for (almost) their entire
    # length; when they are, the union of every equivalent candidate's
    # routes becomes the corrected, single route-set for that stretch -
    # every route that actually travels it, regardless of which of them
    # individually detected which others.
    #
    # Judging equivalence on whole, already-continuous corridor
    # candidates (not on individual raw segments) is what keeps this
    # safe from over-merging: a candidate only merges with another if it
    # tracks it closely along (nearly) its whole length, so a route that
    # only brushes past a shared corridor briefly near a junction, then
    # diverges, cannot drag unrelated routes on the other side of that
    # junction into the same group the way segment-level reconciliation
    # (tried and reverted) could.
    #
    # The result is used ONLY for physical lane reference/materialization.
    # The diagnostic corridor/lane layers keep every raw candidate as
    # detected, unmodified.
    # ==================================================

    debug("")
    debug("FINDING EQUIVALENT CORRIDORS / FINDER ÆKVIVALENTE KORRIDORER")
    debug("----------------------------------------")


    def geometry_sample_points(geometry, spacing):

        sampled = []

        for points in extract_lines(geometry):

            if len(points) < 2:
                continue

            sampled.extend(
                resample_line(points, spacing)
            )

        return sampled


    def corridor_overlap_coverage(geometry1, geometry2):

        if geometry1.length() <= geometry2.length():
            sample_geometry = geometry1
            target_geometry = geometry2
        else:
            sample_geometry = geometry2
            target_geometry = geometry1

        sample_points = geometry_sample_points(
            sample_geometry,
            SAMPLE_DISTANCE
        )

        if not sample_points:
            return 0.0

        close_count = 0

        for point in sample_points:

            point_geometry = QgsGeometry.fromPointXY(
                point
            )

            if (
                point_geometry.distance(target_geometry)
                <= CORRIDOR_EQUIVALENCE_DISTANCE
            ):
                close_count += 1

        return close_count / len(sample_points)


    equivalence_parent = list(
        range(len(merged_corridors))
    )


    def equivalence_find(index):

        while equivalence_parent[index] != index:

            equivalence_parent[index] = (
                equivalence_parent[
                    equivalence_parent[index]
                ]
            )

            index = equivalence_parent[index]

        return index


    def equivalence_union(index1, index2):

        root1 = equivalence_find(index1)
        root2 = equivalence_find(index2)

        if root1 == root2:
            return

        if root1 < root2:
            equivalence_parent[root2] = root1
        else:
            equivalence_parent[root1] = root2


    equivalence_pair_count = 0

    for index1 in range(len(merged_corridors)):

        corridor1 = merged_corridors[index1]

        for index2 in range(
            index1 + 1,
            len(merged_corridors)
        ):

            corridor2 = merged_corridors[index2]

            distance = corridor1["geometry"].distance(
                corridor2["geometry"]
            )

            # Cheap rejection first, before the more expensive coverage
            # sampling - and before logging, so the debug log only shows
            # pairs that were genuine candidates.
            if distance > CORRIDOR_EQUIVALENCE_DISTANCE:
                continue

            coverage = corridor_overlap_coverage(
                corridor1["geometry"],
                corridor2["geometry"]
            )

            debug(
                "Corridor overlap / Korridor overlap:",
                index1,
                index2,
                "routes1", corridor1["routes"],
                "routes2", corridor2["routes"],
                "distance", round(distance, 3),
                "coverage", round(coverage, 3)
            )

            if (
                coverage
                < CORRIDOR_EQUIVALENCE_MIN_COVERAGE
            ):
                continue

            equivalence_union(
                index1,
                index2
            )

            equivalence_pair_count += 1


    equivalence_groups = {}

    for corridor_index in range(
        len(merged_corridors)
    ):

        root_index = equivalence_find(
            corridor_index
        )

        equivalence_groups.setdefault(
            root_index,
            []
        ).append(
            corridor_index
        )


    canonical_corridor_by_index = {}
    corridor_routes_by_canonical_index = {}

    for group_indices in equivalence_groups.values():

        canonical_index = max(
            group_indices,
            key=lambda corridor_index: (
                merged_corridors[
                    corridor_index
                ]["geometry"].length(),
                -corridor_index
            )
        )

        for corridor_index in group_indices:

            canonical_corridor_by_index[
                corridor_index
            ] = canonical_index

        # The corrected route-set for this physical stretch: every route
        # that any equivalent candidate detected, not just whichever
        # candidate happened to become canonical - this is what actually
        # fixes routes disagreeing about who else shares a stretch,
        # rather than just picking one route's (possibly incomplete)
        # side of the disagreement.
        unioned_routes = set()

        for corridor_index in group_indices:
            unioned_routes.update(
                merged_corridors[corridor_index]["routes"]
            )

        corridor_routes_by_canonical_index[canonical_index] = route_set_key(
            unioned_routes
        )


    equivalent_groups = [
        group_indices
        for group_indices in equivalence_groups.values()
        if len(group_indices) > 1
    ]

    debug(
        "Equivalent corridor pairs / Ækvivalente corridor-par:",
        equivalence_pair_count
    )

    debug(
        "Equivalence groups with duplicates / Ækvivalensgrupper med dubletter:",
        len(equivalent_groups)
    )

    for group_indices in equivalent_groups:

        canonical_index = (
            canonical_corridor_by_index[
                group_indices[0]
            ]
        )

        debug(
            "corrected routes",
            ",".join(
                str(route_no)
                for route_no in corridor_routes_by_canonical_index[
                    canonical_index
                ]
            ),
            "→ corridors",
            ",".join(
                str(index + 1)
                for index in group_indices
            ),
            "| canonical:",
            canonical_index + 1
        )



    return canonical_corridor_by_index, corridor_routes_by_canonical_index


def smooth_corridor_centerlines(merged_corridors):
    # ==================================================
    # UDJÆVN CORRIDOR-CENTERLINJER / SMOOTH CORRIDOR CENTERLINES
    #
    # Every lane offset from a corridor shares this one geometry, so
    # smoothing it once here - removing sub-cartographic GPS/receiver
    # noise before any lane is ever derived from it - replaces smoothing
    # (or patching around) that noise independently on every lane
    # downstream.
    # ==================================================

    if CENTERLINE_SMOOTHING_WINDOW <= 0:
        return merged_corridors

    for corridor in merged_corridors:

        parts = extract_lines(corridor["geometry"])

        if not parts or len(parts[0]) < 3:
            continue

        smoothed_points = moving_average_smooth_points(
            parts[0],
            CENTERLINE_SMOOTHING_WINDOW
        )

        corridor["geometry"] = QgsGeometry.fromPolylineXY(
            smoothed_points
        )

    return merged_corridors


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


def build_corridor_route_index(merged_corridors, canonical_corridor_by_index, corridor_routes_by_canonical_index):
    # ==================================================
    # CORRIDOR-OPSLAG / CORRIDOR LOOKUP
    #
    # Precomputes, once, what match_run_to_corridor() needs to look
    # candidates up cheaply per route: which canonical corridors this
    # specific route is a member of, per the CORRECTED route-set from
    # resolve_corridor_equivalence() - not the possibly-incomplete set
    # this route's own classify_route_sets() run happened to detect - and
    # each corridor's own points/arc-length distances computed once
    # rather than on every query. Only canonical corridors are indexed,
    # so every route sharing a corridor is matched to the same single
    # geometry even if that physical stretch was independently detected
    # more than once.
    # ==================================================

    corridors_by_route_no = {}
    corridor_cache = {}
    corridor_routes = {}

    for corridor_index, corridor in enumerate(merged_corridors):

        canonical_index = canonical_corridor_by_index.get(
            corridor_index,
            corridor_index
        )

        if canonical_index != corridor_index:
            continue

        parts = extract_lines(corridor["geometry"])

        if not parts or len(parts[0]) < 2:
            continue

        points = [QgsPointXY(point) for point in parts[0]]
        distances = cumulative_distances(points)

        # Grown by MATCH_DISTANCE so a point that's genuinely within
        # tolerance of the corridor can never fall just outside its own
        # bounding box - a cheap pre-filter to skip corridors nowhere
        # near a route entirely, before even touching its segment grid.
        bounding_box = corridor["geometry"].boundingBox()
        bounding_box.grow(MATCH_DISTANCE)

        # cell_size == MATCH_DISTANCE: nearby_segment_indices()'s 3x3
        # neighbourhood search relies on the query radius never
        # exceeding one cell.
        grid_cell_size = MATCH_DISTANCE
        segment_grid = build_segment_grid(points, grid_cell_size)

        corridor_cache[corridor_index] = {
            "points": points,
            "distances": distances,
            "bounding_box": bounding_box,
            "grid": segment_grid,
            "grid_cell_size": grid_cell_size,
        }

        routes = corridor_routes_by_canonical_index.get(
            canonical_index,
            corridor["routes"]
        )

        corridor_routes[corridor_index] = routes

        for route_no in routes:
            corridors_by_route_no.setdefault(route_no, []).append(corridor_index)

    return {
        "corridors_by_route_no": corridors_by_route_no,
        "corridor_cache": corridor_cache,
        "corridor_routes": corridor_routes,
    }


def match_run_to_corridor(corridor_index_data, route_no, run_start, run_end):
    # ==================================================
    # MATCH RUTE-RUN TIL CORRIDOR / MATCH ROUTE RUN TO CORRIDOR
    #
    # Given a route's own run (its start/end points) and the route's own
    # number, finds the single canonical corridor this route belongs to
    # at this physical location - per the corrected topology, not this
    # route's own local guess about who else is on it. Candidates are
    # every canonical corridor this route is a member of anywhere in the
    # network; disambiguating between more than one (the same route can
    # of course share different corridors at different points along its
    # length) is by picking whichever is spatially closest to this run -
    # and the result is just the sub-range of that corridor's geometry
    # this run actually covers, oriented to match this run's own travel
    # direction.
    # ==================================================

    candidates = corridor_index_data["corridors_by_route_no"].get(
        route_no,
        []
    )

    if not candidates:
        return None

    corridor_cache = corridor_index_data["corridor_cache"]

    best_corridor_index = None
    best_score = None
    best_start_position = None
    best_end_position = None

    for corridor_index in candidates:

        cached = corridor_cache.get(corridor_index)

        if cached is None:
            continue

        start_position, start_point, start_distance = (
            nearest_position_on_polyline(
                run_start,
                cached["points"],
                cached["distances"]
            )
        )
        end_position, end_point, end_distance = (
            nearest_position_on_polyline(
                run_end,
                cached["points"],
                cached["distances"]
            )
        )

        if start_position is None or end_position is None:
            continue

        # A corridor this route is nowhere near must not get matched
        # just because the route is nominally listed as one of its
        # members somewhere else in the network - that would silently
        # offset this run from an unrelated physical location. MATCH_
        # DISTANCE is already the established "is this really the same
        # feature" bound used for cross-route matching in classify_
        # route_sets.
        if start_distance > MATCH_DISTANCE or end_distance > MATCH_DISTANCE:
            continue

        score = start_distance + end_distance

        if best_score is None or score < best_score:
            best_score = score
            best_corridor_index = corridor_index
            best_start_position = start_position
            best_end_position = end_position

    if best_corridor_index is None:
        return None

    cached = corridor_cache[best_corridor_index]

    sub_range_points = extract_polyline_subrange(
        cached["points"],
        cached["distances"],
        best_start_position,
        best_end_position
    )

    if len(sub_range_points) < 2:
        return None

    route_numbers = sorted(
        corridor_index_data["corridor_routes"][best_corridor_index]
    )

    return {
        "corridor_index": best_corridor_index,
        "points": sub_range_points,
        "route_numbers": route_numbers,
    }


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


def match_route_points_to_corridors(points, route_no, corridor_index_data):
    # ==================================================
    # MATCH RUTE-PUNKTER TIL CORRIDORER LANGS RUTEN
    #
    # For every point along this route's own path, finds which corridor
    # (if any) this route is a member of - per the corrected topology
    # from resolve_corridor_equivalence(), not this route's own
    # classify_route_sets() guess - AND is physically near right there.
    # This is what determines run boundaries for assemble_route_path()
    # below directly and continuously from the topology, instead of
    # trusting classify_route_sets()'s own per-route runs to even notice
    # a stretch is shared in the first place: a route's own detection can
    # fail to flag sharing at all (not just get the membership wrong), in
    # which case nothing ever consulted the corrected topology for it,
    # however correct that topology already was - the actual remaining
    # gap after the topology-first rewrite.
    #
    # Two filters keep this affordable at real route/corridor sizes:
    # a corridor's own bounding box (grown by MATCH_DISTANCE, precomputed
    # in build_corridor_route_index()) skips corridors nowhere near this
    # point at all, and its per-corridor segment grid (also precomputed)
    # then finds only the handful of segments actually near the point,
    # instead of nearest_position_on_polyline()'s full scan over every
    # segment. A long, winding corridor's overall bounding box alone is a
    # poor filter - it can cover a lot of empty area a looping trail never
    # actually runs through - so without the grid, points that pass the
    # box check still triggered a full-length scan; confirmed directly
    # against real GPX data (six real routes, corridors up to 2200+
    # points): assemble_route_path took 6-15 seconds PER ROUTE before this
    # grid existed, which is why real runs against real data-sized input
    # appeared to hang rather than just run a bit slow.
    # ==================================================

    candidates = corridor_index_data["corridors_by_route_no"].get(route_no, [])
    corridor_cache = corridor_index_data["corridor_cache"]

    matched_corridor = []

    for point in points:

        best_corridor_index = None
        best_distance = None

        for corridor_index in candidates:

            cached = corridor_cache.get(corridor_index)

            if cached is None:
                continue

            if not point_within_bounding_box(point, cached["bounding_box"]):
                continue

            best_segment_distance = None
            best_segment_position = None

            for segment_index in nearby_segment_indices(
                point, cached["grid"], cached["grid_cell_size"]
            ):

                position, _, distance = nearest_position_on_segment(
                    point,
                    cached["points"][segment_index],
                    cached["points"][segment_index + 1],
                    cached["distances"][segment_index]
                )

                if (
                    best_segment_distance is None
                    or distance < best_segment_distance
                ):
                    best_segment_distance = distance
                    best_segment_position = position

            if best_segment_distance is None or best_segment_distance > MATCH_DISTANCE:
                continue

            if is_beyond_polyline_end(
                point, cached["points"], best_segment_position, cached["distances"][-1]
            ):
                continue

            if best_distance is None or best_segment_distance < best_distance:
                best_distance = best_segment_distance
                best_corridor_index = corridor_index

        matched_corridor.append(best_corridor_index)

    return matched_corridor


def assemble_route_path(classified_line, corridor_index_data):
    # ==================================================
    # SAMMENSÆT RUTE-STRÆKNING FRA TOPOLOGI / ASSEMBLE ROUTE PATH FROM TOPOLOGY
    #
    # Run boundaries come from match_route_points_to_corridors() above -
    # which corridor (if any) this route is a member of AND physically
    # near, checked continuously along its own points - not from
    # classify_route_sets()'s own runs. build_runs() alone groups this
    # per-point corridor-identity sequence into runs (wrapped as
    # single-element tuples, or an empty tuple for "no corridor here").
    # stabilize_route_sets()'s repair pass is deliberately NOT used here:
    # it exists to smooth GPS-noise-scale flicker in classify_route_sets()'s
    # own per-route classification, where a short run really is ambiguous
    # noise. Here a short run right at a corridor's boundary is not noise -
    # it is this route's own path genuinely continuing past the corridor's
    # detected end - and running the repair pass on it merges that boundary
    # signal into the adjacent corridor-matched run, silently re-extending
    # the match past the corridor's real geometry (confirmed directly: this
    # was the exact cause of the assembled path getting truncated to the
    # corridor's own extent instead of the route's true endpoints).
    #
    # For each run, this either takes the route's own points (unshared
    # stretches, or a shared guess with no real corridor match -
    # lane_index 0, nothing to offset from) or the matching corridor's
    # own (smoothed, canonical) sub-range plus this route's lane_index
    # within it (shared stretches). The result is one continuous point
    # sequence with a parallel per-point target offset distance - built
    # once, directly from the topology, never searched for point-by-
    # point afterwards.
    #
    # lane_index is used as-is regardless of whether this run travels the
    # same or the opposite direction along a shared corridor as another
    # route sharing it (e.g. two named routes sharing an access road as
    # part of loops that run opposite ways) - no direction-based sign
    # correction is applied. Verified directly: offset_polyline_points'
    # normal is a 90-degree rotation of the local tangent, so a reversed
    # travel direction already negates the normal on its own: the two
    # effects cancel out and both routes land on the same physical side
    # without any extra correction. Adding one here would double-flip
    # and put them on mismatched sides instead - confirmed by test,
    # not just reasoned about, since intuition first suggested the
    # opposite here and was wrong.
    # ==================================================

    route_line = classified_line["line"]
    route_no = route_line["route_no"]
    points = route_line["points"]

    matched_corridor = match_route_points_to_corridors(
        points, route_no, corridor_index_data
    )

    # A segment counts as "on corridor C" only if BOTH its endpoints are
    # matched to C - using only the start point's match would shift the
    # detected corridor boundary by one whole segment past the corridor's
    # true end (confirmed directly: a route continuing straight past a
    # corridor's real end was getting its final approach segment - whose
    # start point is still the corridor's own last point - misclassified
    # as "on the corridor", stretching match_run_to_corridor's query past
    # the corridor's real geometry and truncating the assembled path to
    # the corridor's own extent instead of the route's true endpoints).
    raw_values = [
        (matched_corridor[index],)
        if (
            matched_corridor[index] is not None
            and matched_corridor[index] == matched_corridor[index + 1]
        )
        else ()
        for index in range(len(points) - 1)
    ]

    # No stabilize_route_sets() here, deliberately: its MIN_STABLE_LENGTH
    # repair pass exists to smooth GPS-noise-scale flicker in the OLD
    # per-route pairwise route-set classification, where a short run was
    # ambiguous noise to be reconciled with a neighbour. Here a short run
    # right at a corridor's boundary is not noise - it is the exact signal
    # that this route's own path continues past the corridor's real
    # extent. Running the repair pass on this data silently merged that
    # boundary run into the corridor-matched run next to it, undoing the
    # is_beyond_polyline_end() fix and truncating the assembled path back
    # to the corridor's own extent (confirmed directly: repair_count == 2
    # for this exact scenario). build_runs() alone just groups consecutive
    # equal values with no repair, which is what this per-point,
    # already-precise proximity signal needs.
    runs = build_runs(raw_values)

    assembled_points = []
    assembled_offsets = []

    for run in runs:

        run_points = points[run["start"]: run["end"] + 2]

        if len(run_points) < 2:
            continue

        piece_points = None
        lane_index = 0.0

        if run["value"]:

            match = match_run_to_corridor(
                corridor_index_data,
                route_no,
                run_points[0],
                run_points[-1]
            )

            if match is not None:
                piece_points = match["points"]
                route_numbers = match["route_numbers"]
                lane_center = (len(route_numbers) - 1) / 2.0
                lane_index = (
                    route_numbers.index(route_no) - lane_center
                )

        if piece_points is None:
            # Either a genuinely unshared stretch, or a shared route-set
            # whose corridor was never materialized (e.g. filtered out
            # for being too short) - either way, fall back to this
            # route's own points, unoffset.
            piece_points = [QgsPointXY(point) for point in run_points]
            lane_index = 0.0

        if len(piece_points) < 2:
            continue

        physical_offset = (
            lane_index
            * MANUAL_TARGET_SCALE
            * OUTPUT_LANE_SPACING_MM
            / 1000.0
        )

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
        assembled_offsets.extend(
            [physical_offset] * len(piece_points)
        )

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


def write_corridor_diagnostics(
    merged_corridors,
    corridor_result,
    corridor_provider,
    canonical_corridor_by_index,
    corridor_routes_by_canonical_index
):
    # ==================================================
    # SKRIV CORRIDOR-DIAGNOSTIK / WRITE CORRIDOR DIAGNOSTICS
    #
    # The "CMRL Corridors" layer is a diagnostic view of the topology
    # itself (smoothed centerlines, route membership) - no lane offsets
    # are computed here, since lanes are now built once per route
    # directly from this same topology (materialize_route_layers()).
    #
    # "routes" is this specific candidate's own, possibly-incomplete raw
    # detection (unchanged, so the raw signal stays inspectable);
    # "corrected_routes" is what actually gets used for matching and lane
    # fan-out - the union from resolve_corridor_equivalence() across
    # every candidate judged to be the same physical stretch. The two
    # can legitimately differ; that gap is exactly the bug this rewrite
    # fixed, so surfacing it directly here avoids re-deriving it by hand
    # from the debug log next time.
    # ==================================================

    debug("")
    debug("WRITING CORRIDOR DIAGNOSTICS / SKRIVER CORRIDOR-DIAGNOSTIK")
    debug("----------------------------------------")

    corridor_features = []

    for corridor_index, corridor in enumerate(merged_corridors):

        corridor_id = corridor_index + 1
        route_numbers = sorted(corridor["routes"])
        routes_text = ",".join(str(route_no) for route_no in route_numbers)
        geometry = corridor["geometry"]

        canonical_index = canonical_corridor_by_index.get(corridor_index, corridor_index)
        corrected_route_numbers = corridor_routes_by_canonical_index.get(
            canonical_index,
            corridor["routes"]
        )
        corrected_routes_text = ",".join(
            str(route_no) for route_no in sorted(corrected_route_numbers)
        )

        corridor_feature = QgsFeature(corridor_result.fields())

        corridor_feature["corridor_id"] = corridor_id
        corridor_feature["routes"] = routes_text
        corridor_feature["route_count"] = len(route_numbers)
        corridor_feature["source_route"] = corridor["source_route"]
        corridor_feature["length_m"] = geometry.length()
        corridor_feature["corrected_routes"] = corrected_routes_text
        corridor_feature["canonical_id"] = canonical_index + 1
        corridor_feature.setGeometry(geometry)

        corridor_features.append(corridor_feature)

    corridor_provider.addFeatures(corridor_features)
    corridor_result.updateExtents()

    debug("Corridor features / Corridor-features:", len(corridor_features))



    return corridor_features


def materialize_route_layers(
    classified_lines,
    corridor_index_data,
    routes,
    result,
    provider,
    manual_result,
    manual_provider
):
    # ==================================================
    # MATERIALISER RUTELAG FRA TOPOLOGI / MATERIALIZE ROUTE LAYERS FROM TOPOLOGY
    #
    # One continuous path per route, built directly from the topology
    # (assemble_route_path), tapered at lane_index changes
    # (smooth_offset_transitions) and offset once
    # (offset_polyline_points_varying) - no per-corridor offsetting, no
    # junction snapping, no point-by-point nearest-reference search
    # afterwards. Automatic Lanes and Route Layout are populated from the
    # same geometry: the former is the untouched automatic result, the
    # latter is the same starting point intended for hand-editing.
    # ==================================================

    debug("")
    debug("MATERIALIZING ROUTE LAYERS FROM TOPOLOGY / MATERIALISERER RUTELAG FRA TOPOLOGI")
    debug("----------------------------------------")

    route_name_by_no = {
        route["number"]: route["name"]
        for route in routes
    }

    parts_by_route = {}
    closed_gap_repairs = 0

    for classified_line in classified_lines:

        route_no = classified_line["line"]["route_no"]

        points, offsets = assemble_route_path(
            classified_line,
            corridor_index_data
        )

        if len(points) < 2:
            continue

        # Corridor-matched stretches already carry smoothed geometry
        # (smooth_corridor_centerlines), but solo/private stretches are
        # still each route's own raw recorded points, and the seam where
        # a route's classified route-set boundary falls slightly before
        # its physical divergence stabilizes can leave a small kink where
        # a corridor-matched piece meets a solo piece. One more arc-length
        # smoothing pass over the fully assembled path - the same
        # primitive used on corridor centerlines - removes both without
        # a seam-specific special case.
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




def report_results(corridor_result, corridors, joined_count, manual_result, raw_change_count, result, route_lines, stability_repair_count, stable_change_count):
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
    debug("Raw route-set transitions / Rå route-set skift:", raw_change_count)
    debug("Stable route-set transitions / Stabile route-set skift:", stable_change_count)
    debug("Noise runs repaired / Støj-runs rettet:", stability_repair_count)
    debug("Raw corridors / Rå korridorer:", len(corridors))
    debug("Merges / Sammenkædninger:", joined_count)
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
        "from the corridor topology - one continuous, offset-once feature "
        "per route, no per-corridor stitching. / CMRL Automatic Lanes og "
        "CMRL Route Layout er begge bygget direkte fra corridor-topologien "
        "- én sammenhængende, én-gangs-offsettet feature pr. rute, ingen "
        "sammensyning mellem corridorer."
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
    raw_route_set_changes: int
    stable_route_set_changes: int
    stability_repairs: int
    raw_corridor_count: int
    joined_corridor_count: int


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
        segment_records, fid_to_segment_id, segment_spatial_index = build_segment_index(
            route_lines, project_crs
        )
        classified_lines, raw_change_count, stable_change_count, stability_repair_count = classify_route_sets(
            route_lines,
            segment_records,
            fid_to_segment_id,
            segment_spatial_index,
        )
        corridors = materialize_corridors(classified_lines)
        merged_corridors, joined_count = merge_corridors(corridors)
        canonical_corridor_by_index, corridor_routes_by_canonical_index = resolve_corridor_equivalence(
            merged_corridors
        )
        smooth_corridor_centerlines(merged_corridors)

        corridor_index_data = build_corridor_route_index(
            merged_corridors,
            canonical_corridor_by_index,
            corridor_routes_by_canonical_index
        )

        corridor_result, corridor_provider, result, provider, manual_result, manual_provider = create_output_layers(
            project, project_crs
        )
        write_corridor_diagnostics(
            merged_corridors,
            corridor_result,
            corridor_provider,
            canonical_corridor_by_index,
            corridor_routes_by_canonical_index,
        )
        lane_features, manual_features = materialize_route_layers(
            classified_lines,
            corridor_index_data,
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
            corridors,
            joined_count,
            manual_result,
            raw_change_count,
            result,
            route_lines,
            stability_repair_count,
            stable_change_count,
        )

        return EngineRunResult(
            parameters=parameters,
            corridor_layer=corridor_result,
            automatic_lane_layer=result,
            manual_lane_layer=manual_result,
            route_count=len(route_lines),
            raw_route_set_changes=raw_change_count,
            stable_route_set_changes=stable_change_count,
            stability_repairs=stability_repair_count,
            raw_corridor_count=len(corridors),
            joined_corridor_count=joined_count,
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
