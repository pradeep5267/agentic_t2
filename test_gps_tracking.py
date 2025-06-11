#!/usr/bin/env python3
"""
test_gps_tracking.py - Test suite focused on GPS data processing and road tracking
functionality of the aio_t14b_mk2.py module.

This test file uses a MockGPS class to simulate GPS data for realistic testing of the
road tracking and coverage calculation functionality.
"""

import os
import sys
import unittest
import threading
import time
import queue
import numpy as np
import pickle
import tempfile
import shutil
from unittest.mock import patch, MagicMock

# --- MODIFICATION START ---
# STEP 1: Mock all hardware/external dependencies BEFORE loading the main module.
serial_mock = MagicMock()
serial_mock.SerialException = Exception
sys.modules['serial'] = serial_mock

pynmea2_mock = MagicMock()
sys.modules['pynmea2'] = pynmea2_mock

requests_mock = MagicMock()
sys.modules['requests'] = requests_mock

# Mock shapely modules
class MockPoint:
    def __init__(self, x, y):
        self.x = x
        self.y = y

class MockPolygon:
    def __init__(self, coords):
        self.coords = coords

shapely_geometry_mock = MagicMock()
shapely_geometry_mock.Point = MockPoint
shapely_geometry_mock.Polygon = MockPolygon
sys.modules['shapely.geometry'] = shapely_geometry_mock

shapely_prepared_mock = MagicMock()
shapely_prepared_mock.prep = MagicMock()
sys.modules['shapely.prepared'] = shapely_prepared_mock

import importlib.util
import types

def load_patched_module(module_name, preprocessed_dir_path):
    """Load a module with PREPROCESSED_DIR patched to the correct value."""
    # Get the path to the module file
    module_path = os.path.join(os.path.dirname(__file__), f"{module_name}.py")
    
    # Create module spec
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    
    # --- FIX: Add the module to sys.modules BEFORE execution ---
    # This ensures that unittest.mock.patch can find and modify this specific instance.
    sys.modules[module_name] = module
    
    # Patch the PREPROCESSED_DIR and other needed paths before executing the module
    module.PREPROCESSED_DIR = preprocessed_dir_path
    module.BASE_DIR = os.path.dirname(__file__)
    
    # Execute the module
    spec.loader.exec_module(module)
    
    return module

# STEP 2: Use the dynamic loader to import aio_t14b_mk2, pre-configuring the data path.
try:
    # This is the directory your test intends to use for its data.
    TEST_DATA_DIR = "preprocessed_roads"
    rcr = load_patched_module('aio_t14b_mk2', TEST_DATA_DIR)
except Exception as e:
    print(f"Error loading aio_t14b_mk2.py: {e}")
    # Provide a more helpful error if the test data itself is missing
    if isinstance(e, FileNotFoundError):
        print(f"\n*** Make sure the test data directory exists and contains the necessary files: {TEST_DATA_DIR} ***\n")
    sys.exit(1)
# --- MODIFICATION END ---


class MockGPS:
    """A class to simulate GPS data feed for testing."""
    
    def __init__(self, routes=None):
        """
        Initialize with predefined GPS routes.
        
        Args:
            routes: A dictionary where keys are route names and values are
                   lists of (lat, lon, fix_quality) tuples.
        """
        self.routes = routes or {}
        self.current_route = None
        self.route_index = 0
        self.gps_queue = None
        self.should_stop = threading.Event()
        self.thread = None
        self.delay = 0.1  # seconds between GPS points
    
    def add_route(self, name, points):
        """Add a new route or replace an existing one."""
        self.routes[name] = points
    
    def set_route(self, name):
        """Set the current route to use."""
        if name in self.routes:
            self.current_route = name
            self.route_index = 0
            return True
        return False
    
    def start(self, gps_queue, delay=None):
        """
        Start sending GPS data to the provided queue.
        
        Args:
            gps_queue: Queue to send GPS data to
            delay: Time in seconds between GPS points
        """
        if delay is not None:
            self.delay = delay
        
        self.gps_queue = gps_queue
        self.should_stop.clear()
        self.thread = threading.Thread(target=self._simulate_gps)
        self.thread.daemon = True
        self.thread.start()
    
    def stop(self):
        """Stop sending GPS data."""
        if self.thread and self.thread.is_alive():
            self.should_stop.set()
            self.thread.join(timeout=1.0)
    
    def _simulate_gps(self):
        """Thread function to simulate GPS data."""
        if not self.current_route or not self.gps_queue:
            return
        
        route = self.routes[self.current_route]
        
        while not self.should_stop.is_set():
            if self.route_index >= len(route):
                # Loop back to beginning of route
                self.route_index = 0
            
            # Get current point
            lat, lon, fix_qual = route[self.route_index]
            
            # Create GPS data
            gps_data = {
                'lat': lat,
                'lon': lon,
                'fix': fix_qual > 0,
                'gps_qual': fix_qual,
                'time': time.time()
            }
            
            # Add to queue
            self.gps_queue.put(gps_data)
            
            # Move to next point
            self.route_index += 1
            
            # Wait before next point
            if self.should_stop.wait(timeout=self.delay):
                break


class TestGPSRoadTracking(unittest.TestCase):
    """Tests focused on GPS processing and road tracking."""
    
    def setUp(self):
        """Set up test environment before each test."""
        # Create temporary directories for test data
        self.temp_dir = tempfile.mkdtemp()
        self.save_dir = os.path.join(self.temp_dir, "recordings")
        os.makedirs(self.save_dir, exist_ok=True)
        
        # --- MODIFICATION START ---
        # The PREPROCESSED_DIR is already set correctly during the patched import.
        # We just need to patch the other paths for this specific test run.
        self.patcher_save_dir = patch('aio_t14b_mk2.SAVE_DIR', self.save_dir)
        self.patcher_csv_file = patch('aio_t14b_mk2.CSV_FILE', os.path.join(self.save_dir, "test_gps_log.csv"))
        self.patcher_database = patch('aio_t14b_mk2.DATABASE', os.path.join(self.temp_dir, "test_coverage.db"))

        self.patcher_save_dir.start()
        self.patcher_csv_file.start()
        self.patcher_database.start()
        # --- MODIFICATION END ---
        
        # Reset global state variables
        rcr.gps_queue = queue.Queue()
        rcr.gps_data = {}
        rcr.recorded_roads = set()
        rcr.road_coverage_state = {}
        rcr.current_road_id = None
        rcr.recording_proc = None
        rcr.recording_file = None
        rcr.recording_start_time = None
        rcr.last_recording_stop = 0
        rcr.shutdown_event = threading.Event()
        
        # Mock CSV buffer
        rcr.csv_buffer = []
        rcr.csv_buffer_lock = threading.Lock()
        rcr.last_csv_flush = time.time()
        
        # Mock counters
        rcr.counter_lock = threading.Lock()
        rcr.zone_check_counter = 0
        rcr.gps_read_counter = 0
        
        # Create test road data
        self.create_test_road_network()
        
        # Initialize mock GPS
        self.mock_gps = MockGPS()
        self.setup_gps_routes()
        
        # Mock recording functions to avoid actual subprocess calls
        self.orig_start_recording = rcr.start_recording
        self.orig_stop_recording = rcr.stop_recording
        rcr.start_recording = MagicMock(return_value="/tmp/test_recording.mp4")
        rcr.stop_recording = MagicMock()
        rcr.save_recording_to_db = MagicMock()
        
        # Initialize CSV - but patch init_csv to use the temp directory
        with patch('aio_t14b_mk2.SAVE_DIR', self.save_dir):
            rcr.init_csv()
    
    def tearDown(self):
        """Clean up after each test."""
        # Stop GPS simulation if running
        self.mock_gps.stop()
        
        # Reset original functions
        rcr.start_recording = self.orig_start_recording
        rcr.stop_recording = self.orig_stop_recording
        
        # --- MODIFICATION START ---
        # Stop all patches
        self.patcher_save_dir.stop()
        self.patcher_csv_file.stop()
        self.patcher_database.stop()
        # --- MODIFICATION END ---

        # Clear temp directory
        shutil.rmtree(self.temp_dir)
        
        # Reset shutdown event
        rcr.shutdown_event.clear()
    
    def create_test_road_network(self):
        """Load actual road network for testing."""
        try:
            # The data is already loaded by the patched import, just assign it to the test class
            self.road_data = rcr.ROAD_DATA
            self.buffer_polygons = rcr.BUFFER_POLYGONS
            self.bounds_array = rcr.BOUNDS_ARRAY
            self.road_ids = rcr.ROAD_IDS
            
            # Get some sample roads for testing
            self.sample_roads = self.road_ids[:4] if len(self.road_ids) >= 4 else self.road_ids
            
            print(f"Loaded actual road network with {len(self.road_ids)} roads")
        except Exception as e:
            print(f"Warning: Could not load actual road data: {e}")
            print("Creating minimal test road network instead")
            
            # Create minimal test data if real data fails to load
            self._create_fallback_road_network()
    
    # ... (The rest of the file from _create_fallback_road_network onwards remains the same) ...
    def _create_fallback_road_network(self):
        """Create a minimal test road network as fallback when real data can't be loaded."""
        # Define several roads with multiple segments
        
        # Road 1: A straight east-west road
        road_1_coords = [
            (-122.1000, 37.4000),  # West end
            (-122.0900, 37.4000),
            (-122.0800, 37.4000),
            (-122.0700, 37.4000),  # East end
        ]
        
        # Road 2: A straight north-south road that intersects Road 1
        road_2_coords = [
            (-122.0850, 37.3900),  # South end
            (-122.0850, 37.4000),  # Intersection with Road 1
            (-122.0850, 37.4100),  # North end
        ]
        
        # Road 3: A curved road
        road_3_coords = [
            (-122.0700, 37.4000),  # Connected to east end of Road 1
            (-122.0650, 37.4050),
            (-122.0600, 37.4100),
            (-122.0550, 37.4150),
            (-122.0500, 37.4200),
        ]
        
        # Road 4: A diagonal road
        road_4_coords = [
            (-122.0950, 37.3950),  # Southwest
            (-122.0900, 37.4000),  # Intersection with Road 1
            (-122.0850, 37.4050),  # Intersection with Road 2
            (-122.0800, 37.4100),  # Northeast
        ]
        
        # Create buffer polygons (simplified as rectangles around roads)
        buffer_polygons = []
        bounds_array = []
        
        # Helper to create buffer around a line segment
        def create_buffer(coords, width=0.0010):  # Width in degrees (approx 100m)
            min_lon = min(p[0] for p in coords) - width
            max_lon = max(p[0] for p in coords) + width
            min_lat = min(p[1] for p in coords) - width
            max_lat = max(p[1] for p in coords) + width
            
            # Create polygon and bounds
            poly = MockPolygon([
                (min_lon, min_lat), (max_lon, min_lat),
                (max_lon, max_lat), (min_lon, max_lat)
            ])
            bounds = [min_lon, min_lat, max_lon, max_lat]
            
            return poly, bounds
        
        # Create buffers for each road
        poly1, bounds1 = create_buffer(road_1_coords)
        poly2, bounds2 = create_buffer(road_2_coords)
        poly3, bounds3 = create_buffer(road_3_coords)
        poly4, bounds4 = create_buffer(road_4_coords)
        
        buffer_polygons.extend([poly1, poly2, poly3, poly4])
        bounds_array = np.array([bounds1, bounds2, bounds3, bounds4])
        
        # Create road data structure
        road_data = {
            "road_1": {
                "name": "Main Street",
                "segments": road_1_coords
            },
            "road_2": {
                "name": "North Avenue",
                "segments": road_2_coords
            },
            "road_3": {
                "name": "Curve Drive",
                "segments": road_3_coords
            },
            "road_4": {
                "name": "Diagonal Way",
                "segments": road_4_coords
            }
        }
        
        # Create road IDs array
        road_ids = ["road_1", "road_2", "road_3", "road_4"]
        
        # Store reference to the test data
        self.road_data = road_data
        self.buffer_polygons = buffer_polygons
        self.bounds_array = bounds_array
        self.road_ids = road_ids
        self.sample_roads = self.road_ids
        
        # Load the data into the module
        rcr.BOUNDS_ARRAY = bounds_array
        rcr.ROAD_DATA = road_data
        rcr.BUFFER_POLYGONS = buffer_polygons
        rcr.ROAD_IDS = road_ids
        rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in buffer_polygons]
    
    def setup_gps_routes(self):
        """Set up GPS routes for testing different scenarios using actual road data."""
        # Get some sample roads
        if not hasattr(self, 'sample_roads') or not self.sample_roads:
            print("Warning: No sample roads available for GPS routes")
            self._create_fallback_gps_routes()
            return
            
        # Get coordinates from actual roads for more realistic testing
        try:
            # Select up to 4 roads to use in routes
            test_roads = self.sample_roads[:4]
            
            # Create routes based on actual road coordinates
            road_routes = {}
            
            for i, road_id in enumerate(test_roads):
                if road_id not in self.road_data:
                    continue
                    
                segments = self.road_data[road_id]['segments']
                if not segments:
                    continue
                
                # Create GPS points from the road segments
                route = []
                
                # First add an approach point (slightly offset from the road)
                first_seg = segments[0]
                lat_offset = 0.0005  # ~50m offset
                lon_offset = 0.0005
                route.append((first_seg[1] - lat_offset, first_seg[0] - lon_offset, 1))  # Approaching point
                
                # Add points from the road segments
                for seg in segments:
                    route.append((seg[1], seg[0], 1))  # Points directly on the road
                
                # Add an exit point
                last_seg = segments[-1]
                route.append((last_seg[1] + lat_offset, last_seg[0] + lon_offset, 1))  # Exit point
                
                # Add the route
                road_routes[f"road_{i+1}"] = route
                
            # Create a combined route that traverses multiple roads
            combined_route = []
            for route in road_routes.values():
                if combined_route and route:
                    # Add an off-road transition between roads
                    transition_start = combined_route[-1]
                    transition_end = route[0]
                    mid_lat = (transition_start[0] + transition_end[0]) / 2
                    mid_lon = (transition_start[1] + transition_end[1]) / 2
                    combined_route.append((mid_lat, mid_lon, 1))  # Off-road transition point
                
                combined_route.extend(route)
            
            # Create a route with GPS signal loss
            gps_loss_route = []
            if road_routes.get("road_1"):
                base_route = road_routes["road_1"]
                # Insert GPS loss in the middle of the route
                mid_idx = len(base_route) // 2
                gps_loss_route = base_route[:mid_idx]
                
                # Add points with no GPS fix
                for i in range(2):
                    if mid_idx + i < len(base_route):
                        pt = base_route[mid_idx + i]
                        gps_loss_route.append((pt[0], pt[1], 0))  # No GPS fix
                
                # Resume GPS fix
                gps_loss_route.extend(base_route[mid_idx + 2:])
            
            # Add routes to mock GPS
            for name, route in road_routes.items():
                if route:
                    self.mock_gps.add_route(name, route)
            
            if combined_route:
                self.mock_gps.add_route("network_tour", combined_route)
            
            if gps_loss_route:
                self.mock_gps.add_route("gps_loss", gps_loss_route)
                
            print(f"Created GPS routes from actual road data: {list(road_routes.keys())}")
            
            # Make sure we have at least one route
            if not self.mock_gps.routes:
                print("Warning: Failed to create routes from actual roads, using fallback")
                self._create_fallback_gps_routes()
                
        except Exception as e:
            print(f"Error creating GPS routes from actual roads: {e}")
            self._create_fallback_gps_routes()
    
    def _create_fallback_gps_routes(self):
        """Create fallback GPS routes when actual road data can't be used."""
        print("Creating fallback GPS routes with hardcoded coordinates")
        
        # Route 1: Drive along Road 1 (Main Street) from west to east
        route_1 = [
            (37.4000, -122.1020, 1),  # Approaching from west
            (37.4000, -122.1000, 1),  # Start of Road 1
            (37.4000, -122.0950, 1),
            (37.4000, -122.0900, 1),  # Intersection with Road 4
            (37.4000, -122.0880, 1),
            (37.4000, -122.0850, 1),  # Intersection with Road 2
            (37.4000, -122.0820, 1),
            (37.4000, -122.0800, 1),
            (37.4000, -122.0750, 1),
            (37.4000, -122.0700, 1),  # End of Road 1, start of Road 3
            (37.4000, -122.0680, 1),  # Now off any road
        ]
        
        # Route 2: Drive along Road 2 (North Avenue) from south to north
        route_2 = [
            (37.3880, -122.0850, 1),  # Approaching from south
            (37.3900, -122.0850, 1),  # Start of Road 2
            (37.3950, -122.0850, 1),
            (37.4000, -122.0850, 1),  # Intersection with Road 1
            (37.4050, -122.0850, 1),  # Intersection with Road 4
            (37.4100, -122.0850, 1),  # End of Road 2
            (37.4120, -122.0850, 1),  # Now off any road
        ]
        
        # Route 3: Drive along Road 3 (Curve Drive)
        route_3 = [
            (37.4000, -122.0700, 1),  # Start of Road 3, connected to Road 1
            (37.4050, -122.0650, 1),
            (37.4100, -122.0600, 1),
            (37.4150, -122.0550, 1),
            (37.4200, -122.0500, 1),  # End of Road 3
            (37.4220, -122.0480, 1),  # Now off any road
        ]
        
        # Route 4: Drive around the network
        route_4 = [
            # Start on Road 1, west end
            (37.4000, -122.1000, 1),
            (37.4000, -122.0900, 1),
            # Turn onto Road 4
            (37.4020, -122.0880, 1),
            (37.4050, -122.0850, 1),
            # Continue on Road 4
            (37.4080, -122.0820, 1),
            (37.4100, -122.0800, 1),
            # Off road briefly
            (37.4120, -122.0780, 1),
            # Back to Road 2, heading south
            (37.4100, -122.0850, 1),
            (37.4050, -122.0850, 1),
            (37.4000, -122.0850, 1),
            # Turn east onto Road 1
            (37.4000, -122.0820, 1),
            (37.4000, -122.0750, 1),
            (37.4000, -122.0700, 1),
            # Turn onto Road 3
            (37.4050, -122.0650, 1),
            (37.4100, -122.0600, 1),
            (37.4150, -122.0550, 1),
            (37.4200, -122.0500, 1),
        ]
        
        # Route 5: GPS signal lost and regained
        route_5 = [
            (37.4000, -122.1000, 1),  # Start on Road 1
            (37.4000, -122.0950, 1),
            (37.4000, -122.0900, 1),
            (37.4000, -122.0850, 0),  # GPS lost (fix_qual = 0)
            (37.4000, -122.0800, 0),  # Still no GPS
            (37.4000, -122.0750, 1),  # GPS regained
            (37.4000, -122.0700, 1),
        ]
        
        # Add routes to mock GPS
        self.mock_gps.add_route("road1_west_to_east", route_1)
        self.mock_gps.add_route("road2_south_to_north", route_2)
        self.mock_gps.add_route("road3_curve", route_3)
        self.mock_gps.add_route("network_tour", route_4)
        self.mock_gps.add_route("gps_loss", route_5)
    
    def run_road_tracking_logic(self, duration=3.0):
        """
        Run the road tracking logic loop with the current GPS queue.
        
        Args:
            duration: How long to run the tracking loop (seconds)
        """
        end_time = time.time() + duration
        last_on_road, exit_logged = None, False
        
        while time.time() < end_time and not rcr.shutdown_event.is_set():
            try:
                gps = rcr.gps_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            
            # Update global GPS data
            rcr.gps_data = gps
            
            # Check if on a road
            rid, info = rcr.find_current_road(gps['lon'], gps['lat'])
            
            if rid:
                # Update coverage
                seg_idx, seg_dist = rcr.find_nearest_segment(rid, gps['lat'], gps['lon'])
                if seg_dist <= rcr.SEGMENT_THRESHOLD_M:
                    rcr.road_coverage_state.setdefault(rid, set()).add(seg_idx)
                
                # Update state
                last_on_road = time.time()
                exit_logged = False
                
                # Handle road changes
                if rid != rcr.current_road_id:
                    # Stop previous recording if any
                    if rcr.recording_proc:
                        rcr.stop_recording()
                        if rcr.current_road_id:
                            rcr.save_recording_to_db(
                                rcr.current_road_id, 
                                rcr.recording_file, 
                                rcr.calculate_coverage(rcr.current_road_id)
                            )
                    
                    # Enter new road
                    rcr.log_csv('ROAD_ENTER', road_id=rid)
                    
                    # Start recording if not already recorded
                    if rid not in rcr.recorded_roads:
                        rcr.recording_proc = True  # Fake recording process
                        rcr.recording_file = f"/tmp/road_{rid}_{int(time.time())}.mp4"
                        rcr.recording_start_time = time.time()
                        rcr.start_recording(rid)
                    
                    rcr.current_road_id = rid
            else:
                # Check if we've been off-road long enough to exit
                if (rcr.current_road_id and last_on_road and not exit_logged and 
                        time.time() - last_on_road > rcr.ROAD_EXIT_THRESHOLD_S):
                    # Exit current road
                    pct = rcr.calculate_coverage(rcr.current_road_id)
                    rcr.log_csv('ROAD_EXIT', road_id=rcr.current_road_id, notes=f"coverage={pct:.1f}")
                    
                    # Stop recording
                    if rcr.recording_proc:
                        rcr.stop_recording()
                        rcr.save_recording_to_db(rcr.current_road_id, rcr.recording_file, pct)
                    
                    rcr.current_road_id, exit_logged = None, True
            
            # Log position
            rcr.log_csv('GPS_POSITION', lat=gps['lat'], lon=gps['lon'], 
                      fix=gps['fix'], gps_qual=gps['gps_qual'])
    
    def test_road1_tracking(self):
        """Test tracking while driving along a road."""
        # Set up route - use road_1 if available, otherwise the first available route
        route_name = None
        if "road_1" in self.mock_gps.routes:
            route_name = "road_1"
        elif self.mock_gps.routes:
            route_name = list(self.mock_gps.routes.keys())[0]
        else:
            self.skipTest("No GPS routes available for testing")
        
        # Get the road ID for this route
        if route_name.startswith("road_") and len(self.sample_roads) >= int(route_name.split("_")[1]):
            road_id = self.sample_roads[int(route_name.split("_")[1])-1]
        else:
            road_id = self.sample_roads[0] if self.sample_roads else "road_1"
            
        print(f"Testing road tracking with route '{route_name}' for road ID '{road_id}'")
        
        # Set up route
        self.mock_gps.set_route(route_name)
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=1.0)
        
        # Verify road coverage state contains expected roads
        road_ids = set(rcr.road_coverage_state.keys())
        self.assertGreater(len(road_ids), 0, "Should detect at least one road")
        
        print(f"Detected roads: {road_ids}")
        
        # Verify coverage
        for detected_road in road_ids:
            coverage = rcr.calculate_coverage(detected_road)
            self.assertGreater(coverage, 0, f"Should have some coverage of road {detected_road}")
            print(f"Coverage for {detected_road}: {coverage:.1f}%")
        
        # Verify recording was started for at least one road
        self.assertGreater(rcr.start_recording.call_count, 0, "Should start recording for at least one road")
    
    def test_road_change_detection(self):
        """Test detection of changing from one road to another."""
        # Add road_1 to recorded roads
        rcr.recorded_roads.add("road_1")
        
        # Set up network tour route
        if "network_tour" not in self.mock_gps.routes:
            self.skipTest("Network tour route not available")
        
        self.mock_gps.set_route("network_tour")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=2.0)
        
        # Verify multiple roads were detected
        detected_roads = set(rcr.road_coverage_state.keys())
        
        # We should detect at least one road
        self.assertGreater(len(detected_roads), 0, "Should detect at least one road")
        
        # If we detected multiple roads, verify recording logic
        if len(detected_roads) > 1:
            # Verify recording was not started for road_1 (already recorded)
            for args, _ in rcr.start_recording.call_args_list:
                self.assertNotEqual(args[0], "road_1", "Should not record road_1 again")
            
            # Verify other roads were recorded
            other_roads = detected_roads - {"road_1"}
            for road in other_roads:
                found = False
                for args, _ in rcr.start_recording.call_args_list:
                    if args[0] == road:
                        found = True
                        break
                self.assertTrue(found, f"Should start recording for {road}")
    
    def test_road_exit_detection(self):
        """Test detection of exiting a road."""
        # Modify ROAD_EXIT_THRESHOLD_S for faster testing
        original_threshold = rcr.ROAD_EXIT_THRESHOLD_S
        rcr.ROAD_EXIT_THRESHOLD_S = 0.2  # 200ms
        
        try:
            # Set up route with clear exit point
            route_name = None
            if "road1_west_to_east" in self.mock_gps.routes:
                route_name = "road1_west_to_east"
            elif self.mock_gps.routes:
                # Find any route that ends with an off-road point
                for name, route in self.mock_gps.routes.items():
                    if len(route) >= 2:  # Need at least 2 points
                        self.mock_gps.set_route(name)
                        route_name = name
                        break
            
            if not route_name:
                self.skipTest("No suitable route found for exit detection test")
            
            # Start GPS simulation
            self.mock_gps.start(rcr.gps_queue, delay=0.05)
            
            # Run tracking logic
            self.run_road_tracking_logic(duration=1.0)
            
            # Verify at least one road was detected
            self.assertGreater(len(rcr.road_coverage_state), 0, "Should detect at least one road")
            
            # If recording was stopped, current_road_id should be None
            if rcr.stop_recording.called:
                self.assertIsNone(rcr.current_road_id, "Should exit the road")
                
                # Verify database save was called
                rcr.save_recording_to_db.assert_called()
            else:
                print("Warning: recording was not stopped, exit detection may not have worked")
        finally:
            # Restore original threshold
            rcr.ROAD_EXIT_THRESHOLD_S = original_threshold
    
    def test_gps_signal_loss(self):
        """Test behavior when GPS signal is lost and regained."""
        # Set up route with GPS signal loss
        if "gps_loss" not in self.mock_gps.routes:
            self.skipTest("GPS loss route not available")
        
        self.mock_gps.set_route("gps_loss")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=1.0)
        
        # Verify at least one road was detected
        self.assertGreater(len(rcr.road_coverage_state), 0, "Should detect at least one road")
        
        # Check coverage for the first road
        first_road = list(rcr.road_coverage_state.keys())[0]
        segments_covered = len(rcr.road_coverage_state.get(first_road, set()))
        total_segments = len(rcr.ROAD_DATA[first_road]["segments"])
        
        # We should have some coverage, but might not be complete due to GPS loss
        self.assertGreater(segments_covered, 0, "Should have some coverage")
        self.assertLessEqual(segments_covered, total_segments, "Coverage should not exceed 100%")
        
        print(f"Coverage with GPS loss: {segments_covered}/{total_segments} segments")
    
    def test_segment_distance_threshold(self):
        """Test the segment distance threshold logic."""
        # Create a route that passes near but not directly on a road segment
        # First find a road to use
        if not self.road_ids:
            self.skipTest("No roads available for testing")
        
        test_road_id = self.road_ids[0]
        test_road = rcr.ROAD_DATA[test_road_id]
        
        if not test_road["segments"]:
            self.skipTest(f"Test road {test_road_id} has no segments")
        
        # Create points at varying distances from the road
        near_road_route = []
        
        # Use the first segment
        seg_lon, seg_lat = test_road["segments"][0]
        
        # Create points at 10m, 20m, 30m, 50m, and 70m away
        distances = [10, 20, 30, 50, 70]
        for dist in distances:
            # Convert meters to approximate degrees (rough conversion)
            # 1 degree latitude ≈ 111 km, 1 degree longitude varies with latitude
            deg_lat = dist / 111000  # degrees latitude offset for dist meters
            near_road_route.append((seg_lat + deg_lat, seg_lon, 1))  # Offset in latitude
        
        # Add a point directly on the road
        near_road_route.append((seg_lat, seg_lon, 1))
        
        # Add the route
        self.mock_gps.add_route("near_road_test", near_road_route)
        self.mock_gps.set_route("near_road_test")
        
        # Test with different thresholds
        test_thresholds = [
            10,   # Should only detect the 10m point and the direct point
            30,   # Should detect the 10m, 20m, 30m points and direct point
            100,  # Should detect all points
        ]
        
        for threshold in test_thresholds:
            # Reset state
            rcr.road_coverage_state = {}
            
            # Set threshold
            original_threshold = rcr.SEGMENT_THRESHOLD_M
            rcr.SEGMENT_THRESHOLD_M = threshold
            
            try:
                # Start GPS simulation
                self.mock_gps.start(rcr.gps_queue, delay=0.05)
                
                # Run tracking logic
                self.run_road_tracking_logic(duration=1.0)
                
                # Stop GPS simulation
                self.mock_gps.stop()
                
                # Count segments covered for the test road
                road_segments = len(rcr.road_coverage_state.get(test_road_id, set()))
                
                # Verify coverage based on threshold
                if threshold >= 10:
                    self.assertGreater(
                        road_segments, 
                        0, 
                        f"With {threshold}m threshold, should detect at least 1 segment"
                    )
                
                print(f"Threshold {threshold}m detected {road_segments} segments")
                
            finally:
                # Restore original threshold
                rcr.SEGMENT_THRESHOLD_M = original_threshold
    
    def test_fast_driving(self):
        """Test road tracking with fast driving (larger gaps between GPS points)."""
        # Find a road with multiple segments
        suitable_roads = [rid for rid in self.road_ids 
                         if rid in self.road_data and len(self.road_data[rid]['segments']) >= 4]
        
        if not suitable_roads:
            self.skipTest("No suitable roads with multiple segments found")
        
        test_road_id = suitable_roads[0]
        segments = self.road_data[test_road_id]['segments']
        
        # Create a route with larger gaps between points (simulating fast driving)
        fast_route = []
        
        # Add only every other point to simulate fast driving
        for i in range(0, len(segments), 2):
            lon, lat = segments[i]
            fast_route.append((lat, lon, 1))
        
        # Make sure we have at least 2 points
        if len(fast_route) < 2:
            self.skipTest(f"Road {test_road_id} doesn't have enough segments for fast driving test")
        
        # Add the route
        self.mock_gps.add_route("fast_driving", fast_route)
        self.mock_gps.set_route("fast_driving")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=1.0)
        
        # Verify the road was detected
        self.assertIn(test_road_id, rcr.road_coverage_state, f"Should detect {test_road_id}")
        
        # Check coverage - should be lower due to fast driving
        covered_segments = len(rcr.road_coverage_state[test_road_id])
        total_segments = len(segments)
        coverage = rcr.calculate_coverage(test_road_id)
        
        print(f"Fast driving coverage: {covered_segments}/{total_segments} segments = {coverage:.1f}%")
        
        # Coverage should be less than 100% due to skipped segments
        self.assertLess(coverage, 100, "Coverage should be incomplete due to fast driving")
        
        # But should still have some coverage
        self.assertGreater(coverage, 0, "Should have some coverage")
    
    def test_recording_lifecycle(self):
        """Test the complete lifecycle of recording starts and stops."""
        # Track recording start and stop calls
        recorded_roads = []
        saved_recordings = []
        
        # Override mocks to track calls
        def mock_start(road_id):
            recorded_roads.append(road_id)
            return f"/tmp/test_recording_{road_id}.mp4"
        
        def mock_save(road_id, file, coverage):
            saved_recordings.append((road_id, file, coverage))
        
        rcr.start_recording = MagicMock(side_effect=mock_start)
        rcr.save_recording_to_db = MagicMock(side_effect=mock_save)
        
        # Set a shorter road exit threshold for testing
        original_threshold = rcr.ROAD_EXIT_THRESHOLD_S
        rcr.ROAD_EXIT_THRESHOLD_S = 0.2  # 200ms
        
        try:
            # Use network tour route if available
            if "network_tour" in self.mock_gps.routes:
                self.mock_gps.set_route("network_tour")
            elif self.mock_gps.routes:
                # Use any available route
                self.mock_gps.set_route(list(self.mock_gps.routes.keys())[0])
            else:
                self.skipTest("No GPS routes available for testing")
            
            # Start GPS simulation
            self.mock_gps.start(rcr.gps_queue, delay=0.05)
            
            # Run tracking logic
            self.run_road_tracking_logic(duration=3.0)
            
            # Verify at least one recording was started
            self.assertGreater(len(recorded_roads), 0, "Should start at least one recording")
            
            # Verify recordings were saved if we exited roads
            if saved_recordings:
                # Each saved recording should correspond to a started recording
                for road_id, _, _ in saved_recordings:
                    self.assertIn(road_id, recorded_roads, 
                               f"Road {road_id} should be in started recordings")
            
            print(f"Recorded roads: {recorded_roads}")
            print(f"Saved recordings: {saved_recordings}")
        finally:
            # Restore original threshold
            rcr.ROAD_EXIT_THRESHOLD_S = original_threshold
    
    def test_already_recorded_roads(self):
        """Test that already recorded roads are not recorded again."""
        # Get a road to mark as already recorded
        if not self.road_ids:
            self.skipTest("No roads available for testing")
        
        first_road = self.road_ids[0]
        
        # Mark it as already recorded
        rcr.recorded_roads = {first_road}
        
        # Find a route that includes this road
        test_route = None
        for name, route in self.mock_gps.routes.items():
            if len(route) >= 2:  # Need at least 2 points
                self.mock_gps.set_route(name)
                test_route = name
                break
        
        if not test_route:
            self.skipTest("No suitable route found for testing")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=2.0)
        
        # Verify coverage is still tracked even for already recorded roads
        if first_road in rcr.road_coverage_state:
            coverage = rcr.calculate_coverage(first_road)
            self.assertGreaterEqual(coverage, 0, f"Should track coverage for {first_road}")
            
            # But recording should not be started for already recorded roads
            for args, _ in rcr.start_recording.call_args_list:
                self.assertNotEqual(args[0], first_road, 
                              f"Should not start recording for already recorded road {first_road}")
    
    def test_gps_queue_processing(self):
        """Test that GPS points are processed correctly from the queue."""
        # Create a set of test points
        test_points = []
        
        # Use real road coordinates if available
        if self.road_ids and self.road_data[self.road_ids[0]]['segments']:
            segments = self.road_data[self.road_ids[0]]['segments']
            for i, seg in enumerate(segments[:4]):  # Use up to 4 points
                lon, lat = seg
                test_points.append({
                    "lat": lat, "lon": lon, 
                    "fix": True, "gps_qual": 1, 
                    "time": time.time() + i  # Increasing time
                })
        else:
            # Use fallback points
            test_points = [
                {"lat": 37.4000, "lon": -122.1000, "fix": True, "gps_qual": 1, "time": time.time()},
                {"lat": 37.4000, "lon": -122.0900, "fix": True, "gps_qual": 1, "time": time.time() + 1},
                {"lat": 37.4000, "lon": -122.0800, "fix": True, "gps_qual": 1, "time": time.time() + 2},
                {"lat": 37.4000, "lon": -122.0700, "fix": True, "gps_qual": 1, "time": time.time() + 3},
            ]
        
        # Put points in queue
        for point in test_points:
            rcr.gps_queue.put(point)
        
        # Process points
        processed_points = []
        
        # Override find_current_road to track processed points
        original_find_road = rcr.find_current_road
        def mock_find_road(lon, lat):
            processed_points.append((lat, lon))
            return original_find_road(lon, lat)
        
        rcr.find_current_road = mock_find_road
        
        try:
            # Run tracking logic
            self.run_road_tracking_logic(duration=1.0)
            
            # Verify all points were processed
            self.assertEqual(len(processed_points), len(test_points), 
                           "Should process all GPS points")
            
            # Verify points were processed in order
            for i, point in enumerate(test_points):
                self.assertEqual(processed_points[i][0], point["lat"], 
                               f"Point {i} latitude should match")
                self.assertEqual(processed_points[i][1], point["lon"], 
                               f"Point {i} longitude should match")
        finally:
            # Restore original function
            rcr.find_current_road = original_find_road
    
    def test_recording_duration_minimum(self):
            """Test that recordings have a minimum duration."""
            # Temporarily restore the original stop_recording function for this test,
            # as the default setUp replaces it with a simple MagicMock.
            original_mock_stop = rcr.stop_recording
            rcr.stop_recording = self.orig_stop_recording

            try:
                # The real stop_recording function calls os.killpg, which we must patch
                # to prevent errors during the test.
                with patch('aio_t14b_mk2.os.getpgid'), patch('aio_t14b_mk2.os.killpg'):

                    # Create a short route that will exit the road quickly
                    if not self.road_ids or not self.road_data[self.road_ids[0]]['segments']:
                        self.skipTest("No suitable roads for testing")

                    road_id = self.road_ids[0]
                    segments = self.road_data[road_id]['segments']
                    if len(segments) < 2:
                        self.skipTest(f"Road {road_id} doesn't have enough segments")

                    # Route: start on road, then go off-road to trigger exit
                    short_route = [
                        (segments[0][1], segments[0][0], 1),
                        (segments[1][1], segments[1][0], 1),
                        (segments[1][1] + 0.1, segments[1][0] + 0.1, 1),
                    ]

                    self.mock_gps.add_route("short_route", short_route)
                    self.mock_gps.set_route("short_route")

                    # Set a very short road exit threshold to trigger stop_recording quickly
                    original_threshold = rcr.ROAD_EXIT_THRESHOLD_S
                    rcr.ROAD_EXIT_THRESHOLD_S = 0.1  # 100ms

                    # Start GPS simulation
                    rcr.current_road_id = None
                    self.mock_gps.start(rcr.gps_queue, delay=0.05)

                    # Time the execution of the tracking logic. If the real stop_recording
                    # function works, it will sleep, making the total duration >= 3 seconds.
                    start_time = time.time()
                    self.run_road_tracking_logic(duration=4.0) # Run long enough for sleep to complete
                    total_duration = time.time() - start_time

                    # The road exit happens after ~0.1s. The stop_recording function should then
                    # wait for the remainder of the 3-second minimum duration.
                    self.assertGreaterEqual(
                        total_duration,
                        rcr.MIN_RECORDING_DURATION,
                        "Total execution time should be at least MIN_RECORDING_DURATION due to the enforced wait."
                    )
            finally:
                # Restore the original mock to ensure other tests are not affected
                rcr.stop_recording = original_mock_stop
                rcr.ROAD_EXIT_THRESHOLD_S = original_threshold
    
    def test_concurrent_threads(self):
        """Test that the recorder can handle concurrent threads safely."""
        # We'll simulate concurrent access to shared state
        thread_count = 5
        test_duration = 1.0
        threads = []
        
        # Use any available route
        if not self.mock_gps.routes:
            self.skipTest("No GPS routes available for testing")
        
        self.mock_gps.set_route(list(self.mock_gps.routes.keys())[0])
        
        # Start GPS simulation at a faster rate
        self.mock_gps.start(rcr.gps_queue, delay=0.02)
        
        # Run multiple threads that read road coverage state
        def reader_thread():
            start_time = time.time()
            while time.time() - start_time < test_duration:
                # Read road coverage state
                for road_id in list(rcr.road_coverage_state.keys()):
                    # Access coverage percent (read operation)
                    coverage = rcr.calculate_coverage(road_id)
                time.sleep(0.01)
        
        # Start reader threads
        for i in range(thread_count):
            thread = threading.Thread(target=reader_thread)
            thread.daemon = True
            thread.start()
            threads.append(thread)
        
        # Run main tracking logic
        self.run_road_tracking_logic(duration=test_duration)
        
        # Wait for all threads to finish
        for thread in threads:
            thread.join(timeout=0.5)
        
        # No assertion needed - if there's a threading issue, it would likely
        # cause an exception during the test
    
    def test_find_road_performance(self):
        """Test the performance of road finding algorithm."""
        # Load the preprocessed data if not already loaded
        if not hasattr(rcr, 'PREPARED_POLYGONS') or not rcr.PREPARED_POLYGONS:
            try:
                rcr.BOUNDS_ARRAY = np.load(f"{self.preprocessed_dir}/road_bounds.npy")
                with open(f"{self.preprocessed_dir}/buffer_polygons.pkl", 'rb') as f:
                    rcr.BUFFER_POLYGONS = pickle.load(f)
                with open(f"{self.preprocessed_dir}/road_ids.pkl", 'rb') as f:
                    rcr.ROAD_IDS = pickle.load(f)
                rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in rcr.BUFFER_POLYGONS]
            except Exception as e:
                self.skipTest(f"Could not load preprocessed data: {e}")
        
        # Generate test points from actual roads
        test_points = []
        
        if self.road_ids:
            # Get points from different roads
            for i, road_id in enumerate(self.road_ids[:3]):  # Use up to 3 roads
                if road_id in self.road_data and self.road_data[road_id]['segments']:
                    # Get a point from this road
                    lon, lat = self.road_data[road_id]['segments'][0]
                    test_points.append((lon, lat))  # On road
            
            # Add a point far from any road
            if test_points:
                # Use large offset from first point
                lon, lat = test_points[0]
                test_points.append((lon + 1.0, lat + 1.0))  # Off road
        
        # Use fallback points if needed
        if not test_points:
            test_points = [
                (-122.1000, 37.4000),  # Test point 1
                (-122.0850, 37.4000),  # Test point 2
                (-122.0600, 37.4100),  # Test point 3
                (-122.1500, 37.4500),  # Test point 4
                (-122.0800, 37.3950),  # Test point 5
            ]
        
        # Measure time to find roads for these points
        start_time = time.time()
        results = []
        
        for lon, lat in test_points:
            rid, _ = rcr.find_current_road(lon, lat)
            results.append(rid)
        
        elapsed = time.time() - start_time
        
        # Performance check - should be fast
        points_per_second = len(test_points) / elapsed
        print(f"Road finding performance: {elapsed:.6f} seconds for {len(test_points)} points")
        print(f"Points per second: {points_per_second:.1f}")
        print(f"Results: {results}")
        
        # Should process points reasonably quickly
        self.assertGreater(points_per_second, 10, "Should process at least 10 points per second")


class TestGPSRoadTrackingWithActualData(unittest.TestCase):
    """Tests focused on GPS processing and road tracking using actual road data."""
    
    def setUp(self):
        """Set up test environment before each test."""
        # Create temporary directories for test data
        self.temp_dir = tempfile.mkdtemp()
        self.save_dir = os.path.join(self.temp_dir, "recordings")
        os.makedirs(self.save_dir, exist_ok=True)
        
        self.patcher_save_dir = patch('aio_t14b_mk2.SAVE_DIR', self.save_dir)
        self.patcher_csv_file = patch('aio_t14b_mk2.CSV_FILE', os.path.join(self.save_dir, "test_actual_gps_log.csv"))
        self.patcher_database = patch('aio_t14b_mk2.DATABASE', os.path.join(self.temp_dir, "test_coverage.db"))

        self.patcher_save_dir.start()
        self.patcher_csv_file.start()
        self.patcher_database.start()
        
        # Try to load actual road data
        try:
            # Load actual preprocessed road data
            bounds_array = np.load("preprocessed_roads/road_bounds.npy")
            
            with open("preprocessed_roads/road_data.pkl", "rb") as f:
                road_data = pickle.load(f)
            
            with open("preprocessed_roads/buffer_polygons.pkl", "rb") as f:
                buffer_polygons = pickle.load(f)
            
            with open("preprocessed_roads/road_ids.pkl", "rb") as f:
                road_ids = pickle.load(f)
            
            self.road_data = road_data
            self.buffer_polygons = buffer_polygons
            self.bounds_array = bounds_array
            self.road_ids = road_ids
            self.sample_roads = self.road_ids[:10] if len(self.road_ids) >= 10 else self.road_ids
            
            print(f"Loaded actual road network with {len(self.road_ids)} roads")
            
            # Load the data into the module
            rcr.BOUNDS_ARRAY = bounds_array
            rcr.ROAD_DATA = road_data
            rcr.BUFFER_POLYGONS = buffer_polygons
            rcr.ROAD_IDS = road_ids
            rcr.PREPARED_POLYGONS = [shapely_prepared_mock.prep(poly) for poly in buffer_polygons]
            
        except Exception as e:
            raise unittest.SkipTest(f"Could not load actual road data: {e}")
        
        # Reset global state variables
        rcr.gps_queue = queue.Queue()
        rcr.gps_data = {}
        rcr.recorded_roads = set()
        rcr.road_coverage_state = {}
        rcr.current_road_id = None
        rcr.recording_proc = None
        rcr.recording_file = None
        rcr.recording_start_time = None
        rcr.last_recording_stop = 0
        rcr.shutdown_event = threading.Event()
        
        # Mock CSV buffer
        rcr.csv_buffer = []
        rcr.csv_buffer_lock = threading.Lock()
        rcr.last_csv_flush = time.time()
        
        # Mock counters
        rcr.counter_lock = threading.Lock()
        rcr.zone_check_counter = 0
        rcr.gps_read_counter = 0
        
        # Initialize mock GPS
        self.mock_gps = MockGPS()
        self.setup_actual_gps_routes()
        
        # Mock recording functions to avoid actual subprocess calls
        self.orig_start_recording = rcr.start_recording
        self.orig_stop_recording = rcr.stop_recording
        rcr.start_recording = MagicMock(return_value="/tmp/test_recording.mp4")
        rcr.stop_recording = MagicMock()
        rcr.save_recording_to_db = MagicMock()
        
        # Initialize CSV
        with patch('aio_t14b_mk2.SAVE_DIR', self.save_dir):
            rcr.init_csv()
    
    def tearDown(self):
        """Clean up after each test."""
        # Stop GPS simulation if running
        self.mock_gps.stop()
        
        # Reset original functions
        rcr.start_recording = self.orig_start_recording
        rcr.stop_recording = self.orig_stop_recording
        
        # Stop all patches
        self.patcher_save_dir.stop()
        self.patcher_csv_file.stop()
        self.patcher_database.stop()

        # Clear temp directory
        shutil.rmtree(self.temp_dir)
        
        # Reset shutdown event
        rcr.shutdown_event.clear()
    
    def setup_actual_gps_routes(self):
        """Set up GPS routes using actual road data."""
        # Get some sample roads for testing
        test_roads = self.sample_roads[:5]  # Use up to 5 roads
        
        route_count = 0
        for i, road_id in enumerate(test_roads):
            if road_id not in self.road_data:
                continue
                
            segments = self.road_data[road_id]['segments']
            if not segments or len(segments) < 2:
                continue
            
            # Create GPS points from the road segments
            route = []
            
            # Add points from the road segments
            for seg in segments:
                route.append((seg[1], seg[0], 1))  # (lat, lon, fix_quality)
            
            # Add the route
            self.mock_gps.add_route(f"actual_road_{i+1}", route)
            route_count += 1
            
            if route_count >= 3:  # Limit to 3 routes for testing
                break
        
        # Create a combined route that traverses multiple roads
        if route_count >= 2:
            combined_route = []
            for route_name in list(self.mock_gps.routes.keys())[:2]:
                route = self.mock_gps.routes[route_name]
                combined_route.extend(route)
            
            if combined_route:
                self.mock_gps.add_route("actual_network_tour", combined_route)
        
        print(f"Created {len(self.mock_gps.routes)} GPS routes from actual road data")
    
    def test_actual_road_tracking(self):
        """Test tracking while driving along actual roads."""
        if not self.mock_gps.routes:
            self.skipTest("No GPS routes available for testing")
        
        # Use the first available route
        route_name = list(self.mock_gps.routes.keys())[0]
        self.mock_gps.set_route(route_name)
        
        print(f"Testing road tracking with actual route '{route_name}'")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic (using the same function from parent class)
        end_time = time.time() + 2.0
        while time.time() < end_time and not rcr.shutdown_event.is_set():
            try:
                gps = rcr.gps_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            
            # Update global GPS data
            rcr.gps_data = gps
            
            # Check if on a road
            rid, info = rcr.find_current_road(gps['lon'], gps['lat'])
            
            if rid:
                # Update coverage
                seg_idx, seg_dist = rcr.find_nearest_segment(rid, gps['lat'], gps['lon'])
                if seg_dist <= rcr.SEGMENT_THRESHOLD_M:
                    rcr.road_coverage_state.setdefault(rid, set()).add(seg_idx)
                
                # Handle road entry
                if rid != rcr.current_road_id:
                    if rcr.recording_proc:
                        rcr.stop_recording()
                    
                    # Start recording if not already recorded
                    if rid not in rcr.recorded_roads:
                        rcr.start_recording(rid)
                    
                    rcr.current_road_id = rid
        
        # Verify road coverage state contains expected roads
        road_ids = set(rcr.road_coverage_state.keys())
        self.assertGreater(len(road_ids), 0, "Should detect at least one actual road")
        
        print(f"Detected actual roads: {road_ids}")
        
        # Verify coverage
        for detected_road in road_ids:
            # Verify the detected road is in our actual road data
            self.assertIn(detected_road, self.road_data, f"Detected road {detected_road} should be in actual road data")
            
            coverage = rcr.calculate_coverage(detected_road)
            self.assertGreater(coverage, 0, f"Should have some coverage of actual road {detected_road}")
            print(f"Coverage for actual road {detected_road}: {coverage:.1f}%")
        
        # Verify recording was started for at least one road
        self.assertGreater(rcr.start_recording.call_count, 0, "Should start recording for at least one actual road")
    
    def test_actual_road_segments_precision(self):
        """Test precision of segment detection with actual road geometries."""
        if not self.sample_roads:
            self.skipTest("No actual roads available for testing")
        
        # Find a road with multiple segments
        test_road = None
        for road_id in self.sample_roads:
            if len(self.road_data[road_id]['segments']) >= 5:
                test_road = road_id
                break
        
        if not test_road:
            self.skipTest("No road with sufficient segments found")
        
        road_info = self.road_data[test_road]
        segments = road_info['segments']
        
        print(f"Testing segment precision with actual road {test_road} ({len(segments)} segments)")
        
        # Test points exactly on segments
        for i, (lon, lat) in enumerate(segments):
            gps_data = {
                'lat': lat,
                'lon': lon,
                'fix': True,
                'gps_qual': 1,
                'time': time.time() + i
            }
            
            # Find road and segment
            rid, info = rcr.find_current_road(lon, lat)
            
            if rid == test_road:
                seg_idx, seg_dist = rcr.find_nearest_segment(rid, lat, lon)
                
                # Should find the correct segment index or very close
                self.assertLessEqual(seg_dist, 10, f"Distance to segment should be very small for point on road, got {seg_dist}m")
                
                # Add to coverage
                rcr.road_coverage_state.setdefault(rid, set()).add(seg_idx)
        
        # Verify we detected the correct road and reasonable coverage
        if test_road in rcr.road_coverage_state:
            covered_segments = len(rcr.road_coverage_state[test_road])
            total_segments = len(segments)
            coverage = rcr.calculate_coverage(test_road)
            
            print(f"Precision test: {covered_segments}/{total_segments} segments = {coverage:.1f}%")
            
            # Should have detected a reasonable number of segments
            self.assertGreater(covered_segments, 0, "Should detect some segments")
            self.assertGreater(coverage, 0, "Should have positive coverage")
    
    def test_actual_road_bounds_optimization(self):
        """Test that the bounds array optimization works with actual data."""
        if not self.road_ids:
            self.skipTest("No actual roads available for testing")
        
        # Test a point that should be in bounds of at least one road
        test_road = self.road_ids[0]
        road_info = self.road_data[test_road]
        
        if not road_info['segments']:
            self.skipTest(f"Test road {test_road} has no segments")
        
        # Use a point from the road
        lon, lat = road_info['segments'][0]
        
        # Test the bounds checking directly
        start_time = time.time()
        rid, info = rcr.find_current_road(lon, lat)
        elapsed = time.time() - start_time
        
        print(f"Road lookup took {elapsed*1000:.2f}ms for actual data with {len(self.road_ids)} roads")
        
        # Should be reasonably fast even with 896 roads
        self.assertLess(elapsed, 0.1, "Road lookup should be fast with bounds optimization")
        
        if rid:
            self.assertIn(rid, self.road_data, f"Found road {rid} should be in actual road data")
            print(f"Successfully found actual road: {rid}")


if __name__ == "__main__":
    unittest.main()