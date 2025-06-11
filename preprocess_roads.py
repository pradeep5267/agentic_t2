#%%
#!/usr/bin/env python3
"""
preprocess_roads.py - Convert road GeoJSON to optimized format for real-time detection
"""

import json
import pickle
import numpy as np
from shapely.geometry import shape, LineString, Point
from shapely.ops import transform
import shapely.prepared as prep
import pyproj
from functools import partial
import os

# Configuration
BUFFER_WIDTH_METERS = 10  # Narrow buffer as requested
SEGMENT_LENGTH_METERS = 10  # 10m segments
SAVE_DIR = "/media/gamedisk/KTP_artefacts/pssavmk2_t2/preprocessed_roads"
OUTPUT_DIR = f"{SAVE_DIR}/"
INPUT_GEOJSON = f"{SAVE_DIR}/roads_with_polygons.geojson"

# Create output directory
os.makedirs(OUTPUT_DIR, exist_ok=True)

def segment_linestring(linestring, segment_length_m):
    """
    Segment a LineString into points at regular intervals
    Returns list of (lon, lat) tuples
    """
    # Project to UTM for accurate distance calculations
    # Use the centroid to determine UTM zone
    centroid = linestring.centroid
    utm_zone = int((centroid.x + 180) / 6) + 1
    
    # Create projection transformers
    wgs84 = pyproj.CRS("EPSG:4326")
    utm = pyproj.CRS(f"EPSG:326{utm_zone:02d}")  # Northern hemisphere
    
    project_to_utm = pyproj.Transformer.from_crs(wgs84, utm, always_xy=True).transform
    project_to_wgs84 = pyproj.Transformer.from_crs(utm, wgs84, always_xy=True).transform
    
    # Transform to UTM
    utm_line = transform(project_to_utm, linestring)
    
    # Calculate segments
    total_length = utm_line.length
    num_segments = max(int(total_length / segment_length_m), 1)
    
    segments = []
    for i in range(num_segments + 1):
        # Get point at normalized distance
        point_utm = utm_line.interpolate(i / num_segments, normalized=True)
        # Transform back to WGS84
        point_wgs84 = transform(project_to_wgs84, Point(point_utm.x, point_utm.y))
        segments.append((point_wgs84.x, point_wgs84.y))
    
    return segments

def create_buffer_polygon(linestring, buffer_width_m):
    """
    Create a buffer polygon around a linestring
    """
    # Project to UTM for accurate buffer
    centroid = linestring.centroid
    utm_zone = int((centroid.x + 180) / 6) + 1
    
    wgs84 = pyproj.CRS("EPSG:4326")
    utm = pyproj.CRS(f"EPSG:326{utm_zone:02d}")
    
    project_to_utm = pyproj.Transformer.from_crs(wgs84, utm, always_xy=True).transform
    project_to_wgs84 = pyproj.Transformer.from_crs(utm, wgs84, always_xy=True).transform
    
    # Transform to UTM, buffer, transform back
    utm_line = transform(project_to_utm, linestring)
    utm_buffer = utm_line.buffer(buffer_width_m)
    wgs84_buffer = transform(project_to_wgs84, utm_buffer)
    
    return wgs84_buffer

def preprocess_roads(geojson_path):
    """
    Preprocess roads into optimized format
    """
    print(f"Loading roads from {geojson_path}")
    
    with open(geojson_path, 'r') as f:
        data = json.load(f)
    
    road_data = {}
    bounds_list = []
    buffer_polygons = []
    
    total_roads = len(data['features'])
    
    for idx, feature in enumerate(data['features']):
        if feature['geometry']['type'] != 'LineString':
            continue
        
        road_id = feature['properties']['id']
        road_name = feature['properties'].get('name', f'Unnamed_{road_id}')
        road_status = feature['properties'].get('status', 'unknown')
        
        # Skip non-allowed roads
        if road_status != 'allowed':
            continue
        
        print(f"Processing road {idx+1}/{total_roads}: {road_id} - {road_name}")
        
        # Create LineString
        coords = feature['geometry']['coordinates']
        linestring = LineString(coords)
        
        # Create buffer polygon
        buffer_poly = create_buffer_polygon(linestring, BUFFER_WIDTH_METERS)
        
        # Create segments
        segments = segment_linestring(linestring, SEGMENT_LENGTH_METERS)
        
        # Store road data
        road_data[road_id] = {
            'name': road_name,
            'status': road_status,
            'polygon': feature['properties'].get('polygon', 'unknown'),
            'highway': feature['properties'].get('highway', 'unknown'),
            'segments': segments,
            'total_segments': len(segments),
            'buffer_polygon': buffer_poly,
            'linestring': linestring,
            'length_m': linestring.length * 111320  # Rough conversion
        }
        
        # Store bounds for quick filtering
        bounds = buffer_poly.bounds  # (minx, miny, maxx, maxy)
        bounds_list.append(bounds)
        buffer_polygons.append(buffer_poly)
    
    print(f"\nProcessed {len(road_data)} allowed roads")
    
    # Convert to numpy array for fast bounds checking
    bounds_array = np.array(bounds_list)
    
    # Save preprocessed data
    print("\nSaving preprocessed data...")
    
    # Save bounds array
    np.save(os.path.join(OUTPUT_DIR, "road_bounds.npy"), bounds_array)
    
    # Save road data and polygons
    with open(os.path.join(OUTPUT_DIR, "road_data.pkl"), 'wb') as f:
        pickle.dump(road_data, f)
    
    with open(os.path.join(OUTPUT_DIR, "buffer_polygons.pkl"), 'wb') as f:
        pickle.dump(buffer_polygons, f)
    
    # Save road ID mapping for bounds/polygon indices
    road_ids = list(road_data.keys())
    with open(os.path.join(OUTPUT_DIR, "road_ids.pkl"), 'wb') as f:
        pickle.dump(road_ids, f)
    
    # Print statistics
    print("\nPreprocessing complete!")
    print(f"Total roads processed: {len(road_data)}")
    print(f"Total segments created: {sum(r['total_segments'] for r in road_data.values())}")
    print(f"Average segments per road: {np.mean([r['total_segments'] for r in road_data.values()]):.1f}")
    print(f"\nOutput files:")
    print(f"  - {OUTPUT_DIR}/road_bounds.npy")
    print(f"  - {OUTPUT_DIR}/road_data.pkl")
    print(f"  - {OUTPUT_DIR}/buffer_polygons.pkl")
    print(f"  - {OUTPUT_DIR}/road_ids.pkl")

if __name__ == "__main__":
    preprocess_roads(INPUT_GEOJSON)
# %%