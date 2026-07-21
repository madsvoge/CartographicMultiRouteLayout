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
    Qgis,
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
# Version: 8.2
#
# Version history / Versionshistorik
# 7.x  Development
# 7.4  Stable cartographic algorithm
# 8.0  Processing Tool architecture
# 8.2  Cartographic Multi-Route Layout
#
# =============================================================================


VERSION = "8.2"

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
# - CartographyParameters: physical distance/width on print / fysisk afstand/bredde på print
# - MappingParameters: mapping from original route to lane reference / mapping fra originalrute til lane-reference
# - PreferredOrderParameters: stable lane order / stabil lane-rækkefølge
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

    # GEOS offsetCurve-parametre.
    offset_segments: int = 8
    offset_miter_limit: float = 2.0


@dataclass(frozen=True)
class MappingParameters:
    # Minimumssøgeafstand. Den effektive afstand er scale-aware og kan blive større.
    manual_lane_search_distance: float = 45.0

    # Straf for unaturlige hop mellem lane-referencer ved junctions.
    manual_continuity_weight: float = 2.0

    # Straf for ændring af lane-position.
    manual_lane_inertia_weight: float = 3.0

    # Fade ind/ud mellem fysisk corridor-lane og fri originalgeometri.
    manual_terminal_transition_distance: float = 200.0


@dataclass(frozen=True)
class PreferredOrderParameters:
    junction_distance: float = 100.0
    min_shared_routes: int = 2
    passes: int = 12


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
    # til at afgøre om ruten selv skal lukkes i manual-materialiseringen.
    # Samme tolerance begge steder undgår at de to trin er uenige om
    # hvorvidt en rute er lukket. / Maximum real-world gap between a
    # route's recorded start and end point that is still treated as one
    # continuous closure. Used both to merge a loop's corridor arcs across
    # its start/end seam and to decide whether the route itself should be
    # closed during manual materialization, so the two steps can't disagree
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
    mapping: MappingParameters = field(default_factory=MappingParameters)
    preferred_order: PreferredOrderParameters = field(default_factory=PreferredOrderParameters)
    corridor: CorridorParameters = field(default_factory=CorridorParameters)
    style: StyleParameters = field(default_factory=StyleParameters)

    def validate(self):
        errors = []

        positive_values = (
            ("output_lane_spacing_mm", self.cartography.output_lane_spacing_mm),
            ("lane_width_mm", self.cartography.lane_width_mm),
            ("manual_target_scale", self.cartography.manual_target_scale),
            ("manual_lane_search_distance", self.mapping.manual_lane_search_distance),
            ("manual_terminal_transition_distance", self.mapping.manual_terminal_transition_distance),
            ("junction_distance", self.preferred_order.junction_distance),
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

        if self.cartography.offset_segments < 1:
            errors.append("offset_segments skal være >= 1")

        if self.cartography.offset_miter_limit <= 0:
            errors.append("offset_miter_limit skal være > 0")

        if self.mapping.manual_continuity_weight < 0:
            errors.append("manual_continuity_weight skal være >= 0")

        if self.mapping.manual_lane_inertia_weight < 0:
            errors.append("manual_lane_inertia_weight skal være >= 0")

        if self.preferred_order.min_shared_routes < 2:
            errors.append("min_shared_routes skal være >= 2")

        if self.preferred_order.passes < 1:
            errors.append("preferred_order passes skal være >= 1")

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
    global LANE_WIDTH_MM, MANUAL_TARGET_SCALE, OFFSET_SEGMENTS, OFFSET_MITER_LIMIT
    global MANUAL_LANE_SEARCH_DISTANCE, MANUAL_CONTINUITY_WEIGHT
    global MANUAL_LANE_INERTIA_WEIGHT, MANUAL_TERMINAL_TRANSITION_DISTANCE
    global PREFERRED_ORDER_JUNCTION_DISTANCE, PREFERRED_ORDER_MIN_SHARED_ROUTES
    global PREFERRED_ORDER_PASSES, CORRIDOR_EQUIVALENCE_DISTANCE
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
    OFFSET_SEGMENTS = parameters.cartography.offset_segments
    OFFSET_MITER_LIMIT = parameters.cartography.offset_miter_limit

    MANUAL_LANE_SEARCH_DISTANCE = parameters.mapping.manual_lane_search_distance
    MANUAL_CONTINUITY_WEIGHT = parameters.mapping.manual_continuity_weight
    MANUAL_LANE_INERTIA_WEIGHT = parameters.mapping.manual_lane_inertia_weight
    MANUAL_TERMINAL_TRANSITION_DISTANCE = parameters.mapping.manual_terminal_transition_distance

    PREFERRED_ORDER_JUNCTION_DISTANCE = parameters.preferred_order.junction_distance
    PREFERRED_ORDER_MIN_SHARED_ROUTES = parameters.preferred_order.min_shared_routes
    PREFERRED_ORDER_PASSES = parameters.preferred_order.passes

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
    # KLASSIFICER ROUTE-SET - DIREKTE SEGMENTKANDIDATER
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
            segment_length = point_distance(point1, point2)
            segment_lengths.append(segment_length)
            segment_geometry = QgsGeometry.fromPolylineXY([point1, point2])
            angle = segment_angle(point1, point2)
            search_rectangle = segment_geometry.boundingBox()
            search_rectangle.grow(INDEX_SEARCH_MARGIN)
            candidate_fids = segment_spatial_index.intersects(search_rectangle)
            matched_routes = {route_line["route_no"]}
            best_by_route = {}
            for candidate_fid in candidate_fids:
                candidate_segment_id = fid_to_segment_id.get(candidate_fid)
                if candidate_segment_id is None:
                    continue
                candidate = segment_records.get(candidate_segment_id)
                if candidate is None:
                    continue
                candidate_route_no = candidate["route_no"]
                if candidate_route_no == route_line["route_no"]:
                    continue
                local_distance = segment_geometry.distance(candidate["geometry"])
                if local_distance > MATCH_DISTANCE:
                    continue
                difference = angle_difference(angle, candidate["angle"])
                if difference > ANGLE_TOLERANCE:
                    continue
                score = (local_distance, difference)
                previous_best = best_by_route.get(candidate_route_no)
                if previous_best is None or score < previous_best:
                    best_by_route[candidate_route_no] = score
            matched_routes.update(best_by_route.keys())
            raw_route_sets.append(route_set_key(matched_routes))
        raw_runs = build_runs(raw_route_sets)
        raw_change_count += max(0, len(raw_runs) - 1)
        stable_route_sets, repairs = stabilize_route_sets(raw_route_sets, segment_lengths)
        stability_repair_count += repairs
        stable_runs = build_runs(stable_route_sets)
        stable_change_count += max(0, len(stable_runs) - 1)
        classified_lines.append({"line": route_line, "segment_lengths": segment_lengths, "route_sets": stable_route_sets, "runs": stable_runs})

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
    # materialize_manual_routes, så de to trin ikke er uenige.
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
    # V7.4.1 KORRIDOR-EQUIVALENS
    #
    # Samme route-set er ikke i sig selv nok: det samme sæt ruter
    # kan mødes flere steder på kortet. Derfor kræves også høj lokal
    # geometrisk overlap-coverage.
    #
    # Resultatet bruges KUN ved fysisk lane-reference/materialisering.
    # De diagnostiske corridor- og lane-lag bevarer alle korridorer.
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

            if corridor1["routes"] != corridor2["routes"]:
                continue

            distance = corridor1["geometry"].distance(
                corridor2["geometry"]
            )

            coverage = corridor_overlap_coverage(
                corridor1["geometry"],
                corridor2["geometry"]
            )

            debug(
                "Corridor overlap / Korridor overlap:",
                index1,
                index2,
                "routes", corridor1["routes"],
                "distance", round(distance, 3),
                "coverage", round(coverage, 3)
            )

            if distance > CORRIDOR_EQUIVALENCE_DISTANCE:
                continue

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
            "routes",
            ",".join(
                str(route_no)
                for route_no in merged_corridors[
                    canonical_index
                ]["routes"]
            ),
            "→ corridors",
            ",".join(
                str(index + 1)
                for index in group_indices
            ),
            "| canonical:",
            canonical_index + 1
        )



    return canonical_corridor_by_index


def resolve_preferred_order(merged_corridors):
    # ==================================================
    # V7.4.3 PREFERRED PAIRWISE ORDER
    # ==================================================
    # lane_index er allerede deterministisk inden for en corridor, men
    # offsetCurve fortolker fortegnet relativt til corridorens retning.
    # Hvis to tilstødende corridorer har modsat centerline-retning, kan
    # samme lane-order derfor fysisk spejlvendes og ruterne krydse.
    #
    # Her behandles hver corridor som en binær orientering: normal/reversed.
    # For hvert junction med mindst to fælles ruter måles forbindelsesprisen
    # mellem de fælles lane-endepunkter. Den orienteringskombination som
    # bedst bevarer den etablerede relative orden får lavest pris.
    # Et lille globalt relaxationsloop vælger corridor-orienteringer, så
    # pairwise preferred order genbruges gennem hele corridor-nettet.


    def oriented_corridor_points(corridor_index, reversed_flag):
        points = extract_lines(
            merged_corridors[corridor_index]["geometry"]
        )[0]

        points = [QgsPointXY(point) for point in points]

        if reversed_flag:
            points.reverse()

        return points


    def lane_position_for_route(route_numbers, route_no):
        lane_center = (len(route_numbers) - 1) / 2.0
        return route_numbers.index(route_no) - lane_center


    def physical_lane_endpoint(
        corridor_index,
        reversed_flag,
        endpoint_index,
        route_no
    ):
        points = oriented_corridor_points(
            corridor_index,
            reversed_flag
        )

        if len(points) < 2:
            return points[0]

        if endpoint_index == 0:
            anchor = points[0]
            neighbour = points[1]
        else:
            anchor = points[-1]
            neighbour = points[-2]

        # Tangent skal altid pege i den orienterede centerlines retning.
        if endpoint_index == 0:
            dx = neighbour.x() - anchor.x()
            dy = neighbour.y() - anchor.y()
        else:
            dx = anchor.x() - neighbour.x()
            dy = anchor.y() - neighbour.y()

        tangent_length = math.hypot(dx, dy)

        if tangent_length < 0.000001:
            return QgsPointXY(anchor)

        nx = -dy / tangent_length
        ny = dx / tangent_length

        route_numbers = sorted(
            merged_corridors[corridor_index]["routes"]
        )

        lane_index = lane_position_for_route(
            route_numbers,
            route_no
        )

        physical_offset = (
            lane_index
            * MANUAL_TARGET_SCALE
            * OUTPUT_LANE_SPACING_MM
            / 1000.0
        )

        return QgsPointXY(
            anchor.x() + nx * physical_offset,
            anchor.y() + ny * physical_offset
        )


    def nearest_endpoint_pair(corridor_a, corridor_b):
        points_a = oriented_corridor_points(
            corridor_a,
            False
        )
        points_b = oriented_corridor_points(
            corridor_b,
            False
        )

        endpoint_pairs = [
            (0, 0, point_distance(points_a[0], points_b[0])),
            (0, 1, point_distance(points_a[0], points_b[-1])),
            (1, 0, point_distance(points_a[-1], points_b[0])),
            (1, 1, point_distance(points_a[-1], points_b[-1]))
        ]

        return min(
            endpoint_pairs,
            key=lambda item: item[2]
        )


    def preferred_order_connection_cost(
        corridor_a,
        reversed_a,
        corridor_b,
        reversed_b,
        endpoint_a_original,
        endpoint_b_original,
        shared_routes
    ):
        endpoint_a = (
            1 - endpoint_a_original
            if reversed_a
            else endpoint_a_original
        )
        endpoint_b = (
            1 - endpoint_b_original
            if reversed_b
            else endpoint_b_original
        )

        total_cost = 0.0

        for route_no in shared_routes:
            point_a = physical_lane_endpoint(
                corridor_a,
                reversed_a,
                endpoint_a,
                route_no
            )
            point_b = physical_lane_endpoint(
                corridor_b,
                reversed_b,
                endpoint_b,
                route_no
            )

            total_cost += point_distance(
                point_a,
                point_b
            )

        return total_cost


    preferred_order_edges = []

    for corridor_a in range(len(merged_corridors)):
        routes_a = set(
            merged_corridors[corridor_a]["routes"]
        )

        for corridor_b in range(
            corridor_a + 1,
            len(merged_corridors)
        ):
            shared_routes = sorted(
                routes_a.intersection(
                    merged_corridors[corridor_b]["routes"]
                )
            )

            if (
                len(shared_routes)
                < PREFERRED_ORDER_MIN_SHARED_ROUTES
            ):
                continue

            endpoint_a, endpoint_b, junction_distance = (
                nearest_endpoint_pair(
                    corridor_a,
                    corridor_b
                )
            )

            if (
                junction_distance
                > PREFERRED_ORDER_JUNCTION_DISTANCE
            ):
                continue

            preferred_order_edges.append(
                {
                    "a": corridor_a,
                    "b": corridor_b,
                    "endpoint_a": endpoint_a,
                    "endpoint_b": endpoint_b,
                    "shared_routes": shared_routes,
                    "junction_distance": junction_distance
                }
            )


    edges_by_corridor = {
        corridor_index: []
        for corridor_index in range(len(merged_corridors))
    }

    for edge in preferred_order_edges:
        edges_by_corridor[edge["a"]].append(edge)
        edges_by_corridor[edge["b"]].append(edge)


    corridor_reversed = {
        corridor_index: False
        for corridor_index in range(len(merged_corridors))
    }

    # Start i den længste corridor. Dens retning er kun reference; det er
    # den relative orientering mellem corridorerne som er kartografisk vigtig.
    if merged_corridors:
        preferred_order_anchor = max(
            range(len(merged_corridors)),
            key=lambda corridor_index: merged_corridors[
                corridor_index
            ]["geometry"].length()
        )
    else:
        preferred_order_anchor = None


    def corridor_orientation_cost(corridor_index, reversed_flag):
        total_cost = 0.0

        for edge in edges_by_corridor[corridor_index]:
            corridor_a = edge["a"]
            corridor_b = edge["b"]

            reversed_a = (
                reversed_flag
                if corridor_a == corridor_index
                else corridor_reversed[corridor_a]
            )
            reversed_b = (
                reversed_flag
                if corridor_b == corridor_index
                else corridor_reversed[corridor_b]
            )

            total_cost += preferred_order_connection_cost(
                corridor_a,
                reversed_a,
                corridor_b,
                reversed_b,
                edge["endpoint_a"],
                edge["endpoint_b"],
                edge["shared_routes"]
            )

        return total_cost


    preferred_order_changes = 0

    for preferred_order_pass in range(
        PREFERRED_ORDER_PASSES
    ):
        pass_changes = 0

        corridor_order = sorted(
            range(len(merged_corridors)),
            key=lambda corridor_index: (
                -len(edges_by_corridor[corridor_index]),
                -merged_corridors[corridor_index]["geometry"].length()
            )
        )

        for corridor_index in corridor_order:
            if corridor_index == preferred_order_anchor:
                continue

            if not edges_by_corridor[corridor_index]:
                continue

            normal_cost = corridor_orientation_cost(
                corridor_index,
                False
            )
            reversed_cost = corridor_orientation_cost(
                corridor_index,
                True
            )

            new_reversed = reversed_cost < normal_cost

            if (
                new_reversed
                != corridor_reversed[corridor_index]
            ):
                corridor_reversed[corridor_index] = new_reversed
                pass_changes += 1
                preferred_order_changes += 1

        if pass_changes == 0:
            break


    preferred_order_reversed_count = 0

    for corridor_index, reversed_flag in corridor_reversed.items():
        if not reversed_flag:
            continue

        points = oriented_corridor_points(
            corridor_index,
            True
        )

        if len(points) < 2:
            continue

        merged_corridors[corridor_index]["geometry"] = (
            QgsGeometry.fromPolylineXY(points)
        )

        preferred_order_reversed_count += 1


    debug(
        "Preferred-order junctions:",
        len(preferred_order_edges)
    )
    debug(
        "Preferred-order orientation changes:",
        preferred_order_changes
    )
    debug(
        "Preferred-order reversed corridors:",
        preferred_order_reversed_count
    )



    return preferred_order_edges, preferred_order_reversed_count, corridor_reversed


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
    # CORRIDOR-LAG
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
            QgsField("length_m", QVariant.Double)
        ]
    )

    corridor_result.updateFields()


    # ==================================================
    # LANE-LAG
    # ==================================================

    result = QgsVectorLayer(
        "LineString?crs=" + project_crs.authid(),
        RESULT_NAME,
        "memory"
    )

    provider = result.dataProvider()

    provider.addAttributes(
        [
            QgsField("corridor_id", QVariant.Int),
            QgsField("routes", QVariant.String),
            QgsField("route_count", QVariant.Int),
            QgsField("route_no", QVariant.Int),
            QgsField("lane_index", QVariant.Double),
            QgsField("offset_m", QVariant.Double),
            QgsField("source_route", QVariant.Int),
            QgsField("length_m", QVariant.Double)
        ]
    )

    result.updateFields()



    return corridor_result, corridor_provider, result, provider


def write_corridors_and_lanes(merged_corridors, corridor_result, corridor_provider, result, provider):
    # ==================================================
    # SKRIV CORRIDORS + LANES
    #
    # Hver lane får sin egen forudberegnede offset-geometri (samme
    # offsetCurve-teknik som manual-lagets fysiske lane-referencer),
    # fremfor at dele corridorens centerline og lade QGIS' renderer
    # beregne en uafhængig offset_curve() pr. corridor ved hvert tegn.
    # Uafhængigt beregnede offsets fra nabo-corridorer rammer generelt
    # ikke samme koordinat i en junction, hvilket giver synlige knæk/
    # krydsninger. snap_lane_junctions() retter det op bagefter ved at
    # sømme delte ruters lane-endepunkter sammen på tværs af corridorer.
    # ==================================================

    debug("")
    debug("WRITING RESULT / SKRIVER RESULTAT")
    debug("----------------------------------------")

    corridor_features = []
    lane_features = []
    lane_feature_corridor_indices = []

    for corridor_index, corridor in enumerate(
        merged_corridors
    ):

        corridor_id = corridor_index + 1

        route_numbers = sorted(
            corridor["routes"]
        )

        routes_text = ",".join(
            str(route_no)
            for route_no in route_numbers
        )

        geometry = corridor["geometry"]
        length_m = geometry.length()

        corridor_feature = QgsFeature(
            corridor_result.fields()
        )

        corridor_feature["corridor_id"] = corridor_id
        corridor_feature["routes"] = routes_text
        corridor_feature["route_count"] = len(
            route_numbers
        )
        corridor_feature["source_route"] = (
            corridor["source_route"]
        )
        corridor_feature["length_m"] = length_m
        corridor_feature.setGeometry(geometry)

        corridor_features.append(
            corridor_feature
        )

        lane_center = (
            len(route_numbers) - 1
        ) / 2.0

        for lane_position, route_no in enumerate(
            route_numbers
        ):

            lane_index = (
                lane_position - lane_center
            )

            physical_offset = (
                lane_index
                * MANUAL_TARGET_SCALE
                * OUTPUT_LANE_SPACING_MM
                / 1000.0
            )

            if abs(physical_offset) < 0.000001:
                lane_geometry = geometry
            else:
                lane_geometry = geometry.offsetCurve(
                    physical_offset,
                    OFFSET_SEGMENTS,
                    Qgis.JoinStyle.Round,
                    OFFSET_MITER_LIMIT
                )

            if lane_geometry.isNull() or lane_geometry.isEmpty():
                continue

            lane_parts = extract_lines(lane_geometry)

            if not lane_parts:
                continue

            # A self-intersecting offset (sharp bend) can split into
            # several parts - keep the longest as the lane's geometry.
            lane_points = max(
                lane_parts,
                key=lambda part: QgsGeometry.fromPolylineXY(part).length()
            )

            lane_feature = QgsFeature(
                result.fields()
            )

            lane_feature["corridor_id"] = corridor_id
            lane_feature["routes"] = routes_text
            lane_feature["route_count"] = len(
                route_numbers
            )
            lane_feature["route_no"] = route_no
            lane_feature["lane_index"] = lane_index
            lane_feature["offset_m"] = physical_offset
            lane_feature["source_route"] = (
                corridor["source_route"]
            )
            lane_feature["length_m"] = length_m

            lane_feature.setGeometry(
                QgsGeometry.fromPolylineXY(lane_points)
            )

            lane_features.append(
                lane_feature
            )

            lane_feature_corridor_indices.append(
                corridor_index
            )


    corridor_provider.addFeatures(
        corridor_features
    )

    corridor_result.updateExtents()



    return lane_features, lane_feature_corridor_indices


def snap_lane_junctions(
    lane_features,
    lane_feature_corridor_indices,
    preferred_order_edges,
    corridor_reversed
):
    # ==================================================
    # SØM LANE-JUNCTIONS / SNAP LANE JUNCTIONS
    #
    # Hver lane har nu sin egen uafhængigt beregnede offsetCurve.
    # Ved en preferred-order junction deler to corridorer en eller flere
    # ruter; disse ruters lane-endepunkter rammer generelt ikke præcis
    # samme koordinat, selvom corridorernes centerlines mødes. Her sømmes
    # de fælles ruters endepunkter sammen til ét fælles punkt pr. junction,
    # samme teknik som loop-sømme i merge_corridors.
    # ==================================================

    debug("")
    debug("SNAPPING LANE JUNCTIONS / SØMMER LANE-JUNCTIONS")
    debug("----------------------------------------")

    lane_feature_by_corridor_route = {}

    for lane_feature, corridor_index in zip(
        lane_features,
        lane_feature_corridor_indices
    ):
        key = (corridor_index, int(lane_feature["route_no"]))
        lane_feature_by_corridor_route[key] = lane_feature


    def lane_points(lane_feature):
        parts = extract_lines(lane_feature.geometry())

        if not parts:
            return None

        return [QgsPointXY(point) for point in parts[0]]


    def current_endpoint(endpoint_original, reversed_flag):
        return (
            1 - endpoint_original
            if reversed_flag
            else endpoint_original
        )


    def midpoint(point1, point2):
        return QgsPointXY(
            (point1.x() + point2.x()) / 2.0,
            (point1.y() + point2.y()) / 2.0
        )


    snapped_count = 0

    for edge in preferred_order_edges:

        corridor_a = edge["a"]
        corridor_b = edge["b"]

        endpoint_a = current_endpoint(
            edge["endpoint_a"],
            corridor_reversed[corridor_a]
        )
        endpoint_b = current_endpoint(
            edge["endpoint_b"],
            corridor_reversed[corridor_b]
        )

        for route_no in edge["shared_routes"]:

            feature_a = lane_feature_by_corridor_route.get(
                (corridor_a, route_no)
            )
            feature_b = lane_feature_by_corridor_route.get(
                (corridor_b, route_no)
            )

            if feature_a is None or feature_b is None:
                continue

            points_a = lane_points(feature_a)
            points_b = lane_points(feature_b)

            if (
                points_a is None or len(points_a) < 2
                or points_b is None or len(points_b) < 2
            ):
                continue

            index_a = -1 if endpoint_a == 1 else 0
            index_b = -1 if endpoint_b == 1 else 0

            snapped_point = midpoint(
                points_a[index_a],
                points_b[index_b]
            )

            points_a[index_a] = snapped_point
            points_b[index_b] = snapped_point

            feature_a.setGeometry(
                QgsGeometry.fromPolylineXY(points_a)
            )
            feature_b.setGeometry(
                QgsGeometry.fromPolylineXY(points_b)
            )

            snapped_count += 1

    debug("Lane junctions snapped / Lane-junctions sømmet:", snapped_count)



    return lane_features


def commit_lane_features(provider, result, lane_features):

    provider.addFeatures(lane_features)
    result.updateExtents()


def materialize_manual_routes(project_crs, routes, route_lines, lane_features, lane_feature_corridor_indices, canonical_corridor_by_index):
    # ==================================================
    # V7.3 ROUTE-SEQUENCE LANE MATERIALISATION
    #
    # V7.2 samlede de materialiserede corridor-lanes direkte.
    # Det gav dobbelte/overlappende linjer omkring corridor-skift.
    #
    # V7.3 gør det omvendt:
    # 1. materialiser corridor-lanes som interne referencer
    # 2. gennemløb ORIGINALRUTEN i dens egen sample-rækkefølge
    # 3. map hvert originalt samplepunkt til nærmeste relevante lane
    # 4. vægt kontinuitet fra forrige valgte punkt
    # 5. byg rutegeometrien af den ordnede punktsekvens
    #
    # Corridor-lanes er altså referencegeometri, ikke outputdele.
    # ==================================================

    debug("")
    debug("MATERIALIZING MANUAL LAYER VIA ROUTE SEQUENCE / MATERIALISERER MANUAL-LAG VIA ROUTE-SEKVENS")
    debug("----------------------------------------")

    manual_result = QgsVectorLayer(
        "MultiLineString?crs=" + project_crs.authid(),
        MANUAL_RESULT_NAME,
        "memory"
    )

    manual_provider = manual_result.dataProvider()

    manual_provider.addAttributes(
        [
            QgsField("route_no", QVariant.Int),
            QgsField("name", QVariant.String),
            QgsField("part_count", QVariant.Int),
            QgsField("target_scale", QVariant.Double),
            QgsField("length_m", QVariant.Double),
            QgsField("mapped_pts", QVariant.Int),
            QgsField("fallback_pts", QVariant.Int)
        ]
    )

    manual_result.updateFields()

    route_name_by_no = {
        route["number"]: route["name"]
        for route in routes
    }


    # --------------------------------------------------
    # BYG FYSISKE LANE-REFERENCER
    # --------------------------------------------------

    lane_reference_layer = QgsVectorLayer(
        "LineString?crs=" + project_crs.authid(),
        "_temporary_lane_reference_index",
        "memory"
    )

    lane_reference_provider = (
        lane_reference_layer.dataProvider()
    )

    lane_reference_provider.addAttributes(
        [
            QgsField("ref_id", QVariant.Int),
            QgsField("route_no", QVariant.Int),
            QgsField("corridor_id", QVariant.Int),
            QgsField("canonical_corridor_id", QVariant.Int),
            QgsField("physical_lane_id", QVariant.Int),
        ]
    )

    lane_reference_layer.updateFields()

    lane_reference_records = {}
    lane_reference_features = []
    next_reference_id = 0
    physical_lane_identity = {}
    next_physical_lane_id = 0

    skipped_equivalent_lane_references = 0

    for lane_feature, corridor_index in zip(
        lane_features,
        lane_feature_corridor_indices
    ):

        canonical_index = (
            canonical_corridor_by_index.get(
                corridor_index,
                corridor_index
            )
        )

        if corridor_index != canonical_index:
            skipped_equivalent_lane_references += 1
            continue

        route_no = int(lane_feature["route_no"])
        lane_index = float(lane_feature["lane_index"])
        canonical_corridor_id = int(
            lane_feature["corridor_id"]
        )
        physical_lane_key = (
            canonical_corridor_id,
            route_no,
            lane_index
        )
        physical_lane_id = physical_lane_identity.get(
            physical_lane_key
        )
        if physical_lane_id is None:
            physical_lane_id = next_physical_lane_id
            physical_lane_identity[
                physical_lane_key
            ] = physical_lane_id
            next_physical_lane_id += 1

        # lane_feature's geometry is already the precomputed, junction-
        # snapped physical offset curve built in write_corridors_and_lanes
        # / snap_lane_junctions - no need to offset it again here.
        lane_geometry = QgsGeometry(
            lane_feature.geometry()
        )

        if (
            lane_geometry.isNull()
            or lane_geometry.isEmpty()
        ):
            continue

        for points in extract_lines(lane_geometry):

            if len(points) < 2:
                continue

            geometry = QgsGeometry.fromPolylineXY(
                points
            )

            reference_feature = QgsFeature(
                lane_reference_layer.fields()
            )

            reference_feature["ref_id"] = next_reference_id
            reference_feature["route_no"] = route_no
            reference_feature["corridor_id"] = int(
                lane_feature["corridor_id"]
            )
            reference_feature["canonical_corridor_id"] = canonical_corridor_id
            reference_feature["physical_lane_id"] = physical_lane_id
            reference_feature.setGeometry(geometry)

            lane_reference_features.append(
                reference_feature
            )

            lane_reference_records[next_reference_id] = {
                "route_no": route_no,
                "geometry": geometry,
                "corridor_id": int(lane_feature["corridor_id"]),
                "canonical_corridor_id": canonical_corridor_id,
                "lane_index": lane_index,
                "physical_lane_id": physical_lane_id
            }

            next_reference_id += 1


    lane_reference_provider.addFeatures(
        lane_reference_features
    )

    lane_reference_fid_to_ref_id = {}

    for feature in lane_reference_layer.getFeatures():

        lane_reference_fid_to_ref_id[
            feature.id()
        ] = int(feature["ref_id"])


    lane_reference_index = QgsSpatialIndex(
        lane_reference_layer.getFeatures()
    )

    corridor_routes_by_id = {}
    corridor_lane_by_route = {}

    for reference in lane_reference_records.values():
        corridor_routes_by_id.setdefault(
            reference["corridor_id"],
            set()
        ).add(
            reference["route_no"]
        )

    for corridor_id, corridor_routes in corridor_routes_by_id.items():
        corridor_lane_by_route[corridor_id] = {
            reference["route_no"]: reference["lane_index"]
            for reference in lane_reference_records.values()
            if reference["corridor_id"] == corridor_id
        }

    for corridor_id, corridor_routes in corridor_routes_by_id.items():
        debug(
            "Corridor membership / Korridor medlemskab:",
            corridor_id,
            "routes",
            sorted(corridor_routes),
            "lane_indexes",
            {
                route_no: corridor_lane_by_route[corridor_id].get(route_no)
                for route_no in sorted(corridor_routes)
            }
        )

    route_transition_history_by_route = {}
    route_closed_by_route = {}

    debug(
        "Physical lane references / Fysiske lane-referencer:",
        len(lane_reference_records)
    )

    debug(
        "Unique physical lanes / Unikke fysiske lanes:",
        len({
            reference["physical_lane_id"]
            for reference in lane_reference_records.values()
        })
    )

    debug(
        "Equivalent lane references skipped / Ækvivalente lane-referencer sprunget over:",
        skipped_equivalent_lane_references
    )


    # --------------------------------------------------
    # V7.4.4 SCALE-AWARE SEARCH DISTANCE
    #
    # Lane-referencerne ligger fysisk forskudt fra corridor-centerline.
    # Ved større target scale / lane spacing kan en fast søgeafstand
    # derfor ikke nå scriptets egne lane-referencer.
    #
    # Søgeafstanden skaleres automatisk med den største lane-offset
    # og får samtidig geometrisk margin til corridor-match/sampling.
    # --------------------------------------------------

    max_physical_lane_offset = 0.0

    for reference in lane_reference_records.values():
        lane_index = abs(float(reference["lane_index"]))

        physical_lane_offset = (
            lane_index
            * MANUAL_TARGET_SCALE
            * OUTPUT_LANE_SPACING_MM
            / 1000.0
        )

        max_physical_lane_offset = max(
            max_physical_lane_offset,
            physical_lane_offset
        )


    EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE = max(
        MANUAL_LANE_SEARCH_DISTANCE,
        max_physical_lane_offset
        + MATCH_DISTANCE
        + SAMPLE_DISTANCE
    )

    debug(
        "Scale-aware lane search distance:",
        "{:.1f} m".format(
            EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE
        )
    )


    # --------------------------------------------------
    # MAP ORIGINAL RUTE TIL LANE-REFERENCER I RÆKKEFØLGE
    # --------------------------------------------------

    manual_parts_by_route = {}
    manual_stats_by_route = {}

    for route_line in route_lines:

        route_no = route_line["route_no"]
        original_points = route_line["points"]
        route_closed = (
            len(original_points) >= 2
            and point_distance(
                original_points[0],
                original_points[-1]
            )
            < LOOP_CLOSURE_TOLERANCE
        )

        route_closed_by_route[route_no] = route_closed
        debug(
            "Route closure / Rute lukket:",
            route_no,
            "closed" if route_closed else "open"
        )
        route_transition_history = []

        mapped_points = []
        raw_mapped_points = []
        raw_original_points = []
        raw_mapping_states = []
        mapped_count = 0
        fallback_count = 0

        previous_original = None
        previous_mapped = None
        previous_reference_id = None

        for original_point in original_points:

            search_rect = QgsRectangle(
                original_point.x() - EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE,
                original_point.y() - EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE,
                original_point.x() + EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE,
                original_point.y() + EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE
            )

            candidate_fids = (
                lane_reference_index.intersects(
                    search_rect
                )
            )

            best_point = None
            best_score = None
            best_reference_id = None

            original_point_geometry = (
                QgsGeometry.fromPointXY(
                    original_point
                )
            )

            if (
                previous_original is not None
                and previous_mapped is not None
            ):
                expected_step = point_distance(
                    previous_original,
                    original_point
                )
            else:
                expected_step = None

            for candidate_fid in candidate_fids:

                reference_id = (
                    lane_reference_fid_to_ref_id.get(
                        candidate_fid
                    )
                )

                if reference_id is None:
                    continue

                reference = lane_reference_records[
                    reference_id
                ]

                if reference["route_no"] != route_no:
                    continue

                reference_geometry = reference["geometry"]

                nearest_geometry = (
                    reference_geometry.nearestPoint(
                        original_point_geometry
                    )
                )

                if (
                    nearest_geometry.isNull()
                    or nearest_geometry.isEmpty()
                ):
                    continue

                nearest_point = QgsPointXY(
                    nearest_geometry.asPoint()
                )

                source_distance = point_distance(
                    original_point,
                    nearest_point
                )

                if (
                    source_distance
                    > EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE
                ):
                    continue

                score = source_distance

                if (
                    expected_step is not None
                    and previous_mapped is not None
                ):
                    mapped_step = point_distance(
                        previous_mapped,
                        nearest_point
                    )

                    continuity_error = abs(
                        mapped_step - expected_step
                    )

                    score += (
                        continuity_error
                        * MANUAL_CONTINUITY_WEIGHT
                    )

                # V7.4.1 LANE-ORDER INERTIA
                # En ny reference må gerne vinde, men et lane-skift
                # skal være geometrisk begrundet. Straffen måles i
                # fysisk lane-afstand og virker kun når lane_index
                # faktisk ændres.
                if previous_reference_id is not None:
                    previous_reference = lane_reference_records.get(
                        previous_reference_id
                    )

                    if previous_reference is not None:
                        lane_shift = abs(
                            reference["lane_index"]
                            - previous_reference["lane_index"]
                        )

                        score += (
                            lane_shift
                            * MANUAL_TARGET_SCALE
                            * OUTPUT_LANE_SPACING_MM
                            / 1000.0
                            * MANUAL_LANE_INERTIA_WEIGHT
                        )

                if (
                    best_score is None
                    or score < best_score
                ):
                    best_score = score
                    best_point = nearest_point
                    best_reference_id = reference_id


            if best_point is None:
                chosen_point = QgsPointXY(
                    original_point
                )
                fallback_count += 1
            else:
                chosen_point = best_point
                mapped_count += 1

                best_reference = lane_reference_records.get(
                    best_reference_id
                )

                if (
                    previous_reference_id is not None
                    and best_reference is not None
                ):
                    previous_reference = lane_reference_records.get(
                        previous_reference_id
                    )

                    if (
                        previous_reference is not None
                        and previous_reference["corridor_id"]
                        != best_reference["corridor_id"]
                    ):
                        prev_corridor = previous_reference["corridor_id"]
                        curr_corridor = best_reference["corridor_id"]
                        prev_routes = sorted(
                            corridor_routes_by_id.get(
                                prev_corridor,
                                []
                            )
                        )
                        curr_routes = sorted(
                            corridor_routes_by_id.get(
                                curr_corridor,
                                []
                            )
                        )

                        if len(prev_routes) != len(curr_routes):
                            prev_lane_by_route = corridor_lane_by_route.get(
                                prev_corridor,
                                {}
                            )
                            curr_lane_by_route = corridor_lane_by_route.get(
                                curr_corridor,
                                {}
                            )
                            transition = {
                                "from_corridor": prev_corridor,
                                "to_corridor": curr_corridor,
                                "prev_routes": prev_routes,
                                "curr_routes": curr_routes,
                                "route_no": route_no,
                                "prev_lane": previous_reference["lane_index"],
                                "curr_lane": best_reference["lane_index"],
                                "prev_route_lane": prev_lane_by_route.get(route_no),
                                "curr_route_lane": curr_lane_by_route.get(route_no),
                                "prev_physical_lane": previous_reference["physical_lane_id"],
                                "curr_physical_lane": best_reference["physical_lane_id"],
                                "prev_canonical_corridor": previous_reference["canonical_corridor_id"],
                                "curr_canonical_corridor": best_reference["canonical_corridor_id"],
                            }
                            route_transition_history.append(
                                transition
                            )
                            if len(route_transition_history) > 10:
                                route_transition_history.pop(0)

                            debug(
                                "Corridor transition for route",
                                route_no,
                                ":",
                                prev_corridor,
                                "(canonical",
                                transition["prev_canonical_corridor"],
                                ")",
                                "->",
                                curr_corridor,
                                "(canonical",
                                transition["curr_canonical_corridor"],
                                ")"
                            )
                            debug(
                                "Prev routes:",
                                ",".join(str(r) for r in prev_routes)
                            )
                            debug(
                                "Curr routes:",
                                ",".join(str(r) for r in curr_routes)
                            )
                            debug(
                                "Lane index",
                                previous_reference["lane_index"],
                                "(physical",
                                previous_reference["physical_lane_id"],
                                ")",
                                "->",
                                best_reference["lane_index"],
                                "(physical",
                                best_reference["physical_lane_id"],
                                ")"
                            )

                previous_reference_id = best_reference_id

            raw_original_points.append(
                QgsPointXY(original_point)
            )
            raw_mapped_points.append(
                QgsPointXY(chosen_point)
            )
            raw_mapping_states.append(
                best_point is not None
            )

            previous_original = QgsPointXY(
                original_point
            )
            previous_mapped = QgsPointXY(
                chosen_point
            )


        # V7.4.2 TERMINAL TRANSITION
        # Find alle generiske mapped/fallback-grænser i route-sekvensen.
        # På den mappede side fades den fysiske lane-offset gradvist mod
        # originalgeometrien over en fast distance langs originalruten.
        # Dermed undgår vi den direkte "krog" fra corridor-lane til fri rute.
        if raw_original_points:
            cumulative_distances = [0.0]

            for point_index in range(
                1,
                len(raw_original_points)
            ):
                cumulative_distances.append(
                    cumulative_distances[-1]
                    + point_distance(
                        raw_original_points[point_index - 1],
                        raw_original_points[point_index]
                    )
                )

            transition_positions = []

            for point_index in range(
                1,
                len(raw_mapping_states)
            ):
                if (
                    raw_mapping_states[point_index]
                    != raw_mapping_states[point_index - 1]
                ):
                    transition_positions.append(
                        (
                            cumulative_distances[point_index - 1]
                            + cumulative_distances[point_index]
                        )
                        / 2.0
                    )

            for (
                original_point,
                raw_mapped_point,
                is_mapped,
                route_distance
            ) in zip(
                raw_original_points,
                raw_mapped_points,
                raw_mapping_states,
                cumulative_distances
            ):
                transition_weight = 1.0

                if is_mapped and transition_positions:
                    distance_to_transition = min(
                        abs(
                            route_distance
                            - transition_position
                        )
                        for transition_position
                        in transition_positions
                    )

                    transition_weight = min(
                        1.0,
                        distance_to_transition
                        / MANUAL_TERMINAL_TRANSITION_DISTANCE
                    )

                    # Smoothstep: rolig tangent ved både fri geometri
                    # og fuld lane-offset.
                    transition_weight = (
                        transition_weight
                        * transition_weight
                        * (
                            3.0
                            - 2.0 * transition_weight
                        )
                    )

                if is_mapped:
                    chosen_point = QgsPointXY(
                        original_point.x()
                        + (
                            raw_mapped_point.x()
                            - original_point.x()
                        )
                        * transition_weight,
                        original_point.y()
                        + (
                            raw_mapped_point.y()
                            - original_point.y()
                        )
                        * transition_weight
                    )
                else:
                    chosen_point = QgsPointXY(
                        original_point
                    )

                if (
                    not mapped_points
                    or point_distance(
                        mapped_points[-1],
                        chosen_point
                    ) > 0.01
                ):
                    mapped_points.append(
                        chosen_point
                    )


        if len(mapped_points) < 2:
            continue

        mapped_geometry = QgsGeometry.fromPolylineXY(
            mapped_points
        )

        manual_parts_by_route.setdefault(
            route_no,
            []
        ).append(
            mapped_geometry
        )

        stats = manual_stats_by_route.setdefault(
            route_no,
            {
                "mapped": 0,
                "fallback": 0
            }
        )

        stats["mapped"] += mapped_count
        stats["fallback"] += fallback_count


    # --------------------------------------------------
    # ÉN MULTIPART-FEATURE PR. RUTE
    # --------------------------------------------------

    manual_features = []

    for route_no in sorted(manual_parts_by_route):

        parts = manual_parts_by_route[route_no]

        if not parts:
            continue

        geometry = QgsGeometry.collectGeometry(
            parts
        )

        if route_closed_by_route.get(route_no, False):
            closed_points = []
            if geometry.isMultipart():
                parts_geometry = geometry.asMultiPolyline()
                if parts_geometry and parts_geometry[0] and parts_geometry[-1]:
                    first_point = QgsPointXY(parts_geometry[0][0])
                    last_point = QgsPointXY(parts_geometry[-1][-1])
                else:
                    first_point = None
                    last_point = None
            else:
                polyline = geometry.asPolyline()
                if polyline:
                    first_point = QgsPointXY(polyline[0])
                    last_point = QgsPointXY(polyline[-1])
                else:
                    first_point = None
                    last_point = None

            if (
                first_point is not None
                and last_point is not None
                and point_distance(
                    first_point,
                    last_point
                ) > 1.0
            ):
                gap_distance = point_distance(
                    first_point,
                    last_point
                )
                debug(
                    "Closed route gap detected for route",
                    route_no,
                    "distance:",
                    gap_distance
                )
                debug(
                    "Repairing closed route geometry by closing the final part."
                )

                if geometry.isMultipart():
                    parts_geometry = geometry.asMultiPolyline()
                    if parts_geometry and parts_geometry[-1]:
                        parts_geometry[-1].append(
                            QgsPointXY(first_point)
                        )
                        geometry = QgsGeometry.fromMultiPolylineXY(
                            parts_geometry
                        )
                else:
                    polyline = geometry.asPolyline()
                    if polyline:
                        polyline.append(
                            QgsPointXY(first_point)
                        )
                        geometry = QgsGeometry.fromPolylineXY(
                            polyline
                        )

                first_point = QgsPointXY(
                    geometry.asPolyline()[0]
                ) if not geometry.isMultipart() else QgsPointXY(
                    geometry.asMultiPolyline()[0][0]
                )
                last_point = QgsPointXY(
                    geometry.asPolyline()[-1]
                ) if not geometry.isMultipart() else QgsPointXY(
                    geometry.asMultiPolyline()[-1][-1]
                )

                if (
                    first_point is not None
                    and last_point is not None
                    and point_distance(
                        first_point,
                        last_point
                    ) > 1.0
                ):
                    debug(
                        "Closed route repair failed for route",
                        route_no,
                        "distance:",
                        point_distance(
                            first_point,
                            last_point
                        ),
                        "- emitting route with the residual gap instead of "
                        "aborting the whole run."
                    )
                    for transition in route_transition_history:
                        debug(
                            "--- corridor transition ---"
                        )
                        debug(
                            "From corridor",
                            transition["from_corridor"]
                        )
                        debug(
                            "Routes:",
                            ",".join(
                                str(r)
                                for r in transition[
                                    "prev_routes"
                                ]
                            )
                        )
                        debug("↓")
                        debug(
                            "To corridor",
                            transition["to_corridor"]
                        )
                        debug(
                            "Routes:",
                            ",".join(
                                str(r)
                                for r in transition[
                                    "curr_routes"
                                ]
                            )
                        )
                        debug(
                            "Lane mapping:",
                            transition["prev_route_lane"],
                            "->",
                            transition["curr_route_lane"],
                            "(physical",
                            transition["prev_physical_lane"],
                            "->",
                            transition["curr_physical_lane"],
                            ")"
                        )
                    # Non-fatal: one route with a stubborn gap must not discard
                    # the manual layer for every other route in the run. / En
                    # enkelt rute med et vedvarende gap må ikke kassere
                    # manual-laget for alle andre ruter i kørslen.

        route_transition_history_by_route[route_no] = route_transition_history

        stats = manual_stats_by_route.get(
            route_no,
            {
                "mapped": 0,
                "fallback": 0
            }
        )

        feature = QgsFeature(
            manual_result.fields()
        )

        feature["route_no"] = route_no
        feature["name"] = route_name_by_no.get(
            route_no,
            "Rute {}".format(route_no)
        )
        feature["part_count"] = len(parts)
        feature["target_scale"] = MANUAL_TARGET_SCALE
        feature["length_m"] = geometry.length()
        feature["mapped_pts"] = stats["mapped"]
        feature["fallback_pts"] = stats["fallback"]
        feature.setGeometry(geometry)

        manual_features.append(
            feature
        )


    manual_provider.addFeatures(
        manual_features
    )

    manual_result.updateExtents()

    debug(
        "Manuelle rutefeatures:",
        len(manual_features)
    )

    for feature in manual_features:

        total_points = (
            feature["mapped_pts"]
            + feature["fallback_pts"]
        )

        if total_points > 0:
            mapped_percent = (
                100.0
                * feature["mapped_pts"]
                / total_points
            )
        else:
            mapped_percent = 0.0

        debug(
            "Rute",
            feature["route_no"],
            "→",
            feature["part_count"],
            "dele | mapped:",
            "{:.1f}%".format(mapped_percent),
            "| fallback:",
            feature["fallback_pts"]
        )



    return manual_result, manual_features, EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE


def apply_renderers(lane_features, manual_features, manual_result, result):
    # ==================================================
    # KARTOGRAFISK RENDERER
    #
    # lane_features har allerede deres endelige, sømmede offset-geometri
    # (se write_corridors_and_lanes / snap_lane_junctions), så begge lag
    # tegnes som en almindelig per-rute farvet linjesymbol - ingen
    # geometry-generator/offset_curve() ved render-tid.
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




def report_results(EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE, corridor_result, corridors, joined_count, manual_result, preferred_order_edges, preferred_order_reversed_count, raw_change_count, result, route_lines, stability_repair_count, stable_change_count):
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
    debug("Effective lane search distance / Effektiv lane søgeafstand:", EFFECTIVE_MANUAL_LANE_SEARCH_DISTANCE)
    debug("Terminal transition distance / Terminal overgangsafstand:", MANUAL_TERMINAL_TRANSITION_DISTANCE)
    debug("Preferred-order junctions / Preferred-order junctioner:", len(preferred_order_edges))
    debug("Preferred-order reversed corridors / Foretrukne reversed korridorer:", preferred_order_reversed_count)
    debug("")
    debug(
        "CMRL Route Layout contains one multipart feature per original route. / CMRL Route Layout indeholder én multipart-feature pr. original rute."
    )
    debug(
        "Geometry is built in the original route order by mapping to the physical lane references. / Geometrien er bygget i originalrutens rækkefølge ved mapping til de fysiske lane-referencer."
    )
    debug(
        "The layer is intended for manual cartographic editing with the Vertex Tool. / Laget er beregnet til manuel kartografisk efterredigering med Vertex Tool."
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
    preferred_order_junction_count: int
    preferred_order_reversed_corridor_count: int
    effective_manual_lane_search_distance: float


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
        canonical_corridor_by_index = resolve_corridor_equivalence(merged_corridors)
        preferred_order_edges, preferred_order_reversed_count, corridor_reversed = resolve_preferred_order(
            merged_corridors
        )
        corridor_result, corridor_provider, result, provider = create_output_layers(
            project, project_crs
        )
        lane_features, lane_feature_corridor_indices = write_corridors_and_lanes(
            merged_corridors,
            corridor_result,
            corridor_provider,
            result,
            provider,
        )
        lane_features = snap_lane_junctions(
            lane_features,
            lane_feature_corridor_indices,
            preferred_order_edges,
            corridor_reversed,
        )
        commit_lane_features(provider, result, lane_features)
        manual_result, manual_features, effective_search_distance = materialize_manual_routes(
            project_crs,
            routes,
            route_lines,
            lane_features,
            lane_feature_corridor_indices,
            canonical_corridor_by_index,
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
            effective_search_distance,
            corridor_result,
            corridors,
            joined_count,
            manual_result,
            preferred_order_edges,
            preferred_order_reversed_count,
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
            preferred_order_junction_count=len(preferred_order_edges),
            preferred_order_reversed_corridor_count=preferred_order_reversed_count,
            effective_manual_lane_search_distance=effective_search_distance,
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
    SEARCH_DISTANCE = "SEARCH_DISTANCE"
    CONTINUITY_WEIGHT = "CONTINUITY_WEIGHT"
    INERTIA_WEIGHT = "INERTIA_WEIGHT"
    TERMINAL_TRANSITION_DISTANCE = "TERMINAL_TRANSITION_DISTANCE"
    JUNCTION_DISTANCE = "JUNCTION_DISTANCE"
    PREFERRED_ORDER_PASSES = "PREFERRED_ORDER_PASSES"
    ADD_DIAGNOSTIC_LAYERS = "ADD_DIAGNOSTIC_LAYERS"

    OUT_MANUAL = "OUT_MANUAL"
    OUT_AUTOMATIC = "OUT_AUTOMATIC"
    OUT_CORRIDORS = "OUT_CORRIDORS"
    OUT_ROUTE_COUNT = "OUT_ROUTE_COUNT"
    OUT_REVERSED_CORRIDORS = "OUT_REVERSED_CORRIDORS"

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
            self.SEARCH_DISTANCE,
            "Search distance (m) / Søgeafstand (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.mapping.manual_lane_search_distance,
            minValue=0.0,
        )
        param.setDescription(
            "Lane search distance in meters for manual mapping. / Lane-søgeafstand i meter for manuel mapping."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.CONTINUITY_WEIGHT,
            "Continuity weight / Kontinuitetsvægt",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.mapping.manual_continuity_weight,
            minValue=0.0,
        )
        param.setDescription(
            "Penalty weight for route continuity at junctions. / Straffeværdi for kontinuitet ved kryds."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.INERTIA_WEIGHT,
            "Inertia weight / Inertiavægt",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.mapping.manual_lane_inertia_weight,
            minValue=0.0,
        )
        param.setDescription(
            "Penalty weight for lane order inertia. / Straffeværdi for lane-order inerti."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.TERMINAL_TRANSITION_DISTANCE,
            "Terminal transition (m) / Terminal overgang (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.mapping.manual_terminal_transition_distance,
            minValue=0.0,
        )
        param.setDescription(
            "Distance for the terminal transition from corridor lanes to original route. / Distance for terminal overgang fra corridor-lanes til originalrute."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.JUNCTION_DISTANCE,
            "Junction distance (m) / Junctionafstand (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=p.preferred_order.junction_distance,
            minValue=0.0,
        )
        param.setDescription(
            "Preferred-order junction distance in meters. / Preferred-order junctionafstand i meter."
        )
        param.setMetadata({"widget_wrapper": {"decimals": 0}})
        self.addParameter(param)

        self.addParameter(QgsProcessingParameterNumber(
            self.PREFERRED_ORDER_PASSES,
            "Preferred-order passes / Preferred-order gennemløb",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=p.preferred_order.passes,
            minValue=1,
        ))
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
        self.addOutput(QgsProcessingOutputNumber(
            self.OUT_REVERSED_CORRIDORS, "Preferred-order reversed corridors / Foretrukne reversed korridorer"
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

        def get_int(name, default_value):
            value = self.parameterAsInt(
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
                offset_segments=defaults.cartography.offset_segments,
                offset_miter_limit=defaults.cartography.offset_miter_limit,
            ),
            mapping=MappingParameters(
                manual_lane_search_distance=get_double(
                    self.SEARCH_DISTANCE,
                    defaults.mapping.manual_lane_search_distance,
                ),
                manual_continuity_weight=get_double(
                    self.CONTINUITY_WEIGHT,
                    defaults.mapping.manual_continuity_weight,
                ),
                manual_lane_inertia_weight=get_double(
                    self.INERTIA_WEIGHT,
                    defaults.mapping.manual_lane_inertia_weight,
                ),
                manual_terminal_transition_distance=get_double(
                    self.TERMINAL_TRANSITION_DISTANCE,
                    defaults.mapping.manual_terminal_transition_distance,
                ),
            ),
            preferred_order=PreferredOrderParameters(
                junction_distance=get_double(
                    self.JUNCTION_DISTANCE,
                    defaults.preferred_order.junction_distance,
                ),
                min_shared_routes=defaults.preferred_order.min_shared_routes,
                passes=get_int(
                    self.PREFERRED_ORDER_PASSES,
                    defaults.preferred_order.passes,
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
            f"Done: {engine_result.route_count} routes, {engine_result.preferred_order_reversed_corridor_count} preferred-order reversals. / Færdig: {engine_result.route_count} ruter, {engine_result.preferred_order_reversed_corridor_count} preferred-order reversals."
        )

        return {
            self.OUT_MANUAL: engine_result.manual_lane_layer.id(),
            self.OUT_AUTOMATIC: engine_result.automatic_lane_layer.id(),
            self.OUT_CORRIDORS: engine_result.corridor_layer.id(),
            self.OUT_ROUTE_COUNT: engine_result.route_count,
            self.OUT_REVERSED_CORRIDORS: (
                engine_result.preferred_order_reversed_corridor_count
            ),
        }


# Når filen køres direkte i QGIS' script-editor, bevares den gamle regressionstest.
# Når QGIS Processing loader filen som et script, skal algoritmeklassen registreres
# uden at motoren automatisk køres.
if __name__ == "__main__":
    ENGINE_RESULT = main()
