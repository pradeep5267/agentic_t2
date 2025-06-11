#!/usr/bin/env python3
"""
read_preprocessed_roads.py - Script to read and display preprocessed road data
"""

import pickle
import numpy as np
import os

# Configuration
PREPROCESSED_DIR = "/media/gamedisk/KTP_artefacts/pssavmk2_t2/preprocessed_roads"

def load_road_bounds():
    """Load road bounds numpy array"""
    bounds_path = os.path.join(PREPROCESSED_DIR, "road_bounds.npy")
    if os.path.exists(bounds_path):
        bounds = np.load(bounds_path)
        print(f"Road bounds shape: {bounds.shape}")
        print(f"Bounds format: (minx, miny, maxx, maxy)")
        print(f"First 5 bounds:")
        for i, bound in enumerate(bounds[:5]):
            print(f"  Road {i}: ({bound[0]:.6f}, {bound[1]:.6f}, {bound[2]:.6f}, {bound[3]:.6f})")
        return bounds
    else:
        print(f"File not found: {bounds_path}")
        return None

def load_road_data():
    """Load road data pickle file"""
    data_path = os.path.join(PREPROCESSED_DIR, "road_data.pkl")
    if os.path.exists(data_path):
        with open(data_path, 'rb') as f:
            road_data = pickle.load(f)
        
        print(f"\nTotal roads in data: {len(road_data)}")
        print("\nFirst 3 road entries:")
        for i, (road_id, data) in enumerate(list(road_data.items())[:3]):
            print(f"\nRoad {i+1} - ID: {road_id}")
            print(f"  Name: {data['name']}")
            print(f"  Status: {data['status']}")
            print(f"  Highway type: {data['highway']}")
            print(f"  Total segments: {data['total_segments']}")
            print(f"  Length (m): {data['length_m']:.1f}")
            print(f"  First 3 segments: {data['segments'][:3]}")
        
        return road_data
    else:
        print(f"File not found: {data_path}")
        return None

def load_buffer_polygons():
    """Load buffer polygons pickle file"""
    polygons_path = os.path.join(PREPROCESSED_DIR, "buffer_polygons.pkl")
    if os.path.exists(polygons_path):
        with open(polygons_path, 'rb') as f:
            polygons = pickle.load(f)
        
        print(f"\nTotal buffer polygons: {len(polygons)}")
        print(f"First polygon type: {type(polygons[0])}")
        print(f"First polygon bounds: {polygons[0].bounds}")
        return polygons
    else:
        print(f"File not found: {polygons_path}")
        return None

def load_road_ids():
    """Load road IDs pickle file"""
    ids_path = os.path.join(PREPROCESSED_DIR, "road_ids.pkl")
    if os.path.exists(ids_path):
        with open(ids_path, 'rb') as f:
            road_ids = pickle.load(f)
        
        print(f"\nTotal road IDs: {len(road_ids)}")
        print(f"First 10 road IDs: {road_ids[:10]}")
        return road_ids
    else:
        print(f"File not found: {ids_path}")
        return None

def display_file_info():
    """Display information about all preprocessed files"""
    print("=" * 60)
    print("PREPROCESSED ROADS DATA SUMMARY")
    print("=" * 60)
    
    files = [
        "road_bounds.npy",
        "road_data.pkl", 
        "buffer_polygons.pkl",
        "road_ids.pkl"
    ]
    
    print("\nFile sizes:")
    for filename in files:
        filepath = os.path.join(PREPROCESSED_DIR, filename)
        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            print(f"  {filename}: {size:,} bytes ({size/1024/1024:.2f} MB)")
        else:
            print(f"  {filename}: File not found")

def main():
    """Main function to read and display all preprocessed data"""
    display_file_info()
    
    print("\n" + "=" * 60)
    print("LOADING DATA FILES")
    print("=" * 60)
    
    # Load all data
    bounds = load_road_bounds()
    road_data = load_road_data()
    polygons = load_buffer_polygons()
    road_ids = load_road_ids()
    
    # Verify data consistency
    if all([bounds is not None, road_data is not None, polygons is not None, road_ids is not None]):
        print("\n" + "=" * 60)
        print("DATA CONSISTENCY CHECK")
        print("=" * 60)
        print(f"Bounds array length: {len(bounds)}")
        print(f"Road data entries: {len(road_data)}")
        print(f"Buffer polygons: {len(polygons)}")
        print(f"Road IDs: {len(road_ids)}")
        
        if len(bounds) == len(polygons) == len(road_ids) == len(road_data):
            print("✓ All data files are consistent in length")
        else:
            print("✗ Data files have inconsistent lengths")

if __name__ == "__main__":
    main()