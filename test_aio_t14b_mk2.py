#!/usr/bin/env python3
"""
test_aio_t14b_mk2.py - Test script for aio_t14b_mk2.py without hardware dependencies

This script tests the core functionality of the road coverage recorder system by:
1. Mocking GPS data
2. Mocking hardware interactions (camera, gstreamer)
3. Testing database operations
4. Testing road detection and coverage calculation
5. Testing CSV logging
6. Testing process management

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
from unittest.mock import patch, MagicMock, mock_open, ANY
from datetime import datetime
from io import StringIO

# Create mocks for the required modules
import unittest.mock as mock
serial = mock.MagicMock()
serial.SerialException = Exception  # Create a mock SerialException class
sys.modules['serial'] = serial

pynmea2 = mock.MagicMock()
sys.modules['pynmea2'] = pynmea2

requests = mock.MagicMock()
sys.modules['requests'] = requests

# Create a pickle-friendly Point class for shapely
class MockPoint:
    def __init__(self, x, y):
        self.x = x
        self.y = y

# Create a pickle-friendly Polygon class that matches the format from the parser
class MockPolygon:
    def __init__(self, id, coords):
        self.id = id
        self.coords = coords
        
        # Bounds extracted from coordinates
        if id == 0:  # Road 123
            self.bounds = (3.0, 51.0, 3.1, 51.1)
        else:  # Road 456
            self.bounds = (3.05, 51.05, 3.15, 51.15)
    
    def intersects(self, point):
        # Simple check for testing purposes
        return True
    
    def contains(self, point):
        # For testing, return True if the point is within the bounds
        if self.id == 0:
            return (3.0 <= point.x <= 3.1) and (51.0 <= point.y <= 51.1)
        else:
            return (3.05 <= point.x <= 3.15) and (51.05 <= point.y <= 51.15)

# Create a pickle-friendly PreparedGeometry class
class MockPreparedGeometry:
    def __init__(self, polygon):
        self.polygon = polygon
    
    def contains(self, point):
        # For testing, delegate to the polygon's contains method
        return self.polygon.contains(point)

# Mock shapely modules
sys.modules['shapely.geometry'] = MagicMock()
sys.modules['shapely.geometry.Point'] = MockPoint
sys.modules['shapely.prepared'] = MagicMock()
sys.modules['shapely.prepared.prep'] = lambda p: MockPreparedGeometry(p)

# Create test directory structure and files
def setup_test_environment():
    """Set up test environment with mock data files and directories"""
    # Create test directories
    os.makedirs("preprocessed_roads", exist_ok=True)
    os.makedirs("/tmp/road_coverage_recordings", exist_ok=True)
    
    # Create mock road_bounds.npy - these are bounding boxes for each road
    bounds_array = np.array([
        [3.0, 51.0, 3.1, 51.1],  # Road 123 bounding box
        [3.05, 51.05, 3.15, 51.15]  # Road 456 bounding box
    ])
    np.save("preprocessed_roads/road_bounds.npy", bounds_array)
    
    # Create mock road_data.pkl - this is the main road information structure
    # Based on the KML/OSM parser, each road has a name and segments (coordinates)
    road_data = {
        "123": {
            "name": "Test Road 1",
            "segments": [(3.05, 51.05), (3.06, 51.06), (3.07, 51.07)],
            "tags": {"highway": "residential", "status": "allowed"},
            "polygon": "Test Area 1"
        },
        "456": {
            "name": "Test Road 2",
            "segments": [(3.08, 51.08), (3.09, 51.09)],
            "tags": {"highway": "residential", "status": "allowed"},
            "polygon": "Test Area 2"
        }
    }
    with open("preprocessed_roads/road_data.pkl", "wb") as f:
        pickle.dump(road_data, f)
    
    # Create pickle-friendly polygon objects that match the format from shapely.geometry.Polygon
    # Each polygon should represent a road area boundary
    polygon1 = MockPolygon(0, [
        (3.0, 51.0), (3.1, 51.0), (3.1, 51.1), (3.0, 51.1), (3.0, 51.0)
    ])
    polygon2 = MockPolygon(1, [
        (3.05, 51.05), (3.15, 51.05), (3.15, 51.15), (3.05, 51.15), (3.05, 51.05)
    ])
    
    buffer_polygons = [polygon1, polygon2]
    with open("preprocessed_roads/buffer_polygons.pkl", "wb") as f:
        pickle.dump(buffer_polygons, f)
    
    # Create mock road_ids.pkl - mapping between polygon indices and road IDs
    road_ids = ["123", "456"]
    with open("preprocessed_roads/road_ids.pkl", "wb") as f:
        pickle.dump(road_ids, f)
    
    return road_data

# Clean up test environment
def cleanup_test_environment():
    """Clean up all test files and directories"""
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

# Test case for aio_t14b_mk2.py
class TestRoadCoverageRecorder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up test environment once for all tests"""
        cls.road_data = setup_test_environment()
        
        # Prepare patches
        cls.patches = [
            patch('subprocess.Popen', MockPopen),
            patch('subprocess.run', return_value=MagicMock(stdout="")),
            patch('os.getpgid', return_value=12345),
            patch('os.killpg', return_value=None),
            patch('os.setsid', return_value=None),
            # Other patches as needed
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
        
        cleanup_test_environment()
    
    def setUp(self):
        """Set up for each test"""
        # Use a temporary database
        self.db_fd, self.db_path = tempfile.mkstemp()
        
        # Create temporary CSV file
        self.csv_path = "/tmp/road_coverage_recordings/test_master_gps_log.csv"
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)
        
        # Patch the database and save paths
        self.patches = [
            patch('aio_t14b_mk2.DATABASE', self.db_path),
            patch('aio_t14b_mk2.SAVE_DIR', '/tmp/road_coverage_recordings'),
            patch('aio_t14b_mk2.CSV_FILE', self.csv_path),
            patch('aio_t14b_mk2.PREPROCESSED_DIR', 'preprocessed_roads')
        ]
        
        for p in self.patches:
            p.start()
        
        # Now import the module after patching
        import aio_t14b_mk2
        self.recorder = aio_t14b_mk2
        
        # Initialize database
        self.recorder.init_database()
    
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
    
    def test_init_database(self):
        """Test database initialization"""
        # Verify tables exist
        conn = sqlite3.connect(self.db_path)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        
        self.assertIn('road_recordings', table_names)
        self.assertIn('manual_marks', table_names)
        self.assertIn('covered_roads', table_names)
        self.assertIn('coverage_history', table_names)
        conn.close()
    
    def test_csv_logging(self):
        """Test CSV logging functionality"""
        # Initialize CSV
        self.recorder.init_csv()
        
        # Log some events
        self.recorder.log_csv('TEST_EVENT', lat=51.05, lon=3.05, notes='Test event')
        self.recorder.log_csv('ANOTHER_EVENT', lat=51.06, lon=3.06, notes='Another test')
        
        # Flush buffer
        self.recorder.flush_csv_buffer()
        
        # Verify CSV file exists and has content
        self.assertTrue(os.path.exists(self.csv_path))
        with open(self.csv_path, 'r') as f:
            content = f.read()
            self.assertIn('TEST_EVENT', content)
            self.assertIn('ANOTHER_EVENT', content)
    
    def test_find_current_road(self):
        """Test finding current road based on GPS coordinates"""
        # Test finding a road
        rid, info = self.recorder.find_current_road(3.05, 51.05)
        self.assertEqual(rid, "123")
        self.assertEqual(info['name'], "Test Road 1")
        
        # Test when not on a road
        rid, info = self.recorder.find_current_road(4.0, 52.0)
        self.assertIsNone(rid)
        self.assertIsNone(info)
    
    def test_find_nearest_segment(self):
        """Test finding nearest road segment"""
        # Test finding nearest segment
        seg_idx, seg_dist = self.recorder.find_nearest_segment("123", 51.051, 3.051)
        self.assertEqual(seg_idx, 0)  # First segment should be closest
        
        # Test another segment
        seg_idx, seg_dist = self.recorder.find_nearest_segment("123", 51.071, 3.071)
        self.assertEqual(seg_idx, 2)  # Third segment should be closest
    
    def test_calculate_coverage(self):
        """Test road coverage calculation"""
        # Set up coverage state
        self.recorder.road_coverage_state = {
            "123": {0, 1}  # 2 out of 3 segments covered
        }
        
        # Test coverage calculation
        coverage = self.recorder.calculate_coverage("123")
        self.assertAlmostEqual(coverage, 66.66666, places=2)
        
        # Test coverage for road not in state
        coverage = self.recorder.calculate_coverage("999")
        self.assertEqual(coverage, 0.0)
    
    def test_db_operations(self):
        """Test database operations"""
        # Save recording to database
        self.recorder.save_recording_to_db("123", "/tmp/test.mp4", 75.5)
        
        # Query to verify
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT feature_id, video_file, coverage_percent FROM road_recordings")
        row = cursor.fetchone()
        self.assertEqual(row[0], "123")
        self.assertEqual(row[1], "/tmp/test.mp4")
        self.assertAlmostEqual(row[2], 75.5)
        
        # Check covered_roads table
        cursor.execute("SELECT feature_id FROM covered_roads")
        row = cursor.fetchone()
        self.assertEqual(row[0], "123")
        conn.close()
    
    def test_recording_operations(self):
        """Test recording start/stop operations"""
        # Start recording
        with patch('os.path.join', return_value='/tmp/test.mp4'):
            recording_file = self.recorder.start_recording("123")
            self.assertIsNotNone(recording_file)
            self.assertIsNotNone(self.recorder.recording_proc)
            self.assertIsNotNone(self.recorder.recording_start_time)
        
        # Stop recording
        self.recorder.stop_recording()
        self.assertIsNone(self.recorder.recording_proc)
        self.assertIsNone(self.recorder.recording_file)
    
    def test_load_recorded_roads(self):
        """Test loading already recorded roads"""
        # Insert test data
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO road_recordings (feature_id, video_file) VALUES (?, ?)", 
                     ("123", "/tmp/test.mp4"))
        conn.execute("INSERT INTO manual_marks (feature_id, status) VALUES (?, ?)",
                     ("456", "complete"))
        conn.commit()
        conn.close()
        
        # Load recorded roads
        roads = self.recorder.load_recorded_roads()
        self.assertIn("123", roads)
        self.assertIn("456", roads)
        self.assertEqual(len(roads), 2)
    
    def test_gps_simulation(self):
        """Test GPS data processing with simulated data"""
        # Mock gps_queue and create simulated GPS data
        gps_data = {
            'lat': 51.05, 
            'lon': 3.05,
            'fix': True,
            'gps_qual': 1,
            'time': time.time()
        }
        
        # Manually set up state
        self.recorder.gps_data = {}
        self.recorder.gps_queue = queue.Queue()
        self.recorder.gps_queue.put(gps_data)
        
        # Call find_current_road with simulated GPS data
        rid, info = self.recorder.find_current_road(gps_data['lon'], gps_data['lat'])
        self.assertEqual(rid, "123")
        
        # Test segment finding
        seg_idx, seg_dist = self.recorder.find_nearest_segment(rid, gps_data['lat'], gps_data['lon'])
        self.assertEqual(seg_idx, 0)
        
        # Add segment to coverage state
        self.recorder.road_coverage_state = {}
        self.recorder.road_coverage_state.setdefault(rid, set()).add(seg_idx)
        
        # Test coverage calculation
        coverage = self.recorder.calculate_coverage(rid)
        self.assertAlmostEqual(coverage, 33.33333, places=2)  # 1 out of 3 segments

    def test_system_health(self):
        """Test system health monitoring"""
        # Mock system stats
        mock_stats = {
            'cpu_temp': 75.5,
            'gpu_temp': 65.2,
            'mem_percent': 45.3,
            'mem_used_mb': 2048,
            'mem_total_mb': 4096,
            'throttled': False,
            'cpu_freq_mhz': 1800,
            'storage_free_gb': 25.6,
            'storage_percent': 48.5
        }
        
        # Test with patched get_jetson_stats
        with patch('aio_t14b_mk2.get_jetson_stats', return_value=mock_stats):
            # Redirect logging output
            with patch('aio_t14b_mk2.log_csv') as mock_log:
                self.recorder.check_system_health()
                # Verify log_csv was called with SYSTEM_HEALTH
                # Use ANY instead of Python's any function
                mock_log.assert_any_call('SYSTEM_HEALTH', notes=ANY)
    
    def test_gps_thread_fallback(self):
        """Test GPS thread with fallback port logic"""
        # Create a SerialException that can be referenced
        serial_exception = Exception("Port error")
        
        # Mock the serial module to simulate port errors
        mock_serial = MagicMock()
        mock_serial.side_effect = [
            serial_exception,  # First port fails
            MagicMock()  # Second port succeeds
        ]
        
        with patch('serial.Serial', mock_serial):
            with patch('aio_t14b_mk2.shutdown_event') as mock_shutdown:
                # Make shutdown_event.is_set() return True after first iteration
                mock_shutdown.is_set.side_effect = [False, True]
                
                # Run the GPS thread
                with patch('aio_t14b_mk2.log_csv') as mock_log:
                    self.recorder.gps_thread()
                    
                    # Verify log_csv was called with GPS_PORT_ERROR and GPS_RETRY
                    mock_log.assert_any_call('GPS_PORT_TRYING', thread_state='GPS', notes=ANY)
                    mock_log.assert_any_call('GPS_PORT_ERROR', thread_state='GPS', notes=ANY)
                    mock_log.assert_any_call('GPS_RETRY', thread_state='GPS', notes=ANY)

def mock_main():
    """Mock main function for testing"""
    print("Running mock main function for testing")
    print("This would normally start the road coverage recorder system")
    print("All tests have been completed successfully!")
    return 0

# Add a mock main function to the recorder module
def add_mock_main():
    import aio_t14b_mk2
    aio_t14b_mk2.main = mock_main

if __name__ == "__main__":
    print("=== Road Coverage Recorder Test Suite ===")
    print("Testing without hardware dependencies...")
    print("Setting up test environment...")
    
    # Run the tests
    test_suite = unittest.TestLoader().loadTestsFromTestCase(TestRoadCoverageRecorder)
    test_result = unittest.TextTestRunner(verbosity=2).run(test_suite)
    
    # If all tests passed, run the mock main function
    if test_result.wasSuccessful():
        print("\nAll tests passed! Running mock main function...")
        add_mock_main()
        import aio_t14b_mk2
        aio_t14b_mk2.main()
    else:
        print("\nSome tests failed. Please check the output above.")
    
    print("\nTest completed. Cleaning up...")
    cleanup_test_environment()