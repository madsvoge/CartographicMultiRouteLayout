from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsLayerTreeLayer,
    QgsSpatialIndex,
    QgsRectangle,
    QgsLineSymbol,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsGeometryGeneratorSymbolLayer,
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
    # Diagnostisk spacing i projektets map units (meter i meterbaseret CRS).
    diagnostic_lane_spacing_map_units: float = 8.0

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
            ("diagnostic_lane_spacing_map_units", self.cartography.diagnostic_lane_spacing_map_units),
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


@dataclass
class EngineContext:
    parameters: EngineParameters
    project: QgsProject = None
    project_crs: object = None
    root: object = None
    group: object = None

    @property
    def index_search_margin(self):
        return self.parameters.corridor.index_search_margin

    @property
    def angle_tolerance(self):
        return self.parameters.corridor.angle_tolerance_radians


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
    global DIAGNOSTIC_LANE_SPACING_MAP_UNITS, OUTPUT_LANE_SPACING_MM
    global LANE_WIDTH_MM, MANUAL_TARGET_SCALE, OFFSET_SEGMENTS, OFFSET_MITER_LIMIT
    global MANUAL_LANE_SEARCH_DISTANCE, MANUAL_CONTINUITY_WEIGHT
    global MANUAL_LANE_INERTIA_WEIGHT, MANUAL_TERMINAL_TRANSITION_DISTANCE
    global PREFERRED_ORDER_JUNCTION_DISTANCE, PREFERRED_ORDER_MIN_SHARED_ROUTES
    global PREFERRED_ORDER_PASSES, CORRIDOR_EQUIVALENCE_DISTANCE
    global CORRIDOR_EQUIVALENCE_MIN_COVERAGE, ROUTE_COLORS
    global SAMPLE_DISTANCE, MATCH_DISTANCE, ANGLE_TOLERANCE
    global MIN_STABLE_LENGTH, STABILITY_PASSES, MIN_CORRIDOR_LENGTH
    global INDEX_SEARCH_MARGIN

    GROUP_NAME = parameters.io.output_group_name
    CORRIDOR_RESULT_NAME = parameters.io.corridor_result_name
    RESULT_NAME = parameters.io.result_name
    MANUAL_RESULT_NAME = parameters.io.manual_result_name

    DIAGNOSTIC_LANE_SPACING_MAP_UNITS = parameters.cartography.diagnostic_lane_spacing_map_units
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

    return EngineContext(parameters=parameters)


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




def setup_project(context=None):
    # ==================================================
    # PROJEKT
    # ==================================================
    # Transformering læser den aktive QGIS-kontekst fra argumenterne.

    project = QgsProject.instance()
    root = project.layerTreeRoot()

    if context is not None:
        context.project = project
        context.root = root

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

    if context is not None:
        context.project_crs = project_crs
        context.group = group

    debug("")
    debug("========================================")
    debug("Cartographic Multi-Route Layout v8.2")
    debug("VERSION", VERSION)
    debug("========================================")

    return project, root, group, project_crs


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
    next_line_id = 0

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

                geometry = QgsGeometry.fromPolylineXY(
                    sampled
                )

                route_lines.append(
                    {
                        "line_id": next_line_id,
                        "route_no": route["number"],
                        "route_name": route["name"],
                        "points": sampled,
                        "geometry": geometry
                    }
                )

                next_line_id += 1

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
                "segment_id": next_segment_id, "line_id": route_line["line_id"],
                "route_no": route_line["route_no"], "segment_index": segment_index,
                "geometry": geometry, "angle": segment_angle(point1, point2)
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



    return corridors


def merge_corridors(corridors):
    # ==================================================
    # SAMMENKÆD DIREKTE NABO-RUNS
    #
    # Kun inden for samme source route og samme route-set.
    # Ingen afstandssøgning. Ingen nabo-gæt.
    # De skal dele præcis endepunkt i den oprindelige
    # samplede routesekvens.
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

    # Geometrisk endpoint-baseret lookup. Tolerancen er kun
    # til floating point-identitet, ikke topologisk snapping.
    ENDPOINT_EPSILON = 0.001


    def endpoint_key(point):

        return (
            int(round(point.x() / ENDPOINT_EPSILON)),
            int(round(point.y() / ENDPOINT_EPSILON))
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

    endpoint_lookup = {}

    for corridor_id, corridor in enumerate(
        corridors_sorted
    ):

        points = geometry_points(
            corridor["geometry"]
        )

        if len(points) < 2:
            continue

        for endpoint_index, point in (
            (0, points[0]),
            (1, points[-1])
        ):

            key = (
                corridor["source_route"],
                corridor["routes"],
                endpoint_key(point)
            )

            endpoint_lookup.setdefault(
                key,
                []
            ).append(
                (
                    corridor_id,
                    endpoint_index
                )
            )


    while unused:

        current_id = min(unused)
        unused.remove(current_id)

        current = corridors_sorted[current_id]

        chain_points = geometry_points(
            current["geometry"]
        )

        source_route = current["source_route"]
        routes_key = current["routes"]

        extended = True

        while extended:

            extended = False

            # Forsøg først ved slutningen.
            end_key = (
                source_route,
                routes_key,
                endpoint_key(chain_points[-1])
            )

            for candidate_id, endpoint_index in (
                endpoint_lookup.get(end_key, [])
            ):

                if candidate_id not in unused:
                    continue

                candidate = corridors_sorted[
                    candidate_id
                ]

                candidate_points = geometry_points(
                    candidate["geometry"]
                )

                if len(candidate_points) < 2:
                    continue

                if endpoint_index == 1:
                    candidate_points.reverse()

                chain_points.extend(
                    candidate_points[1:]
                )

                unused.remove(candidate_id)
                joined_count += 1
                extended = True
                break

            if extended:
                continue

            # Forsøg derefter ved starten.
            start_key = (
                source_route,
                routes_key,
                endpoint_key(chain_points[0])
            )

            for candidate_id, endpoint_index in (
                endpoint_lookup.get(start_key, [])
            ):

                if candidate_id not in unused:
                    continue

                candidate = corridors_sorted[
                    candidate_id
                ]

                candidate_points = geometry_points(
                    candidate["geometry"]
                )

                if len(candidate_points) < 2:
                    continue

                if endpoint_index == 0:
                    candidate_points.reverse()

                chain_points = (
                    candidate_points[:-1]
                    + chain_points
                )

                unused.remove(candidate_id)
                joined_count += 1
                extended = True
                break

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

            if (
                corridor1["geometry"].distance(
                    corridor2["geometry"]
                )
                > CORRIDOR_EQUIVALENCE_DISTANCE
            ):
                continue

            coverage = corridor_overlap_coverage(
                corridor1["geometry"],
                corridor2["geometry"]
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



    return preferred_order_edges, preferred_order_reversed_count


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

            offset_m = (
                lane_index * DIAGNOSTIC_LANE_SPACING_MAP_UNITS
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
            lane_feature["offset_m"] = offset_m
            lane_feature["source_route"] = (
                corridor["source_route"]
            )
            lane_feature["length_m"] = length_m

            # VIGTIGT:
            # Alle lanes i korridoren får samme centerline.
            lane_feature.setGeometry(geometry)

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

    provider.addFeatures(
        lane_features
    )

    result.updateExtents()



    return lane_features, lane_feature_corridor_indices


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
            QgsField("corridor_id", QVariant.Int)
        ]
    )

    lane_reference_layer.updateFields()

    lane_reference_records = {}
    lane_reference_features = []
    next_reference_id = 0

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

        physical_offset = (
            lane_index
            * MANUAL_TARGET_SCALE
            * OUTPUT_LANE_SPACING_MM
            / 1000.0
        )

        source_geometry = QgsGeometry(
            lane_feature.geometry()
        )

        if abs(physical_offset) < 0.000001:
            lane_geometry = source_geometry
        else:
            lane_geometry = source_geometry.offsetCurve(
                physical_offset,
                OFFSET_SEGMENTS,
                Qgis.JoinStyle.Round,
                OFFSET_MITER_LIMIT
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
            reference_feature.setGeometry(geometry)

            lane_reference_features.append(
                reference_feature
            )

            lane_reference_records[next_reference_id] = {
                "route_no": route_no,
                "geometry": geometry,
                "corridor_id": int(lane_feature["corridor_id"]),
                "lane_index": lane_index
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

    for corridor_id, routes in corridor_routes_by_id.items():
        corridor_lane_by_route[corridor_id] = {
            reference["route_no"]: reference["lane_index"]
            for reference in lane_reference_records.values()
            if reference["corridor_id"] == corridor_id
        }

    route_transition_history_by_route = {}
    route_closed_by_route = {}

    debug(
        "Physical lane references / Fysiske lane-referencer:",
        len(lane_reference_records)
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
            < 1.0
        )

        route_closed_by_route[route_no] = route_closed
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
                                "->",
                                curr_corridor
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
                                "Lane",
                                previous_reference["lane_index"],
                                "->",
                                best_reference["lane_index"]
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
                        "Closed route assertion failed for route",
                        route_no,
                        "distance:",
                        point_distance(
                            first_point,
                            last_point
                        )
                    )
                    for transition in route_transition_history_by_route.get(
                        route_no,
                        []
                    ):
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
                            transition["curr_route_lane"]
                        )
                    assert False, (
                    "Closed route {} materialization broke closure".format(
                        route_no
                    )
                )

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
    # ==================================================

    lane_expression = """offset_curve(
        $geometry,
        "lane_index" * @map_scale * {spacing}
    )""".format(spacing=OUTPUT_LANE_SPACING_MM / 1000.0)


    def build_geometry_generator_renderer(expression):

        categories = []

        route_numbers_in_result = sorted(
            {
                feature["route_no"]
                for feature in lane_features
            }
        )

        for route_no in route_numbers_in_result:

            color = ROUTE_COLORS[
                (route_no - 1) % len(ROUTE_COLORS)
            ]

            line_symbol = QgsLineSymbol.createSimple({})

            geometry_generator = (
                QgsGeometryGeneratorSymbolLayer.create(
                    {
                        "geometryModifier": expression,
                        "SymbolType": "Line"
                    }
                )
            )

            sub_symbol = QgsLineSymbol.createSimple(
                {
                    "color": color,
                    "width": str(LANE_WIDTH_MM),
                    "width_unit": "MM",
                    "capstyle": "round",
                    "joinstyle": "round"
                }
            )

            geometry_generator.setSubSymbol(
                sub_symbol
            )

            line_symbol.deleteSymbolLayer(0)
            line_symbol.appendSymbolLayer(
                geometry_generator
            )

            categories.append(
                QgsRendererCategory(
                    route_no,
                    line_symbol,
                    "Rute {}".format(route_no)
                )
            )

        return QgsCategorizedSymbolRenderer(
            "route_no",
            categories
        )


    def build_manual_renderer():

        categories = []

        route_numbers_in_result = sorted(
            {
                int(feature["route_no"])
                for feature in manual_features
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
        build_geometry_generator_renderer(
            lane_expression
        )
    )

    manual_result.setRenderer(
        build_manual_renderer()
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
        engine_context = _bind_engine_parameters(
            DEFAULT_PARAMETERS if parameters is None else parameters
        )
        parameters = engine_context.parameters

        # QGIS-kontekst publiceres kun som intern kompatibilitet for den nuværende
        # geometrikerne. Kaldere skal ikke sætte disse kontekstvariabler.
        project, root, group, project_crs = setup_project(engine_context)

        engine_context.project_crs = project_crs
        engine_context.group = group

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
        preferred_order_edges, preferred_order_reversed_count = resolve_preferred_order(
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

    engine_context.project_crs = project_crs
    engine_context.group = group

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
    preferred_order_edges, preferred_order_reversed_count = resolve_preferred_order(
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
                diagnostic_lane_spacing_map_units=(
                    defaults.cartography.diagnostic_lane_spacing_map_units
                ),
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
