#!/usr/bin/env python3
"""
test_integration.py - Integration tests for aio_t14b_mk2.py

This script tests the complete integration of the road coverage recorder system with 
realistic data formats based on the actual preprocessing pipeline.

Features tested:
1. Simulated GPS track following real road segments
2. Road detection and coverage calculation with shapely geometries
3. Database integration with all required tables
4. CSV logging with realistic data
5. Process management with clean shutdown

Run this script in the same directory as aio_t14b_mk2.py
"""

import os
import sys
import time
import tempfile
import unittest
import sqlite3
import queue
import threading
import pickle
import numpy as np
import shutil
import json
from unittest.mock import patch, MagicMock, mock_open, call
from datetime import datetime, timedelta
from io import StringIO
import signal

# Mock required modules
sys.modules['serial'] = MagicMock()
sys.modules['pynmea2'] = MagicMock()
sys.modules['requests'] = MagicMock()

# Create a Point class that matches the shapely Point behavior
class MockPoint:
    def __init__(self, x, y):
        self.x = x
        self.y = y

# Create a Polygon class that matches the shapely Polygon behavior
class MockPolygon:
    def __init__(self, id, coords):
        self.id = id
        self.coords = coords
        
        # Extract bounds from coordinates
        lons = [p[0] for p in coords]
        lats = [p[1] for p in coords]
        self.bounds = (min(lons), min(lats), max(lons), max(lats))
    
    def contains(self, point):
        # Simple check if point is inside the bounding box
        # Note: In a real application, you'd implement a proper point-in-polygon algorithm
        minx, miny, maxx, maxy = self.bounds
        return (minx <= point.x <= maxx) and (miny <= point.y <= maxy)

# Create a PreparedGeometry class that matches the shapely behavior
class MockPreparedGeometry:
    def __init__(self, polygon):
        self.polygon = polygon
    
    def contains(self, point):
        return self.polygon.contains(point)

# Mock shapely modules
sys.modules['shapely.geometry'] = MagicMock()
sys.modules['shapely.geometry.Point'] = MockPoint
sys.modules['shapely.prepared'] = MagicMock()
sys.modules['shapely.prepared.prep'] = lambda p: MockPreparedGeometry(p)

# # Generate test roads based on the real format from the KML/OSM parser
# def create_test_roads():
#     """Create realistic test road data based on the KML/OSM parser output format"""
    
#     # Road 1: A simple straight road with multiple segments
#     road1_id = "123"
#     road1_name = "Main Street"
#     road1_segments = [
#         (3.000, 51.000),
#         (3.001, 51.001),
#         (3.002, 51.002),
#         (3.003, 51.003),
#         (3.004, 51.004),
#         (3.005, 51.005),
#     ]
#     road1_bounds = [3.000, 51.000, 3.005, 51.005]
#     # --- FIX: Changed the southern boundary of the polygon from 50.999 to 51.002 ---
#     # This prevents it from completely containing the polygon for road "789".
#     road1_polygon = MockPolygon(0, [
#         (2.999, 51.002), # <-- Was 50.999
#         (3.006, 51.002), # <-- Was 50.999
#         (3.006, 51.006),
#         (2.999, 51.006),
#         (2.999, 51.002)  # <-- Was 50.999
#     ])
    
#     # Road 2: A curved road with multiple segments
#     road2_id = "456"
#     road2_name = "Broadway Avenue"
#     road2_segments = [
#         (3.010, 51.010),
#         (3.012, 51.011),
#         (3.014, 51.013),
#         (3.016, 51.016),
#         (3.017, 51.020),
#         (3.016, 51.024),
#     ]
#     road2_bounds = [3.010, 51.010, 3.017, 51.024]
#     road2_polygon = MockPolygon(1, [
#         (3.009, 51.009),
#         (3.018, 51.009),
#         (3.018, 51.025),
#         (3.009, 51.025),
#         (3.009, 51.009)
#     ])
    
#     # Road 3: An intersection road
#     road3_id = "789"
#     road3_name = "Park Road"
#     road3_segments = [
#         (3.004, 51.004),  # Intersection with Road 1
#         (3.004, 51.003),
#         (3.004, 51.002),
#         (3.004, 51.001),
#         (3.004, 51.000),
#     ]
#     road3_bounds = [3.003, 51.000, 3.005, 51.004]  # Give it some width
#     road3_polygon = MockPolygon(2, [
#         (3.003, 50.999),
#         (3.005, 50.999),
#         (3.005, 51.005),
#         (3.003, 51.005),
#         (3.003, 50.999)
#     ])
    
#     # Combine road data in the format expected by the recorder
#     road_data = {
#         road1_id: {
#             "name": road1_name,
#             "segments": road1_segments,
#             "tags": {"highway": "residential", "status": "allowed"},
#             "polygon": "Test Area 1"
#         },
#         road2_id: {
#             "name": road2_name,
#             "segments": road2_segments,
#             "tags": {"highway": "residential", "status": "allowed"},
#             "polygon": "Test Area 2"
#         },
#         road3_id: {
#             "name": road3_name,
#             "segments": road3_segments,
#             "tags": {"highway": "residential", "status": "allowed"},
#             "polygon": "Test Area 3"
#         }
#     }
    
#     # Create bounds array
#     bounds_array = np.array([
#         road1_bounds,
#         road2_bounds,
#         road3_bounds
#     ])
    
#     # Create buffer polygons
#     buffer_polygons = [road1_polygon, road2_polygon, road3_polygon]
    
#     # Create road IDs
#     road_ids = [road1_id, road2_id, road3_id]
    
#     return road_data, bounds_array, buffer_polygons, road_ids

# Generate test roads based on the real format from the KML/OSM parser
def create_test_roads():
    """Create realistic test road data based on the KML/OSM parser output format"""
    
    # Road 1: A simple straight road with multiple segments
    road1_id = "123"
    road1_name = "Main Street"
    road1_segments = [
        (3.000, 51.000), (3.001, 51.001), (3.002, 51.002),
        (3.003, 51.003), (3.004, 51.004), (3.005, 51.005),
    ]
    # This polygon is intentionally smaller to not contain Road 3
    road1_polygon = MockPolygon(0, [
        (2.999, 51.002), (3.006, 51.002),
        (3.006, 51.006), (2.999, 51.006),
        (2.999, 51.002)
    ])
    
    # Road 2: A curved road with multiple segments
    road2_id = "456"
    road2_name = "Broadway Avenue"
    road2_segments = [
        (3.010, 51.010), (3.012, 51.011), (3.014, 51.013),
        (3.016, 51.016), (3.017, 51.020), (3.016, 51.024),
    ]
    road2_polygon = MockPolygon(1, [
        (3.009, 51.009), (3.018, 51.009),
        (3.018, 51.025), (3.009, 51.025),
        (3.009, 51.009)
    ])
    
    # Road 3: An intersection road
    road3_id = "789"
    road3_name = "Park Road"
    road3_segments = [
        (3.004, 51.004), (3.004, 51.003), (3.004, 51.002),
        (3.004, 51.001), (3.004, 51.000),
    ]
    road3_polygon = MockPolygon(2, [
        (3.003, 50.999), (3.005, 50.999),
        (3.005, 51.005), (3.003, 51.005),
        (3.003, 50.999)
    ])
    
    # Combine road data in the format expected by the recorder
    road_data = {
        road1_id: {"name": road1_name, "segments": road1_segments, "tags": {"highway": "residential"}, "polygon": "Test Area 1"},
        road2_id: {"name": road2_name, "segments": road2_segments, "tags": {"highway": "residential"}, "polygon": "Test Area 2"},
        road3_id: {"name": road3_name, "segments": road3_segments, "tags": {"highway": "residential"}, "polygon": "Test Area 3"}
    }
    
    # Create buffer polygons list
    buffer_polygons = [road1_polygon, road2_polygon, road3_polygon]
    
    # --- FIX: Derive the bounds_array directly from the mock polygons' .bounds attribute ---
    # This removes the hardcoded, inconsistent bounds data.
    bounds_array = np.array([p.bounds for p in buffer_polygons])
    
    # Create road IDs
    road_ids = [road1_id, road2_id, road3_id]
    
    return road_data, bounds_array, buffer_polygons, road_ids

# Load actual road data from existing files
def load_actual_road_data():
    """Load actual road data from existing preprocessed files"""
    
    try:
        # Load actual preprocessed road data
        bounds_array = np.load("preprocessed_roads/road_bounds.npy")
        
        with open("preprocessed_roads/road_data.pkl", "rb") as f:
            road_data = pickle.load(f)
        
        with open("preprocessed_roads/buffer_polygons.pkl", "rb") as f:
            buffer_polygons = pickle.load(f)
        
        with open("preprocessed_roads/road_ids.pkl", "rb") as f:
            road_ids = pickle.load(f)
        
        print(f"Loaded {len(road_data)} actual roads for testing")
        return road_data, bounds_array, buffer_polygons, road_ids
        
    except Exception as e:
        print(f"Warning: Could not load actual road data: {e}")
        return None, None, None, None

# Create test directory structure and files with realistic data
def setup_integration_test_environment():
    """Set up test environment with realistic road data files"""
    
    # Create test directories
    os.makedirs("preprocessed_roads", exist_ok=True)
    os.makedirs("/tmp/road_coverage_recordings", exist_ok=True)
    
    # Generate test roads
    road_data, bounds_array, buffer_polygons, road_ids = create_test_roads()
    
    # Save road data to files
    np.save("preprocessed_roads/road_bounds.npy", bounds_array)
    
    with open("preprocessed_roads/road_data.pkl", "wb") as f:
        pickle.dump(road_data, f)
    
    with open("preprocessed_roads/buffer_polygons.pkl", "wb") as f:
        pickle.dump(buffer_polygons, f)
    
    with open("preprocessed_roads/road_ids.pkl", "wb") as f:
        pickle.dump(road_ids, f)
    
    return road_data, bounds_array, buffer_polygons, road_ids

# Clean up test environment
def cleanup_integration_test_environment():
    """Clean up test files and directories"""
    # Don't delete preprocessed_roads - it's real data
    pass
    
    try:
        if os.path.exists("/tmp/road_coverage_recordings"):
            for file in os.listdir("/tmp/road_coverage_recordings"):
                os.remove(os.path.join("/tmp/road_coverage_recordings", file))
    except:
        pass

# Mock subprocess for testing
class MockPopen:
    def __init__(self, *args, **kwargs):
        self.pid = 12345
        self.returncode = None
        self.args = args
        self.kwargs = kwargs
    
    def poll(self):
        return None
    
    def wait(self, timeout=None):
        self.returncode = 0
        return 0

# Generate a realistic GPS track that follows a road
def generate_gps_track(road_segments, noise=0.00001):
    """Generate a realistic GPS track that follows road segments with small noise"""
    import random
    
    track = []
    for segment in road_segments:
        # Add some small random noise to simulate GPS inaccuracy
        lat = segment[1] + random.uniform(-noise, noise)
        lon = segment[0] + random.uniform(-noise, noise)
        
        # Create a GPS data point
        point = {
            'lat': lat,
            'lon': lon,
            'fix': True,
            'gps_qual': 1,
            'time': time.time()
        }
        track.append(point)
    
    return track

# Test case for integration testing
class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up test environment once for all tests"""
        cls.road_data, cls.bounds_array, cls.buffer_polygons, cls.road_ids = setup_integration_test_environment()
        
        # Prepare patches
        cls.patches = [
            patch('subprocess.Popen', MockPopen),
            patch('subprocess.run', return_value=MagicMock(stdout="")),
            patch('os.getpgid', return_value=12345),
            patch('os.killpg', return_value=None),
            patch('os.setsid', return_value=None),
            patch('time.sleep', return_value=None),
        ]
        
        # Start all patches
        for p in cls.patches:
            p.start()
    
    @classmethod
    def tearDownClass(cls):
        """Clean up test environment after all tests"""
        # Stop all patches
        for p in cls.patches:
            p.stop()
        
        cleanup_integration_test_environment()
    
    def setUp(self):
        """Set up for each test"""
        # Use a temporary database and CSV file
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.csv_path = "/tmp/road_coverage_recordings/test_integration_log.csv"
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)
        
        # Patch paths and config
        self.patches = [
            patch('aio_t14b_mk2.DATABASE', self.db_path),
            patch('aio_t14b_mk2.SAVE_DIR', '/tmp/road_coverage_recordings'),
            patch('aio_t14b_mk2.CSV_FILE', self.csv_path),
            patch('aio_t14b_mk2.SEGMENT_THRESHOLD_M', 50),  # Larger threshold for testing
            patch('aio_t14b_mk2.ROAD_EXIT_THRESHOLD_S', 1)  # Shorter threshold for testing
        ]
        
        for p in self.patches:
            p.start()
        
        # Import the module after patching
        import aio_t14b_mk2
        self.recorder = aio_t14b_mk2
        
        # Initialize test components
        self.recorder.init_database()
        self.recorder.init_csv()
        
        # Reset global state
        self.recorder.gps_queue = queue.Queue()
        self.recorder.gps_data = {}
        self.recorder.road_coverage_state = {}
        self.recorder.current_road_id = None
        self.recorder.recording_proc = None
        self.recorder.recording_file = None
        self.recorder.recording_start_time = None
        self.recorder.last_recording_stop = 0
        self.recorder.shutdown_event = threading.Event()
    
    def tearDown(self):
        """Clean up after each test"""
        # Close database
        os.close(self.db_fd)
        os.unlink(self.db_path)
        
        # Remove CSV file
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)
        
        # Stop patches
        for p in self.patches:
            p.stop()
    
    def test_realistic_road_tracking(self):
        """Test tracking a realistic GPS track along a road"""
        # Generate a GPS track following Road 1
        road1_track = generate_gps_track(self.road_data["123"]["segments"])
        
        # Mock recording functions
        mock_start_recording = MagicMock(return_value="/tmp/test_recording.mp4")
        mock_stop_recording = MagicMock()
        
        with patch('aio_t14b_mk2.start_recording', mock_start_recording):
            with patch('aio_t14b_mk2.stop_recording', mock_stop_recording):
                with patch('aio_t14b_mk2.post_state'):
                    # Process each GPS point in the track
                    for gps in road1_track:
                        self.recorder.gps_queue.put(gps)
                        
                        # Simulate one iteration of the main loop
                        try:
                            gps_data = self.recorder.gps_queue.get(timeout=0.1)
                            rid, info = self.recorder.find_current_road(gps_data['lon'], gps_data['lat'])
                            
                            if rid:
                                seg_idx, seg_dist = self.recorder.find_nearest_segment(rid, gps_data['lat'], gps_data['lon'])
                                if seg_dist <= self.recorder.SEGMENT_THRESHOLD_M:
                                    self.recorder.road_coverage_state.setdefault(rid, set()).add(seg_idx)
                                
                                # Handle road entry
                                if rid != self.recorder.current_road_id:
                                    if self.recorder.recording_proc:
                                        self.recorder.stop_recording()
                                    
                                    with patch('aio_t14b_mk2.log_csv'):
                                        if rid not in self.recorder.recorded_roads:
                                            self.recorder.start_recording(rid)
                                    
                                    self.recorder.current_road_id = rid
                                    self.recorder.last_on_road = time.time()
                                    self.recorder.exit_logged = False
                            
                            # Handle road exit logic would go here...
                            
                        except queue.Empty:
                            pass
                
                # Verify we detected road "123"
                self.assertEqual(self.recorder.current_road_id, "123")
                
                # Verify segments were recorded correctly
                self.assertIn("123", self.recorder.road_coverage_state)
                
                # We should have recorded most segments in the road
                coverage = self.recorder.calculate_coverage("123")
                self.assertGreater(coverage, 50.0)  # At least 50% coverage
    
    def test_road_transition(self):
        """Test transitioning between roads"""
        # Create a GPS track that transitions from Road 1 to Road 3 (they intersect)
        road1_segments = self.road_data["123"]["segments"]
        road3_segments = self.road_data["789"]["segments"]
        
        # Find the intersection point - Road 3 starts at an intersection with Road 1
        intersection_point = road3_segments[0]
        
        # Create a track that approaches the intersection on Road 1, then follows Road 3
        combined_track = generate_gps_track(road1_segments[:4])  # First part of Road 1
        combined_track += generate_gps_track(road3_segments)     # All of Road 3
        
        # Mock recording functions
        mock_start_recording = MagicMock(return_value="/tmp/test_recording.mp4")
        mock_stop_recording = MagicMock()
        
        with patch('aio_t14b_mk2.start_recording', mock_start_recording):
            with patch('aio_t14b_mk2.stop_recording', mock_stop_recording):
                with patch('aio_t14b_mk2.post_state'):
                    with patch('aio_t14b_mk2.log_csv'):
                        # Process each GPS point in the track
                        for gps in combined_track:
                            self.recorder.gps_queue.put(gps)
                            
                            # Simulate one iteration of the main loop
                            try:
                                gps_data = self.recorder.gps_queue.get(timeout=0.1)
                                rid, info = self.recorder.find_current_road(gps_data['lon'], gps_data['lat'])
                                
                                if rid:
                                    seg_idx, seg_dist = self.recorder.find_nearest_segment(rid, gps_data['lat'], gps_data['lon'])
                                    if seg_dist <= self.recorder.SEGMENT_THRESHOLD_M:
                                        self.recorder.road_coverage_state.setdefault(rid, set()).add(seg_idx)
                                    
                                    # Handle road entry
                                    if rid != self.recorder.current_road_id:
                                        if self.recorder.recording_proc:
                                            self.recorder.stop_recording()
                                        
                                        if rid not in self.recorder.recorded_roads:
                                            self.recorder.start_recording(rid)
                                        
                                        self.recorder.current_road_id = rid
                                        self.recorder.last_on_road = time.time()
                                        self.recorder.exit_logged = False
                                
                                # No road exit logic for this test
                                
                            except queue.Empty:
                                pass
                
                # Debug: Print what roads were detected
                print(f"Detected roads: {list(self.recorder.road_coverage_state.keys())}")
                print(f"Current road ID: {self.recorder.current_road_id}")
                
                # Verify we detected both roads
                self.assertIn("123", self.recorder.road_coverage_state)
                self.assertIn("789", self.recorder.road_coverage_state)
                
                # Final road should be "789"
                self.assertEqual(self.recorder.current_road_id, "789")
                
                # We should have recorded segments in both roads
                coverage1 = self.recorder.calculate_coverage("123")
                coverage3 = self.recorder.calculate_coverage("789")
                self.assertGreater(coverage1, 0.0)
                self.assertGreater(coverage3, 0.0)
    
    def test_signal_handler(self):
        """Test signal handler emergency backup functionality"""
        # Create a realistic CSV file with some data
        self.recorder.init_csv()
        self.recorder.log_csv('TEST_EVENT', lat=51.05, lon=3.05, notes='Test event')
        self.recorder.log_csv('ANOTHER_EVENT', lat=51.06, lon=3.06, notes='Another test')
        self.recorder.flush_csv_buffer()
        
        # Verify CSV file exists
        self.assertTrue(os.path.exists(self.csv_path))
        
        # Mock copy2 to verify emergency backup
        with patch('shutil.copy2') as mock_copy:
            # Call signal handler
            self.recorder.signal_handler(signal.SIGINT, None)
            
            # Verify emergency backup was created
            mock_copy.assert_called_once()
            # The first arg should be the source CSV file
            self.assertEqual(mock_copy.call_args[0][0], self.csv_path)
            # The second arg should be the emergency backup file
            self.assertTrue('emergency_save_' in mock_copy.call_args[0][1])
        
        # Verify shutdown event was set
        self.assertTrue(self.recorder.shutdown_event.is_set())
    
    def test_database_operations(self):
        """Test database operations with realistic road data"""
        # Add test data to the database
        conn = sqlite3.connect(self.db_path)
        
        # Add a road recording
        conn.execute("""
            INSERT INTO road_recordings 
            (feature_id, video_file, started_at, coverage_percent)
            VALUES (?, ?, ?, ?)
        """, ("123", "/tmp/test_road_123.mp4", datetime.now().isoformat(), 75.5))
        
        # Add a manual mark
        conn.execute("""
            INSERT INTO manual_marks
            (feature_id, status, marked_at)
            VALUES (?, ?, ?)
        """, ("456", "complete", datetime.now().isoformat()))
        
        # Add a covered road
        conn.execute("""
            INSERT INTO covered_roads
            (feature_id)
            VALUES (?)
        """, ("789",))
        
        # Add coverage history
        conn.execute("""
            INSERT INTO coverage_history
            (feature_id, covered_at, latitude, longitude, accuracy)
            VALUES (?, ?, ?, ?, ?)
        """, ("789", datetime.now().isoformat(), 51.05, 3.05, 2.5))
        
        conn.commit()
        conn.close()
        
        # Load recorded roads
        recorded_roads = self.recorder.load_recorded_roads()
        
        # Verify all roads were loaded
        self.assertIn("123", recorded_roads)  # From road_recordings
        self.assertIn("456", recorded_roads)  # From manual_marks
        self.assertEqual(len(recorded_roads), 2)  # Both unique roads
        
        # Save a new recording to the database
        self.recorder.save_recording_to_db("789", "/tmp/test_road_789.mp4", 90.0)
        
        # Verify the new recording was saved
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT feature_id, video_file, coverage_percent FROM road_recordings WHERE feature_id = ?", ("789",))
        row = cursor.fetchone()
        self.assertEqual(row[0], "789")
        self.assertEqual(row[1], "/tmp/test_road_789.mp4")
        self.assertEqual(row[2], 90.0)
        
        # Verify the covered_roads table was updated
        cursor.execute("SELECT feature_id FROM covered_roads WHERE feature_id = ?", ("789",))
        row = cursor.fetchone()
        self.assertEqual(row[0], "789")
        
        conn.close()


class TestIntegrationWithActualData(unittest.TestCase):
    """Integration tests using actual road data from preprocessed files"""
    
    @classmethod
    def setUpClass(cls):
        """Set up test environment once for all tests"""
        # Try to load actual road data
        actual_data = load_actual_road_data()
        if actual_data[0] is None:
            raise unittest.SkipTest("Actual road data not available, skipping actual data tests")
        
        cls.road_data, cls.bounds_array, cls.buffer_polygons, cls.road_ids = actual_data
        
        # Prepare patches
        cls.patches = [
            patch('subprocess.Popen', MockPopen),
            patch('subprocess.run', return_value=MagicMock(stdout="")),
            patch('os.getpgid', return_value=12345),
            patch('os.killpg', return_value=None),
            patch('os.setsid', return_value=None),
            patch('time.sleep', return_value=None),
        ]
        
        # Start all patches
        for p in cls.patches:
            p.start()
    
    @classmethod
    def tearDownClass(cls):
        """Clean up test environment after all tests"""
        # Stop all patches
        for p in cls.patches:
            p.stop()
    
    def setUp(self):
        """Set up for each test"""
        # Use a temporary database and CSV file
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.csv_path = "/tmp/road_coverage_recordings/test_actual_data_log.csv"
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)
        
        # Patch paths and config
        self.patches = [
            patch('aio_t14b_mk2.DATABASE', self.db_path),
            patch('aio_t14b_mk2.SAVE_DIR', '/tmp/road_coverage_recordings'),
            patch('aio_t14b_mk2.CSV_FILE', self.csv_path),
            patch('aio_t14b_mk2.SEGMENT_THRESHOLD_M', 50),
            patch('aio_t14b_mk2.ROAD_EXIT_THRESHOLD_S', 1)
        ]
        
        for p in self.patches:
            p.start()
        
        # Import the module after patching
        import aio_t14b_mk2
        self.recorder = aio_t14b_mk2
        
        # Initialize test components
        self.recorder.init_database()
        self.recorder.init_csv()
        
        # Reset global state
        self.recorder.gps_queue = queue.Queue()
        self.recorder.gps_data = {}
        self.recorder.road_coverage_state = {}
        self.recorder.current_road_id = None
        self.recorder.recording_proc = None
        self.recorder.recording_file = None
        self.recorder.recording_start_time = None
        self.recorder.last_recording_stop = 0
        self.recorder.shutdown_event = threading.Event()
    
    def tearDown(self):
        """Clean up after each test"""
        # Close database
        os.close(self.db_fd)
        os.unlink(self.db_path)
        
        # Remove CSV file
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)
        
        # Stop patches
        for p in self.patches:
            p.stop()
    
    def test_actual_road_detection(self):
        """Test road detection with actual road data"""
        # Select a few actual roads for testing
        test_roads = self.road_ids[:5]  # Use first 5 roads
        
        for road_id in test_roads:
            road_info = self.road_data[road_id]
            if not road_info['segments']:
                continue
            
            # Test with a point from the road's segments
            lon, lat = road_info['segments'][0]
            
            # Mock recording functions
            mock_start_recording = MagicMock(return_value="/tmp/test_recording.mp4")
            mock_stop_recording = MagicMock()
            
            with patch('aio_t14b_mk2.start_recording', mock_start_recording):
                with patch('aio_t14b_mk2.stop_recording', mock_stop_recording):
                    with patch('aio_t14b_mk2.post_state'):
                        # Create GPS data point
                        gps_data = {
                            'lat': lat,
                            'lon': lon,
                            'fix': True,
                            'gps_qual': 1,
                            'time': time.time()
                        }
                        
                        self.recorder.gps_queue.put(gps_data)
                        
                        # Process the GPS point
                        try:
                            gps = self.recorder.gps_queue.get(timeout=0.1)
                            rid, info = self.recorder.find_current_road(gps['lon'], gps['lat'])
                            
                            if rid:
                                seg_idx, seg_dist = self.recorder.find_nearest_segment(rid, gps['lat'], gps['lon'])
                                if seg_dist <= self.recorder.SEGMENT_THRESHOLD_M:
                                    self.recorder.road_coverage_state.setdefault(rid, set()).add(seg_idx)
                                
                                # Handle road entry
                                if rid != self.recorder.current_road_id:
                                    with patch('aio_t14b_mk2.log_csv'):
                                        if rid not in self.recorder.recorded_roads:
                                            self.recorder.start_recording(rid)
                                    
                                    self.recorder.current_road_id = rid
                                    self.recorder.last_on_road = time.time()
                                    self.recorder.exit_logged = False
                            
                        except queue.Empty:
                            pass
            
            # If we detected a road, verify it makes sense
            if self.recorder.road_coverage_state:
                detected_roads = list(self.recorder.road_coverage_state.keys())
                print(f"Detected roads for {road_id}: {detected_roads}")
                
                # At least one road should be detected
                self.assertGreater(len(detected_roads), 0, "Should detect at least one road")
                
                # Calculate coverage for detected roads
                for detected_road in detected_roads:
                    coverage = self.recorder.calculate_coverage(detected_road)
                    self.assertGreaterEqual(coverage, 0, f"Coverage should be non-negative for {detected_road}")
                    print(f"Coverage for {detected_road}: {coverage:.1f}%")
            
            # Reset for next test
            self.recorder.road_coverage_state = {}
            self.recorder.current_road_id = None
    
    def test_actual_road_segments_coverage(self):
        """Test segment coverage calculation with actual roads"""
        # Pick a road with multiple segments
        test_road = None
        for road_id in self.road_ids:
            if len(self.road_data[road_id]['segments']) >= 3:
                test_road = road_id
                break
        
        if not test_road:
            self.skipTest("No road with multiple segments found")
        
        road_info = self.road_data[test_road]
        segments = road_info['segments']
        
        # Simulate GPS points along the road
        mock_start_recording = MagicMock(return_value="/tmp/test_recording.mp4")
        mock_stop_recording = MagicMock()
        
        with patch('aio_t14b_mk2.start_recording', mock_start_recording):
            with patch('aio_t14b_mk2.stop_recording', mock_stop_recording):
                with patch('aio_t14b_mk2.post_state'):
                    with patch('aio_t14b_mk2.log_csv'):
                        
                        for i, (lon, lat) in enumerate(segments):
                            gps_data = {
                                'lat': lat,
                                'lon': lon,
                                'fix': True,
                                'gps_qual': 1,
                                'time': time.time() + i
                            }
                            
                            self.recorder.gps_queue.put(gps_data)
                            
                            # Process the GPS point
                            try:
                                gps = self.recorder.gps_queue.get(timeout=0.1)
                                rid, info = self.recorder.find_current_road(gps['lon'], gps['lat'])
                                
                                if rid:
                                    seg_idx, seg_dist = self.recorder.find_nearest_segment(rid, gps['lat'], gps['lon'])
                                    if seg_dist <= self.recorder.SEGMENT_THRESHOLD_M:
                                        self.recorder.road_coverage_state.setdefault(rid, set()).add(seg_idx)
                                
                            except queue.Empty:
                                pass
        
        # Verify coverage
        if test_road in self.recorder.road_coverage_state:
            coverage = self.recorder.calculate_coverage(test_road)
            covered_segments = len(self.recorder.road_coverage_state[test_road])
            total_segments = len(segments)
            
            print(f"Road {test_road}: {covered_segments}/{total_segments} segments covered = {coverage:.1f}%")
            
            # Should have covered some segments
            self.assertGreater(covered_segments, 0, "Should cover at least one segment")
            self.assertGreater(coverage, 0, "Coverage percentage should be greater than 0")
            
            # Coverage should not exceed 100%
            self.assertLessEqual(coverage, 100, "Coverage should not exceed 100%")
    
    def test_actual_road_database_operations(self):
        """Test database operations with actual road IDs"""
        # Use actual road IDs for database operations
        test_roads = self.road_ids[:3]  # Use first 3 roads
        
        conn = sqlite3.connect(self.db_path)
        
        # Add recordings for actual roads
        for i, road_id in enumerate(test_roads):
            conn.execute("""
                INSERT INTO road_recordings 
                (feature_id, video_file, started_at, coverage_percent)
                VALUES (?, ?, ?, ?)
            """, (road_id, f"/tmp/test_road_{road_id}.mp4", datetime.now().isoformat(), 75.5 + i))
        
        conn.commit()
        conn.close()
        
        # Load recorded roads
        recorded_roads = self.recorder.load_recorded_roads()
        
        # Verify all test roads were loaded
        for road_id in test_roads:
            self.assertIn(road_id, recorded_roads, f"Road {road_id} should be in recorded roads")
        
        print(f"Successfully loaded {len(recorded_roads)} recorded roads including actual road IDs")


if __name__ == "__main__":
    print("=== Road Coverage Recorder Integration Tests ===")
    print("Testing with realistic data formats...")
    print("Setting up test environment...")
    
    unittest.main(verbosity=2)