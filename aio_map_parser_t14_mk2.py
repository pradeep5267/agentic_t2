#!/usr/bin/env python3
import os
import json
import shutil
from xml.etree import ElementTree as ET

from shapely.geometry import Polygon, LineString, mapping
from shapely.ops import unary_union


def parse_kml_polygons(kml_path):
    """
    Parse a plain KML file and extract every <Placemark>'s <name> and <Polygon>.
    Returns a list of (placemark_name, shapely.Polygon) pairs.
    """
    if not os.path.exists(kml_path):
        raise FileNotFoundError(f"KML file not found: {kml_path}")

    tree = ET.parse(kml_path)
    root = tree.getroot()
    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    polygons = []
    for placemark in root.findall(".//kml:Placemark", ns):
        # Extract the placemark name (fall back to "Unnamed" if missing)
        name_elem = placemark.find("kml:name", ns)
        placemark_name = name_elem.text.strip() if name_elem is not None else "Unnamed"

        # Every <Polygon> inside this <Placemark>
        for kml_poly in placemark.findall(".//kml:Polygon", ns):
            coords_elem = kml_poly.find(".//kml:coordinates", ns)
            if coords_elem is None:
                continue

            # Parse whitespace‐separated "lon,lat,alt" strings
            raw_points = coords_elem.text.strip().split()
            pts = []
            for entry in raw_points:
                parts = entry.split(",")
                lon, lat = float(parts[0]), float(parts[1])
                pts.append((lon, lat))

            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)  # Attempt to fix minor geometry issues

            polygons.append((placemark_name, poly))

    if not polygons:
        raise ValueError(f"No <Polygon> found in KML: {kml_path}")
    return polygons


def parse_osm_file(osm_path):
    """
    Parse an OSM XML file and return:
      - nodes: dict {node_id → (lon, lat)}
      - allowed_ways: list of ways where 'highway' ∈ {drivable tags} AND no access=no
      - restricted_ways: list of ways where 'highway' ∈ {drivable tags} AND access=no OR motor_vehicle=no OR vehicle=no

    Drivable tags follow Map Features → Highway → Roads & Link roads:
      motorway, trunk, primary, secondary, tertiary,
      unclassified, residential, service, living_street,
      motorway_link, trunk_link, primary_link, secondary_link, tertiary_link
    """
    if not os.path.exists(osm_path):
        raise FileNotFoundError(f"OSM file not found: {osm_path}")

    tree = ET.parse(osm_path)
    root = tree.getroot()

    # 1) Collect all nodes
    nodes = {
        node.get("id"): (float(node.get("lon")), float(node.get("lat")))
        for node in root.findall("node")
    }

    # 2) Define drivable/parkable highway types
    allowed_highway = {
        # Map Features → Highway → Roads (1.11.1)
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "unclassified", "residential", "service", "living_street",
        # Map Features → Highway → Link roads (1.11.2)
        "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"
    }

    allowed_ways = []
    restricted_ways = []

    for way in root.findall("way"):
        tags = {tag.get("k"): tag.get("v") for tag in way.findall("tag")}
        hwy = tags.get("highway")

        # Skip if not a drivable highway type
        if hwy not in allowed_highway:
            continue

        # Check for explicit restrictions (Map Features → Key:access)
        is_restricted = (
            tags.get("access") == "no"
            or tags.get("motor_vehicle") == "no"
            or tags.get("vehicle") == "no"
        )

        node_refs = [nd.get("ref") for nd in way.findall("nd")]
        way_record = {"id": way.get("id"), "nodes": node_refs, "tags": tags}

        if is_restricted:
            restricted_ways.append(way_record)
        else:
            allowed_ways.append(way_record)

    return nodes, allowed_ways, restricted_ways


def extract_endpoints(ways, nodes):
    """
    Convert each way entry into a Shapely LineString and record its endpoints:
      - id: way_id
      - name: tags['name'] or fallback "way_<id>"
      - start: (lon, lat) of first node
      - end: (lon, lat) of last node
      - geometry: Shapely LineString of all node coordinates
    Returns a list of dicts: {id, name, start, end, geometry, tags (optional)}.
    """
    endpoints = []
    for way in ways:
        node_refs = way["nodes"]
        if len(node_refs) < 2:
            continue

        first_id, last_id = node_refs[0], node_refs[-1]
        if first_id in nodes and last_id in nodes:
            start = nodes[first_id]
            end = nodes[last_id]
            name = way["tags"].get("name", f"way_{way['id']}")
            coords = [nodes[nid] for nid in node_refs if nid in nodes]
            line = LineString(coords)

            endpoints.append({
                "id": way["id"],
                "name": name,
                "start": start,
                "end": end,
                "geometry": line,
                "tags": way["tags"]  # preserve all tags if needed
            })

    return endpoints


def assign_roads_to_polygons(road_list, polygons):
    """
    Given a list of road dicts (each with a Shapely 'geometry') and a list of
    (placemark_name, Polygon) pairs, assign each road to the first polygon that
    intersects it. Returns a list of new road dicts with an added 'polygon' key.

    If a road intersects multiple polygons, it will be assigned to each found polygon
    (duplicate entries); if that is not desired, modify to only choose the first match.
    """
    assigned = []
    for road in road_list:
        geom = road["geometry"]
        matched_any = False
        for (placemark_name, poly) in polygons:
            if geom.intersects(poly):
                # Copy road dict and add 'polygon' property
                newrec = road.copy()
                newrec["polygon"] = placemark_name
                assigned.append(newrec)
                matched_any = True
        if not matched_any:
            # If no polygon match, assign to null or skip. Here, we skip.
            pass
    return assigned


def write_geojson(all_road_endpoints, output_path="roads_with_polygons.geojson"):
    """
    Given a list of roads (each with 'geometry' and properties including 'id', 'name',
    'tags', 'status', 'polygon'), write them out as a single GeoJSON FeatureCollection.
    Each feature's 'properties' will include:
      - id
      - name
      - highway (from tags)
      - status: "allowed" or "restricted"
      - polygon: <PlacemarkName>
      - ...plus any other tags you want to carry forward...
    """
    features = []
    for road in all_road_endpoints:
        geom = road["geometry"]
        
        # FIX: The status was already set correctly in the tags earlier
        # Just use it directly from tags
        status = road["tags"].get("status", "allowed")
        
        props = {
            "id": road["id"],
            "name": road["name"],
            "highway": road["tags"].get("highway"),
            "status": status,
            "polygon": road.get("polygon", "NoPolygon")
        }
        # Optionally include additional tags:
        # for k, v in road["tags"].items():
        #     if k not in props:
        #         props[k] = v

        feature = {
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": props
        }
        features.append(feature)

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2)

    print(f"GeoJSON (with polygons + status) saved to: {output_path}")
    
    # Print summary statistics
    allowed_count = sum(1 for f in features if f["properties"]["status"] == "allowed")
    restricted_count = sum(1 for f in features if f["properties"]["status"] == "restricted")
    print(f"  - Allowed roads: {allowed_count}")
    print(f"  - Restricted roads: {restricted_count}")
    print(f"  - Total roads: {len(features)}")
    
    # Move the file to the static directory
    base_dir = os.path.abspath(os.path.dirname(__file__))
    static_dir = os.path.join(base_dir, "static")
    os.makedirs(static_dir, exist_ok=True)
    static_path = os.path.join(static_dir, "roads_with_polygons.geojson")
    shutil.copy(output_path, static_path)
    print(f"Copied GeoJSON to static folder: {static_path}")


if __name__ == "__main__":
    # === User‐Configurable Paths ===
    # Changed paths to match aio_t14b_mk2.py's file structure
    SAVE_DIR = "/media/gamedisk/KTP_artefacts/pssavmk2_t2"
    PREPROCESSED_DIR = f"{SAVE_DIR}/preprocessed_roads"
    
    # Input files should be in the preprocessed directory
    osm_file = f"{PREPROCESSED_DIR}/combined_area.osm"
    kml_file = f"{PREPROCESSED_DIR}/areas_a1.kml"
    
    # Output to the main recording directory
    output_geojson = f"{PREPROCESSED_DIR}/roads_with_polygons.geojson"
    # ==============================

    # Create directories if they don't exist
    os.makedirs(SAVE_DIR, exist_ok=True)
    os.makedirs(PREPROCESSED_DIR, exist_ok=True)

    # 1) Parse OSM XML for drivable vs restricted roads
    nodes_dict, allowed_list, restricted_list = parse_osm_file(osm_file)
    
    print(f"Parsed OSM file:")
    print(f"  - Allowed ways: {len(allowed_list)}")
    print(f"  - Restricted ways: {len(restricted_list)}")

    # 2) Convert each way to a LineString + endpoints
    allowed_endpoints = extract_endpoints(allowed_list, nodes_dict)
    restricted_endpoints = extract_endpoints(restricted_list, nodes_dict)

    # 3) Label each endpoint record with its status
    for rec in allowed_endpoints:
        rec["tags"]["status"] = "allowed"
    for rec in restricted_endpoints:
        rec["tags"]["status"] = "restricted"

    # 4) Parse KML polygons
    try:
        kml_polygons = parse_kml_polygons(kml_file)
        print(f"Parsed {len(kml_polygons)} polygons from KML")
    except Exception as e:
        print(f"Warning: Could not parse polygons from KML ({kml_file}): {e}")
        kml_polygons = []

    # 5) Assign roads to polygons
    allowed_assigned = assign_roads_to_polygons(allowed_endpoints, kml_polygons)
    restricted_assigned = assign_roads_to_polygons(restricted_endpoints, kml_polygons)
    
    print(f"Roads assigned to polygons:")
    print(f"  - Allowed roads in polygons: {len(allowed_assigned)}")
    print(f"  - Restricted roads in polygons: {len(restricted_assigned)}")

    # 6) Combine into one list
    all_assigned = allowed_assigned + restricted_assigned

    # 7) Write the combined GeoJSON
    write_geojson(all_assigned, output_geojson)