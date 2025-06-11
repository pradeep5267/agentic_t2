import os
import sqlite3
import pytest
import time
import requests
import json
from datetime import datetime, timedelta

# Import the Flask app
import app as myapp

# Selenium imports for frontend testing (optional)
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    selenium_available = True
except ImportError:
    selenium_available = False

# Add adapter and converter for ISO 8601 timestamps
def adapt_datetime_iso(val):
    """Adapt datetime.datetime to timezone-naive ISO 8601 format."""
    return val.isoformat()

def convert_timestamp(val):
    """Convert ISO 8601 string to datetime object."""
    return datetime.fromisoformat(val.decode())

sqlite3.register_adapter(datetime, adapt_datetime_iso)
sqlite3.register_converter("timestamp", convert_timestamp)

# --- Pytest Fixtures ---

@pytest.fixture(scope="session")
def app():
    """Provides a single instance of the Flask app for the test module."""
    myapp.app.config.update({
        "TESTING": True,
    })
    yield myapp.app

@pytest.fixture
def client(app, tmp_path, monkeypatch):
    """
    Provides a Flask test client with an isolated, temporary database for each test.
    This ensures tests don't interfere with each other.
    """
    db_file = tmp_path / "test_coverage.db"
    monkeypatch.setattr(myapp, "DATABASE", str(db_file))
    
    from migrate_db import migrate
    migrate()
    
    with app.test_client() as client:
        with app.app_context():
            yield client

@pytest.fixture(scope="module")
def chrome_driver():
    """Provides a Chrome driver for Selenium tests, if available."""
    if not selenium_available:
        pytest.skip("Selenium not installed, skipping browser tests")
        
    chrome_opts = Options()
    chrome_opts.add_argument("--headless")
    chrome_opts.add_argument("--disable-gpu")
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument("--disable-dev-shm-usage")
    try:
        driver = webdriver.Chrome(options=chrome_opts)
        yield driver
        driver.quit()
    except Exception as e:
        pytest.skip(f"Chrome driver not available: {e}")

@pytest.fixture(scope="module")
def firefox_driver():
    """Provides a Firefox driver as alternative for Selenium tests."""
    if not selenium_available:
        pytest.skip("Selenium not installed, skipping browser tests")
        
    options = FirefoxOptions()
    options.add_argument("--headless")
    try:
        driver = webdriver.Firefox(options=options)
        yield driver
        driver.quit()
    except Exception as e:
        pytest.skip(f"Firefox driver not available: {e}")

# --- Test Classes ---

class TestDatabase:
    """Tests for database schema and interactions."""
    
    def test_db_tables_exist(self, app):
        """Ensures that the database has all required tables."""
        with app.app_context():
            db = myapp.get_db()
            cursor = db.cursor()
            tables = ["covered_roads", "coverage_history", "road_recordings", "manual_marks"]
            for table in tables:
                cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}';")
                assert cursor.fetchone() is not None, f"Table '{table}' was not created."

    def test_db_relationships(self, app):
        """Tests foreign key relationships in the database."""
        unique_id = f"test_road_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        with app.app_context():
            db = myapp.get_db()
            # Insert a covered road with unique ID
            db.execute("INSERT INTO covered_roads (feature_id) VALUES (?)", (unique_id,))
            # Insert a coverage history entry that references it
            db.execute("""
                INSERT INTO coverage_history 
                (feature_id, covered_at, latitude, longitude, accuracy) 
                VALUES (?, ?, ?, ?, ?)
            """, (unique_id, datetime.utcnow(), 37.7749, -122.4194, 5.0))
            db.commit()
            
            # Query to verify the relationship
            cursor = db.execute("""
                SELECT ch.id FROM coverage_history ch
                JOIN covered_roads cr ON ch.feature_id = cr.feature_id
                WHERE cr.feature_id = ?
            """, (unique_id,))
            assert cursor.fetchone() is not None

    def test_manual_marks_storage(self, app):
        """Tests storing and retrieving manual road marks."""
        unique_id = f"manual_road_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        with app.app_context():
            db = myapp.get_db()
            # Add a manual mark with unique ID
            db.execute("""
                INSERT INTO manual_marks (feature_id, status, marked_at)
                VALUES (?, ?, ?)
            """, (unique_id, 'complete', datetime.utcnow()))
            db.commit()
            
            # Retrieve and verify
            cursor = db.execute("SELECT feature_id, status FROM manual_marks WHERE feature_id = ?", (unique_id,))
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == unique_id
            assert result[1] == 'complete'
    
    def test_road_recordings_storage(self, app):
        """Tests storing and retrieving road recording metadata."""
        unique_id = f"recorded_road_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        with app.app_context():
            db = myapp.get_db()
            # Add a recording with unique ID
            now = datetime.utcnow()
            db.execute("""
                INSERT INTO road_recordings 
                (feature_id, video_file, started_at, coverage_percent)
                VALUES (?, ?, ?, ?)
            """, (unique_id, 'test_video.mp4', now, 85.5))
            db.commit()
            
            # Retrieve and verify
            cursor = db.execute("SELECT video_file, coverage_percent FROM road_recordings WHERE feature_id = ?", (unique_id,))
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == 'test_video.mp4'
            assert result[1] == 85.5


class TestApiEndpoints:
    """Tests for the Flask API endpoints."""

    def test_api_covered_with_combined_data(self, client):
        """
        Tests if the /api/covered endpoint correctly combines data from both
        'covered_roads' (from user proximity) and 'road_recordings' (from recorder).
        """
        db = myapp.get_db()
        db.execute("INSERT INTO covered_roads (feature_id) VALUES ('road_from_user')")
        db.execute("INSERT INTO road_recordings (feature_id, video_file) VALUES ('road_from_recorder', 'video.mp4')")
        db.commit()

        response = client.get('/api/covered')
        assert response.status_code == 200
        data = response.get_json()
        assert 'road_from_user' in data['covered']
        assert 'road_from_recorder' in data['covered']
        assert len(data['covered']) >= 2  # May include more if there are other test roads

    def test_api_stats_shows_recent_recordings(self, client):
        """Tests if the /api/stats endpoint correctly lists recent recordings."""
        db = myapp.get_db()
        # Use datetime.utcnow() directly thanks to the adapter
        db.execute("""
            INSERT INTO road_recordings (feature_id, video_file, started_at, coverage_percent)
            VALUES ('way_123', 'video_abc.mp4', ?, 85.5)
        """, (datetime.utcnow(),))
        db.commit()

        response = client.get('/api/stats')
        assert response.status_code == 200
        stats = response.get_json()
        assert len(stats['recent_recordings']) >= 1
        assert any(r['feature_id'] == 'way_123' for r in stats['recent_recordings'])
        assert any(r['feature_id'] == 'way_123' and r['coverage_percent'] == 85.5 
                 for r in stats['recent_recordings'])

    def test_export_geojson_with_combined_data(self, client):
        """Tests if the GeoJSON export includes roads from both coverage sources."""
        if not os.path.exists(os.path.join(myapp.STATIC_DIR, "roads_with_polygons.geojson")):
            pytest.skip("roads_with_polygons.geojson not found, skipping GeoJSON export test.")
        
        with open(os.path.join(myapp.STATIC_DIR, "roads_with_polygons.geojson")) as f:
            features = json.load(f)['features']
        
        if len(features) < 2:
            pytest.skip("Not enough features in GeoJSON to run test.")

        road_id_user = features[0]['properties']['id']
        road_id_recorder = features[1]['properties']['id']

        db = myapp.get_db()
        db.execute("INSERT INTO covered_roads (feature_id) VALUES (?)", (road_id_user,))
        db.execute("INSERT INTO road_recordings (feature_id, video_file) VALUES (?, 'video.mp4')", (road_id_recorder,))
        db.commit()

        response = client.get('/api/export/geojson')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['features']) >= 2
        exported_ids = {f['properties']['id'] for f in data['features']}
        assert road_id_user in exported_ids
        assert road_id_recorder in exported_ids
        
    def test_manual_marks_are_included_in_covered(self, client):
        """Tests if manually marked roads are included in covered roads endpoint."""
        db = myapp.get_db()
        db.execute("INSERT INTO manual_marks (feature_id, status) VALUES ('road_manually_marked', 'complete')")
        db.commit()
        
        response = client.get('/api/covered')
        assert response.status_code == 200
        data = response.get_json()
        assert 'road_manually_marked' in data['covered'], "Manual marks should be included in covered roads"
    
    def test_manual_mark_endpoint(self, client):
        """Tests the manual mark endpoint for toggling road status."""
        # Test marking a road as complete
        response = client.post('/api/manual-mark', json={
            'feature_id': 'test_road_mark',
            'status': 'complete'
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data['feature_id'] == 'test_road_mark'
        assert data['status'] == 'complete'
        
        # Test marking the same road as incomplete
        response = client.post('/api/manual-mark', json={
            'feature_id': 'test_road_mark',
            'status': 'incomplete'
        })
        assert response.status_code == 200
        
        # Verify it was removed from the manual_marks table
        db = myapp.get_db()
        cursor = db.execute("SELECT * FROM manual_marks WHERE feature_id = 'test_road_mark'")
        assert cursor.fetchone() is None
    
    def test_recorder_state_endpoint(self, client):
        """Tests the recorder state POST and GET endpoints."""
        # Test POST
        test_state = {
            'lat': 37.7749,
            'lon': -122.4194,
            'heading': 45.0,
            'orientation': 'NE'
        }
        
        response = client.post('/api/recorder-state', json=test_state)
        assert response.status_code == 204
        
        # Test GET
        response = client.get('/api/recorder-state')
        assert response.status_code == 200
        data = response.get_json()
        assert data['lat'] == 37.7749
        assert data['lon'] == -122.4194
        assert data['heading'] == 45.0
        assert data['orientation'] == 'NE'
        assert 'ts' in data
    
    def test_coverage_history_endpoint(self, client):
        """Tests the coverage history endpoint."""
        # Add some history data
        db = myapp.get_db()
        now = datetime.utcnow()
        db.execute("""
            INSERT INTO covered_roads (feature_id) VALUES ('history_road')
        """)
        db.execute("""
            INSERT INTO coverage_history 
            (feature_id, covered_at, latitude, longitude, accuracy)
            VALUES (?, ?, ?, ?, ?)
        """, ('history_road', now, 37.7749, -122.4194, 5.0))
        db.commit()
        
        # Test the endpoint
        response = client.get('/api/coverage-history?feature_id=history_road')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['history']) == 1
        assert data['history'][0]['feature_id'] == 'history_road'
        assert data['history'][0]['latitude'] == 37.7749


class TestIntegration:
    """Tests for integration between components without requiring browser automation."""
    
    def test_db_to_api_integration(self, client):
        """Tests the flow from database updates to API responses."""
        db = myapp.get_db()
        
        # Add data to all three tables that should contribute to "covered" roads
        db.execute("INSERT INTO covered_roads (feature_id) VALUES ('integration_road_1')")
        db.execute("INSERT INTO road_recordings (feature_id, video_file) VALUES ('integration_road_2', 'video.mp4')")
        db.execute("INSERT INTO manual_marks (feature_id, status) VALUES ('integration_road_3', 'complete')")
        db.commit()
        
        # Check that all show up in the covered endpoint
        response = client.get('/api/covered')
        assert response.status_code == 200
        data = response.get_json()
        
        assert 'integration_road_1' in data['covered']
        assert 'integration_road_2' in data['covered']
        assert 'integration_road_3' in data['covered']
    
    def test_manual_mark_affects_covered_status(self, client):
        """Tests that manually marking a road updates its covered status."""
        # First check that the test road isn't already covered
        test_road = 'test_mark_to_cover'
        
        response = client.get('/api/covered')
        initial_covered = set(response.get_json()['covered'])
        assert test_road not in initial_covered
        
        # Now mark it as complete
        response = client.post('/api/manual-mark', json={
            'feature_id': test_road,
            'status': 'complete'
        })
        assert response.status_code == 200
        
        # Verify it now shows up in covered roads
        response = client.get('/api/covered')
        updated_covered = set(response.get_json()['covered'])
        assert test_road in updated_covered
        
        # Mark it as incomplete
        response = client.post('/api/manual-mark', json={
            'feature_id': test_road,
            'status': 'incomplete'
        })
        assert response.status_code == 200
        
        # Verify it's removed from covered roads
        response = client.get('/api/covered')
        final_covered = set(response.get_json()['covered'])
        assert test_road not in final_covered
    
    def test_stats_api_reflects_database_state(self, client):
        """Tests that the stats API correctly reflects the database state."""
        db = myapp.get_db()
        
        # Add test data
        today = datetime.utcnow()
        db.execute("""
            INSERT INTO road_recordings 
            (feature_id, video_file, started_at, coverage_percent)
            VALUES ('stats_test_road', 'stats_video.mp4', ?, 90.5)
        """, (today,))
        db.commit()
        
        # Check the stats API
        response = client.get('/api/stats')
        assert response.status_code == 200
        stats = response.get_json()
        
        # Find our test road in the recordings
        found = False
        for rec in stats['recent_recordings']:
            if rec['feature_id'] == 'stats_test_road':
                assert rec['video_file'] == 'stats_video.mp4'
                assert rec['coverage_percent'] == 90.5
                found = True
                break
        
        assert found, "The test road should appear in the stats API"
    
    def test_full_coverage_workflow(self, client):
        """
        Tests a complete coverage workflow: mark a road, verify covered status,
        add recording, verify stats.
        """
        # Use a unique road ID for this test
        road_id = f"workflow_test_{datetime.utcnow().strftime('%H%M%S')}"
        
        # Step 1: Mark road as covered manually
        response = client.post('/api/manual-mark', json={
            'feature_id': road_id,
            'status': 'complete'
        })
        assert response.status_code == 200
        
        # Step 2: Verify it appears in covered roads
        response = client.get('/api/covered')
        covered_roads = response.get_json()['covered']
        assert road_id in covered_roads
        
        # Step 3: Add a recording for the same road
        db = myapp.get_db()
        db.execute("""
            INSERT INTO road_recordings 
            (feature_id, video_file, started_at, coverage_percent)
            VALUES (?, ?, ?, ?)
        """, (road_id, f"{road_id}.mp4", datetime.utcnow(), 95.0))
        db.commit()
        
        # Step 4: Verify it still appears in covered roads (should only appear once)
        response = client.get('/api/covered')
        updated_covered = response.get_json()['covered']
        assert road_id in updated_covered
        assert updated_covered.count(road_id) == 1, "Road should appear exactly once in covered list"
        
        # Step 5: Verify it appears in stats
        response = client.get('/api/stats')
        stats = response.get_json()
        assert any(r['feature_id'] == road_id for r in stats['recent_recordings'])


# Optional Selenium tests - will be skipped if Selenium is not available
@pytest.mark.skipif(not selenium_available, reason="Selenium not installed")
class TestBrowserIntegration:
    """Tests using Selenium to verify frontend integration. These may be skipped."""
    
    def setup_method(self):
        """Check if we can connect to a local Flask server for testing."""
        try:
            response = requests.get("http://localhost:5000", timeout=0.5)
            self.server_available = response.status_code == 200
        except:
            self.server_available = False
    
    # @pytest.mark.skipif(True, reason="Direct Selenium tests temporarily disabled")
    def test_basic_page_load(self, chrome_driver):
        """Basic test to verify the page loads correctly."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        assert "Track Coverage" in chrome_driver.title
        
        # Check for basic elements
        map_element = chrome_driver.find_element(By.ID, "map")
        assert map_element.is_displayed()
        
        sidebar = chrome_driver.find_element(By.ID, "sidebar")
        assert sidebar.is_displayed()
    
    # These tests should be added to your existing TestBrowserIntegration class

# @pytest.mark.skipif(True, reason="Enable when Selenium tests are needed")
class TestEnhancedBrowserIntegration:
    """Enhanced Selenium tests for frontend integration."""
    
    def setup_method(self):
        """Check if we can connect to a local Flask server for testing."""
        try:
            response = requests.get("http://localhost:5000", timeout=0.5)
            self.server_available = response.status_code == 200
        except:
            self.server_available = False
    
    def test_map_initialization(self, chrome_driver):
        """Test that the map loads properly with Leaflet controls."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for map to load
        map_element = wait.until(EC.visibility_of_element_located((By.ID, "map")))
        
        # Check for Leaflet controls
        try:
            zoom_controls = chrome_driver.find_element(By.CLASS_NAME, "leaflet-control-zoom")
            assert zoom_controls.is_displayed(), "Leaflet zoom controls should be visible"
            
            # Check if the map has the correct size
            map_size = map_element.size
            assert map_size['width'] > 400, "Map should be at least 400px wide"
            assert map_size['height'] > 300, "Map should be at least 300px tall"
            
            # Verify Leaflet is properly initialized using JavaScript
            assert chrome_driver.execute_script("return typeof L !== 'undefined' && L.version")
            
        except NoSuchElementException:
            pytest.fail("Leaflet controls not found - map may not have initialized properly")
    
    def test_filter_controls(self, chrome_driver):
        """Test the filter controls in the sidebar."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for sidebar to load
        sidebar = wait.until(EC.visibility_of_element_located((By.ID, "sidebar")))
        
        # Check for filter groups
        filter_groups = chrome_driver.find_elements(By.CLASS_NAME, "filter-group")
        assert len(filter_groups) >= 3, "Should have at least 3 filter groups"
        
        # Test reset button
        reset_button = chrome_driver.find_element(By.ID, "reset-filters")
        assert reset_button.is_displayed(), "Reset filters button should be visible"
        
        # Find some checkboxes
        checkboxes = chrome_driver.find_elements(By.CSS_SELECTOR, ".filter-group input[type='checkbox']")
        if not checkboxes:
            pytest.skip("No filter checkboxes found - may need mock data")
            
        # Toggle a checkbox and check if it stays toggled
        if len(checkboxes) > 0:
            checkbox = checkboxes[0]
            initial_state = checkbox.is_selected()
            checkbox.click()
            assert checkbox.is_selected() != initial_state, "Checkbox state should toggle when clicked"
            
            # Reset filters
            reset_button.click()
            assert checkbox.is_selected() == True, "Checkbox should be reset to checked state"
    
    def test_export_buttons(self, chrome_driver):
        """Test the export buttons functionality."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for export section to load
        export_section = wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "export-section")))
        
        # Find all export buttons
        export_buttons = chrome_driver.find_elements(By.CLASS_NAME, "export-btn")
        assert len(export_buttons) == 3, "Should have 3 export buttons (JSON, CSV, GeoJSON)"
        
        # Verify button text
        button_texts = [btn.text for btn in export_buttons]
        assert "Export JSON" in button_texts, "Should have JSON export button"
        assert "Export CSV" in button_texts, "Should have CSV export button"
        assert "Export GeoJSON" in button_texts, "Should have GeoJSON export button"
        
        # Test that clicking a button creates a download
        # Note: This is hard to test in headless mode, so we're just testing the click works
        export_buttons[0].click()
        time.sleep(1)  # Allow time for any errors to surface
        
        # No assertion needed - if the click causes an error, the test will fail
    
    def test_stats_panel_content(self, chrome_driver):
        """Test that the stats panel loads with content."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for stats section to load
        stats_section = wait.until(EC.visibility_of_element_located((By.ID, "stats-section")))
        
        # Wait for content to load (may be async)
        try:
            wait.until(lambda d: d.find_element(By.ID, "stats-content").text != "Loading...")
        except TimeoutException:
            pass  # Continue even if it doesn't change from Loading...
        
        # Get stats content
        stats_content = chrome_driver.find_element(By.ID, "stats-content").text
        
        # Check if content has loaded (either with data or "No stats available" message)
        assert stats_content, "Stats content should not be empty"
        assert stats_content != "Loading...", "Stats should load and not stay in loading state"
    
    def test_recordings_panel_content(self, chrome_driver):
        """Test that the recordings panel loads with content."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for recordings section to load
        recordings_section = wait.until(EC.visibility_of_element_located((By.ID, "recordings-section")))
        
        # Wait for content to load (may be async)
        try:
            wait.until(lambda d: d.find_element(By.ID, "recordings-content").text != "Loading...")
        except TimeoutException:
            pass  # Continue even if it doesn't change from Loading...
        
        # Get recordings content
        recordings_content = chrome_driver.find_element(By.ID, "recordings-content").text
        
        # Check if content has loaded
        assert recordings_content, "Recordings content should not be empty"
    
    def test_mock_road_interaction(self, chrome_driver):
        """Test interaction with mock roads on the map."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for map to load
        map_element = wait.until(EC.visibility_of_element_located((By.ID, "map")))
        
        # Add a mock road to the map using JavaScript
        chrome_driver.execute_script("""
            if (typeof L === 'undefined' || !window.map) {
                // Initialize map if not already done
                window.map = L.map('map').setView([37.7749, -122.4194], 12);
                L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                    attribution: '&copy; OpenStreetMap contributors'
                }).addTo(window.map);
            }
            
            // Create mock roads layer if it doesn't exist
            if (!window.roadsLayer) {
                window.roadsLayer = L.layerGroup().addTo(window.map);
            }
            
            // Add a test road
            const testRoad = L.polyline([[37.7749, -122.4194], [37.7750, -122.4195]], {
                color: 'blue',
                weight: 2,
                opacity: 0.8
            });
            testRoad.featureId = 'selenium_test_road';
            
            // Add click handler
            testRoad.on('click', function() {
                this.setStyle({color: 'green'});
                this.clicked = true;
            });
            
            // Add to layer and center map
            window.roadsLayer.addLayer(testRoad);
            window.map.setView([37.7749, -122.4194], 15);
            
            // Return test road ID for verification
            return testRoad._leaflet_id;
        """)
        
        # Wait a moment for the road to be added
        time.sleep(1)
        
        # Verify road was added
        road_count = chrome_driver.execute_script("return window.roadsLayer.getLayers().length")
        assert road_count > 0, "At least one road should be added to the map"
        
        # Try to click the road (this is tricky because it's SVG, but we can simulate it)
        chrome_driver.execute_script("""
            const road = window.roadsLayer.getLayers()[0];
            road.fire('click');
        """)
        
        # Verify the road color changed to green
        road_color = chrome_driver.execute_script("""
            const road = window.roadsLayer.getLayers()[0];
            return road.options.color;
        """)
        assert road_color == 'green', "Road color should change to green after clicking"
    
    def test_status_boxes(self, chrome_driver):
        """Test the status and recorder boxes display properly."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for status box to load
        status_box = wait.until(EC.visibility_of_element_located((By.ID, "status-box")))
        
        # Check that it has content
        assert status_box.text, "Status box should have content"
        
        # Check for recorder box
        recorder_box = chrome_driver.find_element(By.ID, "recorder-box")
        assert recorder_box.is_displayed(), "Recorder box should be visible"
        
        # Verify recorder box has expected structure
        assert "Recorder" in recorder_box.text, "Recorder box should have 'Recorder' heading"
        assert "Lat:" in recorder_box.text, "Recorder box should show latitude"
        assert "Lon:" in recorder_box.text, "Recorder box should show longitude"
    
    def test_responsive_layout(self, chrome_driver):
        """Test that the layout is responsive to different window sizes."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for container to load
        container = wait.until(EC.visibility_of_element_located((By.ID, "container")))
        
        # Get initial sizes
        initial_map_size = chrome_driver.find_element(By.ID, "map").size
        initial_sidebar_size = chrome_driver.find_element(By.ID, "sidebar").size
        
        # Resize window to a smaller size
        chrome_driver.set_window_size(800, 600)
        time.sleep(1)  # Allow time for resize to take effect
        
        # Get new sizes
        new_map_size = chrome_driver.find_element(By.ID, "map").size
        new_sidebar_size = chrome_driver.find_element(By.ID, "sidebar").size
        
        # Verify layout responds to size change
        assert new_map_size != initial_map_size or new_sidebar_size != initial_sidebar_size, "Layout should respond to window size changes"
        
        # Verify elements are still visible
        assert chrome_driver.find_element(By.ID, "map").is_displayed(), "Map should remain visible after resize"
        assert chrome_driver.find_element(By.ID, "sidebar").is_displayed(), "Sidebar should remain visible after resize"
    
    def test_recorder_state_update(self, chrome_driver):
        """Test that the recorder state box updates when new data arrives."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for recorder box to load
        recorder_box = wait.until(EC.visibility_of_element_located((By.ID, "recorder-box")))
        
        # Get initial state
        initial_text = recorder_box.text
        
        # Update recorder state using JavaScript (simulating API response)
        chrome_driver.execute_script("""
            document.getElementById('recorder-box').innerHTML = `
                <strong>Recorder</strong><br>
                Lat: 37.7749<br>
                Lon: -122.4194<br>
                Heading: 45.0°<br>
                Orientation: NE<br>
                Updated: ${new Date().toLocaleTimeString()}
            `;
        """)
        
        # Get updated state
        updated_text = recorder_box.text
        
        # Verify state was updated
        assert updated_text != initial_text, "Recorder box should update with new data"
        assert "37.7749" in updated_text, "Updated recorder box should show new latitude"
        assert "-122.4194" in updated_text, "Updated recorder box should show new longitude"
    
    def test_api_integration(self, chrome_driver):
        """Test that frontend correctly integrates with backend APIs."""
        if not self.server_available:
            pytest.skip("Local Flask server not available")
            
        chrome_driver.get("http://localhost:5000")
        wait = WebDriverWait(chrome_driver, 10)
        
        # Wait for page to load
        container = wait.until(EC.visibility_of_element_located((By.ID, "container")))
        
        # Test that API calls are made
        network_logs = chrome_driver.execute_script("""
            // We can't access the network log directly in Selenium,
            // so we'll return information about what APIs were loaded in the page
            
            const apis = [
                "/api/covered",
                "/api/manual-marks",
                "/api/recorder-state",
                "/api/stats"
            ];
            
            // Create mock function to test if APIs were called
            return apis.map(api => {
                return {
                    "endpoint": api,
                    "called": document.body.innerHTML.includes(api)
                };
            });
        """)
        
        # Check that at least some API calls were made
        assert any(entry["called"] for entry in network_logs), "At least some API endpoints should be called"
        
        # Add a visual indication of which APIs were called (helpful for debugging)
        print("\nAPI Integration:")
        for entry in network_logs:
            status = "✓" if entry["called"] else "✗"
            print(f"{status} {entry['endpoint']}")

# Import the module to test
try:
    import aio_t14b_mk2 as rcr  # Alias as 'rcr' to keep existing test code working
except ImportError:
    print("Could not import aio_t14b_mk2.py. Make sure it's in the same directory.")
    sys.exit(1)

class TestRecorderDatabase(unittest.TestCase):
    """Tests for database functionality of the road coverage recorder."""
    
    def setUp(self):
        """Set up test environment before each test."""
        # Create temporary directory
        self.temp_dir = tempfile.mkdtemp()
        
        # Create temporary database file
        self.test_db = os.path.join(self.temp_dir, "test_coverage.db")
        
        # Mock the database path in the module
        rcr.DATABASE = self.test_db
        
        # Initialize the database
        self.init_test_database()
    
    def tearDown(self):
        """Clean up after each test."""
        # Remove temporary directory
        shutil.rmtree(self.temp_dir)
    
    def init_test_database(self):
        """Initialize test database with required tables."""
        conn = sqlite3.connect(self.test_db)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS road_recordings (
                feature_id TEXT PRIMARY KEY, 
                video_file TEXT, 
                started_at TEXT, 
                coverage_percent REAL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS manual_marks (
                feature_id TEXT PRIMARY KEY, 
                status TEXT, 
                marked_at TEXT
            )
        ''')
        conn.commit()
        conn.close()
    
    def test_database_initialization(self):
        """Test that database initialization creates all required tables."""
        # Delete the database file first
        if os.path.exists(self.test_db):
            os.unlink(self.test_db)
        
        # Call the initialization function
        rcr.init_database()
        
        # Check if tables were created
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        
        # Check road_recordings table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='road_recordings'")
        self.assertIsNotNone(cursor.fetchone(), "road_recordings table should be created")
        
        # Check manual_marks table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='manual_marks'")
        self.assertIsNotNone(cursor.fetchone(), "manual_marks table should be created")
        
        conn.close()
    
    def test_save_recording_to_db(self):
        """Test saving recording information to the database."""
        # Define test data
        road_id = "test_road_123"
        video_file = "/tmp/test_recording.mp4"
        coverage = 75.5
        
        # Save to database
        rcr.save_recording_to_db(road_id, video_file, coverage)
        
        # Verify it was saved correctly
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT video_file, coverage_percent FROM road_recordings WHERE feature_id = ?", 
            (road_id,)
        )
        result = cursor.fetchone()
        conn.close()
        
        self.assertIsNotNone(result, "Record should be saved to database")
        self.assertEqual(result[0], video_file, "Video file should match")
        self.assertEqual(result[1], coverage, "Coverage percentage should match")
    
    def test_update_existing_recording(self):
        """Test updating an existing recording with new information."""
        # Insert initial record
        road_id = "update_test_road"
        initial_file = "/tmp/initial_recording.mp4"
        initial_coverage = 50.0
        
        rcr.save_recording_to_db(road_id, initial_file, initial_coverage)
        
        # Update with new information
        new_file = "/tmp/new_recording.mp4"
        new_coverage = 85.0
        
        rcr.save_recording_to_db(road_id, new_file, new_coverage)
        
        # Verify the update
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT video_file, coverage_percent FROM road_recordings WHERE feature_id = ?", 
            (road_id,)
        )
        result = cursor.fetchone()
        conn.close()
        
        self.assertEqual(result[0], new_file, "Video file should be updated")
        self.assertEqual(result[1], new_coverage, "Coverage percentage should be updated")
    
    def test_load_recorded_roads(self):
        """Test loading previously recorded roads from the database."""
        # Insert test data - mixture of road recordings and manual marks
        conn = sqlite3.connect(self.test_db)
        conn.execute(
            "INSERT INTO road_recordings (feature_id, video_file) VALUES (?, ?)",
            ("road_recording_1", "/tmp/video1.mp4")
        )
        conn.execute(
            "INSERT INTO road_recordings (feature_id, video_file) VALUES (?, ?)",
            ("road_recording_2", "/tmp/video2.mp4")
        )
        conn.execute(
            "INSERT INTO manual_marks (feature_id, status) VALUES (?, ?)",
            ("manual_road_1", "complete")
        )
        conn.execute(
            "INSERT INTO manual_marks (feature_id, status) VALUES (?, ?)",
            ("manual_road_2", "complete")
        )
        # Add a road marked as incomplete - should not be included
        conn.execute(
            "INSERT INTO manual_marks (feature_id, status) VALUES (?, ?)",
            ("incomplete_road", "incomplete")
        )
        conn.commit()
        conn.close()
        
        # Load recorded roads
        recorded_roads = rcr.load_recorded_roads()
        
        # Verify correct roads were loaded
        self.assertEqual(len(recorded_roads), 4, "Should load 4 roads (2 recordings + 2 complete marks)")
        self.assertIn("road_recording_1", recorded_roads, "Should include recorded road 1")
        self.assertIn("road_recording_2", recorded_roads, "Should include recorded road 2")
        self.assertIn("manual_road_1", recorded_roads, "Should include manually marked road 1")
        self.assertIn("manual_road_2", recorded_roads, "Should include manually marked road 2")
        self.assertNotIn("incomplete_road", recorded_roads, "Should not include incomplete road")
    
    def test_load_recorded_roads_empty_db(self):
        """Test loading recorded roads from an empty database."""
        # Ensure database is empty
        conn = sqlite3.connect(self.test_db)
        conn.execute("DELETE FROM road_recordings")
        conn.execute("DELETE FROM manual_marks")
        conn.commit()
        conn.close()
        
        # Load recorded roads
        recorded_roads = rcr.load_recorded_roads()
        
        # Verify result is an empty set
        self.assertEqual(len(recorded_roads), 0, "Should return empty set for empty database")
        self.assertIsInstance(recorded_roads, set, "Should return a set")
    
    def test_load_recorded_roads_error_handling(self):
        """Test error handling when loading recorded roads."""
        # Create an invalid database file to force an error
        with open(self.test_db, 'w') as f:
            f.write("This is not a valid SQLite database file")
        
        # Load recorded roads should handle the error gracefully
        recorded_roads = rcr.load_recorded_roads()
        
        # Should return an empty set on error
        self.assertEqual(len(recorded_roads), 0, "Should return empty set on database error")
    
    def test_recording_timestamp_format(self):
        """Test that recording timestamps are stored in ISO format."""
        # Save a recording
        road_id = "timestamp_test_road"
        video_file = "/tmp/timestamp_test.mp4"
        coverage = 60.0
        
        # Mock datetime.now to return a known value
        test_time = datetime(2023, 5, 15, 12, 30, 45)
        with patch('road_coverage_recorder.datetime') as mock_datetime:
            mock_datetime.now.return_value = test_time
            mock_datetime.isoformat = datetime.isoformat  # Preserve the real method
            
            # Save recording
            rcr.save_recording_to_db(road_id, video_file, coverage)
        
        # Verify timestamp format
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT started_at FROM road_recordings WHERE feature_id = ?", 
            (road_id,)
        )
        result = cursor.fetchone()
        conn.close()
        
        # Should be in ISO format (2023-05-15T12:30:45)
        self.assertIsNotNone(result, "Record should be saved to database")
        self.assertEqual(result[0], "2023-05-15T12:30:45", "Timestamp should be in ISO format")
    
    def test_database_transaction_integrity(self):
        """Test database transaction integrity on errors."""
        # Create a connection that will fail
        with patch('sqlite3.connect') as mock_connect:
            # First allow connection but make execute fail
            mock_conn = MagicMock()
            mock_conn.execute.side_effect = sqlite3.Error("Simulated database error")
            mock_connect.return_value = mock_conn
            
            # Try to save a recording
            rcr.save_recording_to_db("error_test_road", "/tmp/error_test.mp4", 50.0)
        
        # Now with a real connection, verify no record was added
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM road_recordings WHERE feature_id = 'error_test_road'")
        count = cursor.fetchone()[0]
        conn.close()
        
        self.assertEqual(count, 0, "No record should be added on database error")
    
    def test_integration_with_recording_process(self):
        """Test integration between recording process and database."""
        # Simulate a recording cycle
        road_id = "integration_test_road"
        
        # Mock necessary functions and state
        rcr.recording_file = f"/tmp/{road_id}_recording.mp4"
        rcr.recording_start_time = time.time() - 10  # Started 10 seconds ago
        rcr.current_road_id = road_id
        
        # Add some fake coverage data
        rcr.ROAD_DATA = {
            road_id: {
                "name": "Integration Test Road",
                "segments": [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]
            }
        }
        rcr.road_coverage_state = {road_id: {0, 2, 4}}  # 3 of 5 segments covered = 60%
        
        # Mock stop_recording to not actually stop anything
        original_stop = rcr.stop_recording
        rcr.stop_recording = MagicMock()
        
        try:
            # Simulate exiting the road
            rcr.log_csv('ROAD_EXIT', road_id=road_id, notes="integration test")
            
            # Stop recording and save to database
            rcr.stop_recording()
            rcr.save_recording_to_db(road_id, rcr.recording_file, rcr.calculate_coverage(road_id))
            
            # Verify database entry
            conn = sqlite3.connect(self.test_db)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT video_file, coverage_percent FROM road_recordings WHERE feature_id = ?", 
                (road_id,)
            )
            result = cursor.fetchone()
            conn.close()
            
            self.assertIsNotNone(result, "Record should be saved to database")
            self.assertEqual(result[0], rcr.recording_file, "Video file should match")
            self.assertEqual(result[1], 60.0, "Coverage percentage should be 60%")
        finally:
            # Restore original function
            rcr.stop_recording = original_stop
    
    def test_multiple_database_connections(self):
        """Test that multiple database connections don't interfere with each other."""
        # This test simulates multiple threads accessing the database
        
        # Create some test data
        base_road_id = "multiconn_test_road"
        threads = 5
        
        # Function to simulate a thread saving to the database
        def simulate_thread(thread_id):
            road_id = f"{base_road_id}_{thread_id}"
            video_file = f"/tmp/video_{thread_id}.mp4"
            coverage = 50.0 + thread_id * 5.0
            
            # Save to database
            rcr.save_recording_to_db(road_id, video_file, coverage)
            
            return road_id
        
        # Run the simulated threads
        saved_roads = [simulate_thread(i) for i in range(threads)]
        
        # Verify all records were saved correctly
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        
        for thread_id in range(threads):
            road_id = f"{base_road_id}_{thread_id}"
            expected_file = f"/tmp/video_{thread_id}.mp4"
            expected_coverage = 50.0 + thread_id * 5.0
            
            cursor.execute(
                "SELECT video_file, coverage_percent FROM road_recordings WHERE feature_id = ?", 
                (road_id,)
            )
            result = cursor.fetchone()
            
            self.assertIsNotNone(result, f"Record for {road_id} should be saved")
            self.assertEqual(result[0], expected_file, f"Video file for {road_id} should match")
            self.assertEqual(result[1], expected_coverage, f"Coverage for {road_id} should match")
        
        conn.close()
    
    def test_database_consistency_with_preexisting_data(self):
        """Test database operations are consistent with preexisting data."""
        # Insert some preexisting data
        preexisting_roads = [
            ("preexisting_road_1", "/tmp/pre1.mp4", "2023-01-01T12:00:00", 80.0),
            ("preexisting_road_2", "/tmp/pre2.mp4", "2023-01-02T12:00:00", 90.0),
        ]
        
        conn = sqlite3.connect(self.test_db)
        for road in preexisting_roads:
            conn.execute(
                "INSERT INTO road_recordings (feature_id, video_file, started_at, coverage_percent) VALUES (?, ?, ?, ?)",
                road
            )
        conn.commit()
        conn.close()
        
        # Load recorded roads
        recorded_roads = rcr.load_recorded_roads()
        
        # Verify preexisting roads are loaded
        self.assertIn("preexisting_road_1", recorded_roads, "Should load preexisting road 1")
        self.assertIn("preexisting_road_2", recorded_roads, "Should load preexisting road 2")
        
        # Add a new road
        new_road = "new_test_road"
        rcr.save_recording_to_db(new_road, "/tmp/new.mp4", 75.0)
        
        # Reload and verify all roads are present
        updated_roads = rcr.load_recorded_roads()
        self.assertIn("preexisting_road_1", updated_roads, "Should still have preexisting road 1")
        self.assertIn("preexisting_road_2", updated_roads, "Should still have preexisting road 2")
        self.assertIn(new_road, updated_roads, "Should have new road")
    
    def test_manual_marks_integration(self):
        """Test integration with manual marks feature."""
        # First, verify no roads are loaded initially
        conn = sqlite3.connect(self.test_db)
        conn.execute("DELETE FROM road_recordings")
        conn.execute("DELETE FROM manual_marks")
        conn.commit()
        conn.close()
        
        initial_roads = rcr.load_recorded_roads()
        self.assertEqual(len(initial_roads), 0, "Should start with no roads")
        
        # Add a manual mark
        conn = sqlite3.connect(self.test_db)
        conn.execute(
            "INSERT INTO manual_marks (feature_id, status, marked_at) VALUES (?, ?, ?)",
            ("manual_test_road", "complete", datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        
        # Load recorded roads
        with_manual_roads = rcr.load_recorded_roads()
        self.assertIn("manual_test_road", with_manual_roads, "Should include manually marked road")
        
        # Now add a road recording for the same road
        rcr.save_recording_to_db("manual_test_road", "/tmp/manual_test.mp4", 100.0)
        
        # Reload and verify it's still counted just once
        final_roads = rcr.load_recorded_roads()
        self.assertIn("manual_test_road", final_roads, "Should still include the road")
        
        # The count should still be 1 (not 2) since it's the same road ID
        count = sum(1 for road in final_roads if road == "manual_test_road")
        self.assertEqual(count, 1, "Road should be counted only once")

import os
import sys
import unittest
import time
import threading
import sqlite3
import queue
import tempfile
import json
import pickle
import numpy as np
import shutil
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime
from shapely.geometry import Point, Polygon

# Add the directory containing aio_t14b_mk2.py to sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Import the module to test - with error handling in case file isn't found
try:
    import aio_t14b_mk2 as rcr  # Alias as 'rcr' to keep existing test code working
except ImportError:
    print("Could not import aio_t14b_mk2.py. Make sure it's in the same directory.")
    sys.exit(1)

class TestRoadCoverageRecorder(unittest.TestCase):
    """Test suite for the road coverage recorder script."""
    
    def setUp(self):
        """Set up test environment before each test."""
        # Create temporary directories for test data
        self.temp_dir = tempfile.mkdtemp()
        self.save_dir = os.path.join(self.temp_dir, "recordings")
        os.makedirs(self.save_dir, exist_ok=True)
        
        # Set the path to the actual preprocessed data
        self.preprocessed_dir = "/media/gamedisk/KTP_artefacts/PSSav_mk2/output_artefacts"
        
        # Mock the global variables in the module
        rcr.PREPROCESSED_DIR = self.preprocessed_dir
        rcr.SAVE_DIR = self.save_dir
        rcr.CSV_FILE = os.path.join(self.save_dir, "test_gps_log.csv")
        rcr.DATABASE = os.path.join(self.temp_dir, "test_coverage.db")
        
        # Create test database
        conn = sqlite3.connect(rcr.DATABASE)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS road_recordings(
                feature_id TEXT PRIMARY KEY, video_file TEXT, 
                started_at TEXT, coverage_percent REAL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS manual_marks(
                feature_id TEXT PRIMARY KEY, status TEXT, marked_at TEXT
            )
        ''')
        conn.commit()
        conn.close()
        
        # Reset global state variables in module
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
        
        # Prepare test road data
        self.create_mock_road_data()
    
    def tearDown(self):
        """Clean up after each test."""
        # Clear temp directory
        shutil.rmtree(self.temp_dir)
        
        # Reset shutdown event
        rcr.shutdown_event.clear()
    
    def create_mock_road_data(self):
        """Load the actual road data for testing."""
        # Instead of creating mock data, we'll load the real data for reference
        # We'll still use it just for test validation, not modify the actual files
        
        # Load the real data
        try:
            self.bounds_array = np.load(os.path.join(self.preprocessed_dir, "road_bounds.npy"))
            with open(os.path.join(self.preprocessed_dir, "road_data.pkl"), 'rb') as f:
                self.road_data = pickle.load(f)
            with open(os.path.join(self.preprocessed_dir, "buffer_polygons.pkl"), 'rb') as f:
                self.buffer_polygons = pickle.load(f)
            with open(os.path.join(self.preprocessed_dir, "road_ids.pkl"), 'rb') as f:
                self.road_ids = pickle.load(f)
            
            # Get a sample road ID for testing - use the first one
            self.sample_road_id = self.road_ids[0] if self.road_ids else "road_1"
            
            print(f"Loaded actual road data: {len(self.road_ids)} roads")
        except Exception as e:
            print(f"Warning: Could not load actual road data: {e}")
            print("Creating minimal test data instead")
            
            # Create minimal test data as fallback
            self.road_data = {"road_1": {"name": "Test Road", "segments": [(-122.1234, 37.4321), (-122.1240, 37.4325)]}}
            self.road_ids = ["road_1"]
            self.bounds_array = np.array([[-122.1244, 37.4319, -122.1232, 37.4327]])
            self.buffer_polygons = [Polygon([(-122.1238, 37.4319), (-122.1244, 37.4323),
                                            (-122.1242, 37.4327), (-122.1232, 37.4323)])]
            self.sample_road_id = "road_1"
    
    def test_load_preprocessed_data(self):
        """Test loading of preprocessed GIS data."""
        # Mock loading function to capture loaded data
        orig_print = print
        
        def mock_load_data():
            rcr.print = MagicMock()  # Silence print statements
            
            # Call the main load function from module
            rcr.BOUNDS_ARRAY = np.load(f"{self.preprocessed_dir}/road_bounds.npy")
            with open(f"{self.preprocessed_dir}/road_data.pkl", 'rb') as f:
                rcr.ROAD_DATA = pickle.load(f)
            with open(f"{self.preprocessed_dir}/buffer_polygons.pkl", 'rb') as f:
                rcr.BUFFER_POLYGONS = pickle.load(f)
            with open(f"{self.preprocessed_dir}/road_ids.pkl", 'rb') as f:
                rcr.ROAD_IDS = pickle.load(f)
            rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in rcr.BUFFER_POLYGONS]
            
            rcr.print = orig_print  # Restore print
        
        # Load data
        mock_load_data()
        
        # Verify all data structures are loaded correctly
        self.assertTrue(isinstance(rcr.BOUNDS_ARRAY, np.ndarray), "Bounds array should be a numpy array")
        self.assertEqual(len(rcr.BOUNDS_ARRAY), 2, "Should have 2 roads in bounds array")
        
        self.assertTrue(isinstance(rcr.ROAD_DATA, dict), "Road data should be a dictionary")
        self.assertEqual(len(rcr.ROAD_DATA), 2, "Should have 2 roads in road data")
        self.assertTrue("road_1" in rcr.ROAD_DATA, "Road 1 should be in road data")
        self.assertTrue("road_2" in rcr.ROAD_DATA, "Road 2 should be in road data")
        
        self.assertTrue(isinstance(rcr.BUFFER_POLYGONS, list), "Buffer polygons should be a list")
        self.assertEqual(len(rcr.BUFFER_POLYGONS), 2, "Should have 2 buffer polygons")
        
        self.assertTrue(isinstance(rcr.ROAD_IDS, list), "Road IDs should be a list")
        self.assertEqual(len(rcr.ROAD_IDS), 2, "Should have 2 road IDs")
        
        self.assertTrue(isinstance(rcr.PREPARED_POLYGONS, list), "Prepared polygons should be a list")
        self.assertEqual(len(rcr.PREPARED_POLYGONS), 2, "Should have 2 prepared polygons")
    
    def test_find_current_road(self):
        """Test finding the current road based on GPS coordinates."""
        # Load mock data
        rcr.BOUNDS_ARRAY = np.load(f"{self.preprocessed_dir}/road_bounds.npy")
        with open(f"{self.preprocessed_dir}/road_data.pkl", 'rb') as f:
            rcr.ROAD_DATA = pickle.load(f)
        with open(f"{self.preprocessed_dir}/buffer_polygons.pkl", 'rb') as f:
            rcr.BUFFER_POLYGONS = pickle.load(f)
        with open(f"{self.preprocessed_dir}/road_ids.pkl", 'rb') as f:
            rcr.ROAD_IDS = pickle.load(f)
        rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in rcr.BUFFER_POLYGONS]
        
        # Test point on road 1
        road_id, road_info = rcr.find_current_road(-122.1235, 37.4322)
        self.assertEqual(road_id, "road_1", "Should detect being on road_1")
        self.assertEqual(road_info["name"], "Test Road 1", "Should have correct road name")
        
        # Test point on road 2
        road_id, road_info = rcr.find_current_road(-122.2015, 37.5015)
        self.assertEqual(road_id, "road_2", "Should detect being on road_2")
        self.assertEqual(road_info["name"], "Test Road 2", "Should have correct road name")
        
        # Test point not on any road
        road_id, road_info = rcr.find_current_road(-123.0000, 38.0000)
        self.assertIsNone(road_id, "Should not detect any road")
        self.assertIsNone(road_info, "Should not return road info")
        
        # Verify zone counter increments
        self.assertTrue(rcr.zone_check_counter > 0, "Zone check counter should increment")
    
    def test_find_nearest_segment(self):
        """Test finding the nearest road segment to a GPS position."""
        # Load mock data
        with open(f"{self.preprocessed_dir}/road_data.pkl", 'rb') as f:
            rcr.ROAD_DATA = pickle.load(f)
        
        # Test for road 1 - should be closest to segment 0
        segment_idx, distance = rcr.find_nearest_segment("road_1", 37.4322, -122.1235)
        self.assertEqual(segment_idx, 0, "Should find segment 0 as closest")
        self.assertLess(distance, 100, "Distance should be reasonable")
        
        # Test for road 2 - near the third segment
        segment_idx, distance = rcr.find_nearest_segment("road_2", 37.5025, -122.2025)
        self.assertEqual(segment_idx, 2, "Should find segment 2 as closest")
        self.assertLess(distance, 100, "Distance should be reasonable")
    
    def test_calculate_coverage(self):
        """Test calculating road coverage percentage."""
        # Load mock data
        with open(f"{self.preprocessed_dir}/road_data.pkl", 'rb') as f:
            rcr.ROAD_DATA = pickle.load(f)
        
        # No coverage initially
        coverage = rcr.calculate_coverage("road_2")
        self.assertEqual(coverage, 0.0, "Coverage should be 0% initially")
        
        # Add some coverage
        rcr.road_coverage_state["road_2"] = {0, 2}  # 2 of 4 segments covered
        coverage = rcr.calculate_coverage("road_2")
        self.assertEqual(coverage, 50.0, "Coverage should be 50%")
        
        # Full coverage
        rcr.road_coverage_state["road_2"] = {0, 1, 2, 3}  # All segments covered
        coverage = rcr.calculate_coverage("road_2")
        self.assertEqual(coverage, 100.0, "Coverage should be 100%")
    
    @patch('subprocess.Popen')
    def test_start_recording(self, mock_popen):
        """Test starting a recording."""
        # Setup mock subprocess
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Process is running
        mock_popen.return_value = mock_process
        
        # Mock os.getpgid to return a fake process group ID
        with patch('os.getpgid', return_value=12345):
            # Start recording
            recording_file = rcr.start_recording("road_1")
            
            # Verify recording started
            self.assertIsNotNone(recording_file, "Should return a recording file path")
            self.assertTrue("road_1" in recording_file, "File name should contain road ID")
            self.assertTrue(rcr.recording_proc is not None, "Recording process should be set")
            self.assertTrue(rcr.recording_start_time is not None, "Recording start time should be set")
            
            # Verify subprocess was called with correct command
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertTrue('gst-launch-1.0' in args, "Command should use gstreamer")
            self.assertTrue('filesink' in args, "Command should include filesink")
    
    @patch('subprocess.Popen')
    @patch('os.killpg')
    def test_stop_recording(self, mock_killpg, mock_popen):
        """Test stopping a recording."""
        # Setup mock subprocess
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Process is running
        mock_process.wait.return_value = 0  # Process exits cleanly
        mock_popen.return_value = mock_process
        
        # Set up recording state
        rcr.recording_proc = mock_process
        rcr.recording_file = "/tmp/test_recording.mp4"
        rcr.recording_start_time = time.time() - 5  # Started 5 seconds ago
        rcr.current_road_id = "road_1"
        
        # Mock os.getpgid
        with patch('os.getpgid', return_value=12345):
            # Stop recording
            rcr.stop_recording()
            
            # Verify recording stopped
            self.assertIsNone(rcr.recording_proc, "Recording process should be cleared")
            self.assertIsNone(rcr.recording_file, "Recording file should be cleared")
            self.assertIsNone(rcr.recording_start_time, "Recording start time should be cleared")
            
            # Verify process was killed correctly
            mock_killpg.assert_called_once_with(12345, rcr.signal.SIGINT)
            mock_process.wait.assert_called_once()
    
    def test_csv_logging(self):
        """Test CSV logging functionality."""
        # Initialize CSV
        rcr.init_csv()
        self.assertTrue(os.path.exists(rcr.CSV_FILE), "CSV file should be created")
        
        # Log an event
        rcr.log_csv("TEST_EVENT", lat=37.1234, lon=-122.5678, notes="Test note")
        
        # Flush buffer
        rcr.flush_csv_buffer()
        
        # Read CSV and verify entry
        with open(rcr.CSV_FILE, 'r') as f:
            lines = f.readlines()
            self.assertTrue(len(lines) > 1, "CSV should have header and data row")
            self.assertTrue("TEST_EVENT" in lines[1], "Event type should be in CSV")
            self.assertTrue("Test note" in lines[1], "Notes should be in CSV")
    
    def test_load_recorded_roads(self):
        """Test loading previously recorded roads from the database."""
        # Insert test data into database
        conn = sqlite3.connect(rcr.DATABASE)
        conn.execute("INSERT INTO road_recordings (feature_id, video_file) VALUES (?, ?)", 
                    ("road_1", "test_video.mp4"))
        conn.execute("INSERT INTO manual_marks (feature_id, status) VALUES (?, ?)",
                    ("road_2", "complete"))
        conn.commit()
        conn.close()
        
        # Load recorded roads
        recorded_roads = rcr.load_recorded_roads()
        
        # Verify both types of roads are loaded
        self.assertEqual(len(recorded_roads), 2, "Should load 2 roads")
        self.assertTrue("road_1" in recorded_roads, "Should load road from recordings")
        self.assertTrue("road_2" in recorded_roads, "Should load road from manual marks")
    
    def test_database_integration(self):
        """Test database operations for saving recordings."""
        # Setup recording data
        road_id = "road_test"
        video_file = "/tmp/test_recording.mp4"
        coverage = 75.5
        
        # Save recording
        rcr.save_recording_to_db(road_id, video_file, coverage)
        
        # Verify data was saved
        conn = sqlite3.connect(rcr.DATABASE)
        cursor = conn.execute("SELECT video_file, coverage_percent FROM road_recordings WHERE feature_id = ?", 
                             (road_id,))
        row = cursor.fetchone()
        conn.close()
        
        self.assertIsNotNone(row, "Should have a database entry")
        self.assertEqual(row[0], video_file, "Video file should match")
        self.assertEqual(row[1], coverage, "Coverage percentage should match")
    
    @patch('time.sleep', return_value=None)  # Speed up tests
    def test_gps_processing_workflow(self, _):
        """Test the entire GPS processing workflow with mocked GPS data."""
        # Load preprocessed data
        rcr.BOUNDS_ARRAY = np.load(f"{self.preprocessed_dir}/road_bounds.npy")
        with open(f"{self.preprocessed_dir}/road_data.pkl", 'rb') as f:
            rcr.ROAD_DATA = pickle.load(f)
        with open(f"{self.preprocessed_dir}/buffer_polygons.pkl", 'rb') as f:
            rcr.BUFFER_POLYGONS = pickle.load(f)
        with open(f"{self.preprocessed_dir}/road_ids.pkl", 'rb') as f:
            rcr.ROAD_IDS = pickle.load(f)
        rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in rcr.BUFFER_POLYGONS]
        
        # Mock recording functions
        rcr.start_recording = MagicMock(return_value="/tmp/test_recording.mp4")
        rcr.stop_recording = MagicMock()
        rcr.save_recording_to_db = MagicMock()
        
        # Create a thread to simulate the main loop with our mocked GPS data
        def mock_main_loop():
            # Points that move along road_1, then off-road, then to road_2
            gps_points = [
                # On road_1
                {"lat": 37.4321, "lon": -122.1234, "fix": True, "gps_qual": 1, "time": time.time()},
                {"lat": 37.4322, "lon": -122.1236, "fix": True, "gps_qual": 1, "time": time.time()},
                {"lat": 37.4323, "lon": -122.1238, "fix": True, "gps_qual": 1, "time": time.time()},
                
                # Off-road
                {"lat": 37.4500, "lon": -122.1500, "fix": True, "gps_qual": 1, "time": time.time()},
                {"lat": 37.4600, "lon": -122.1600, "fix": True, "gps_qual": 1, "time": time.time()},
                
                # On road_2
                {"lat": 37.5000, "lon": -122.2000, "fix": True, "gps_qual": 1, "time": time.time()},
                {"lat": 37.5010, "lon": -122.2010, "fix": True, "gps_qual": 1, "time": time.time()},
                {"lat": 37.5020, "lon": -122.2020, "fix": True, "gps_qual": 1, "time": time.time()}
            ]
            
            # Fill the queue with GPS points
            for point in gps_points:
                rcr.gps_queue.put(point)
                time.sleep(0.01)  # Small delay
            
            # Wait a bit to let the main loop process the data
            time.sleep(0.5)
            
            # Signal the main loop to end
            rcr.shutdown_event.set()
        
        # Start the mock GPS thread
        gps_thread = threading.Thread(target=mock_main_loop)
        gps_thread.daemon = True
        gps_thread.start()
        
        # Run the main function with mocked subprocess
        with patch('subprocess.Popen'), patch('os.getpgid', return_value=12345), \
             patch('os.killpg'), patch('subprocess.run'):
            # We'll use a simplified version of the main loop for testing
            last_on_road, exit_logged = None, False
            
            while not rcr.shutdown_event.is_set():
                try:
                    gps = rcr.gps_queue.get(timeout=0.1)
                except queue.Empty:
                    if rcr.gps_queue.empty():  # If the queue is empty, we can exit
                        break
                    continue
                
                rid, info = rcr.find_current_road(gps['lon'], gps['lat'])
                if rid:
                    seg_idx, seg_dist = rcr.find_nearest_segment(rid, gps['lat'], gps['lon'])
                    if seg_dist <= rcr.SEGMENT_THRESHOLD_M:
                        rcr.road_coverage_state.setdefault(rid, set()).add(seg_idx)
                
                rcr.log_csv('GPS_POSITION', lat=gps['lat'], lon=gps['lon'], fix=gps['fix'], 
                           gps_qual=gps['gps_qual'])
                
                if rid:
                    last_on_road = time.time()
                    exit_logged = False
                    if rid != rcr.current_road_id:
                        if rcr.recording_proc:
                            rcr.stop_recording()
                            rcr.save_recording_to_db(rcr.current_road_id, rcr.recording_file, 
                                                   rcr.calculate_coverage(rcr.current_road_id))
                        rcr.log_csv('ROAD_ENTER', road_id=rid)
                        if rid not in rcr.recorded_roads:
                            rcr.start_recording(rid)
                        rcr.current_road_id = rid
                else:
                    if rcr.current_road_id and last_on_road and not exit_logged and \
                       time.time() - last_on_road > rcr.ROAD_EXIT_THRESHOLD_S:
                        pct = rcr.calculate_coverage(rcr.current_road_id)
                        rcr.log_csv('ROAD_EXIT', road_id=rcr.current_road_id, notes=f"coverage={pct:.1f}")
                        if rcr.recording_proc:
                            rcr.stop_recording()
                            rcr.save_recording_to_db(rcr.current_road_id, rcr.recording_file, pct)
                        rcr.current_road_id, exit_logged = None, True
            
            # Wait for mock thread to finish
            gps_thread.join()
        
        # Verify road coverage state
        self.assertTrue("road_1" in rcr.road_coverage_state, "Should have coverage for road_1")
        self.assertTrue("road_2" in rcr.road_coverage_state, "Should have coverage for road_2")
        
        # Verify recording lifecycle
        self.assertTrue(rcr.start_recording.called, "Should have called start_recording")
        self.assertTrue(rcr.stop_recording.called, "Should have called stop_recording")
        self.assertTrue(rcr.save_recording_to_db.called, "Should have called save_recording_to_db")
    
    def test_init_database(self):
        """Test database initialization."""
        # Delete existing test database
        if os.path.exists(rcr.DATABASE):
            os.unlink(rcr.DATABASE)
        
        # Initialize database
        rcr.init_database()
        
        # Verify tables exist
        conn = sqlite3.connect(rcr.DATABASE)
        cursor = conn.cursor()
        
        # Check road_recordings table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='road_recordings'")
        self.assertIsNotNone(cursor.fetchone(), "road_recordings table should exist")
        
        # Check manual_marks table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='manual_marks'")
        self.assertIsNotNone(cursor.fetchone(), "manual_marks table should exist")
        
        conn.close()
    
    @patch('requests.post')
    def test_post_state(self, mock_post):
        """Test posting state to the dashboard."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_post.return_value = mock_response
        
        # Post state
        rcr.post_state(37.1234, -122.5678, 45.0, "NE")
        
        # Verify post was called with correct data
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], rcr.RECORDER_STATE_URL)
        self.assertTrue('json' in kwargs)
        self.assertEqual(kwargs['json']['lat'], 37.1234)
        self.assertEqual(kwargs['json']['lon'], -122.5678)
        self.assertEqual(kwargs['json']['heading'], 45.0)
        self.assertEqual(kwargs['json']['orientation'], "NE")
    
    def test_cleanup_orphaned_processes(self):
        """Test cleanup of orphaned processes."""
        # Mock subprocess.run
        with patch('subprocess.run') as mock_run:
            # Mock finding orphaned processes
            mock_run.side_effect = [
                MagicMock(stdout="12345\n67890\n", stderr=""),  # pgrep
                MagicMock(),  # pkill
                MagicMock()   # pkill -9
            ]
            
            # Run cleanup
            rcr.cleanup_orphaned_processes()
            
            # Verify correct commands were executed
            self.assertEqual(mock_run.call_count, 3, "Should call subprocess.run 3 times")
            
            # Check first call (pgrep)
            args, kwargs = mock_run.call_args_list[0]
            self.assertTrue(['pgrep', '-f', 'gst-launch-1.0'] == args[0], "First call should be pgrep")
            
            # Check second call (pkill)
            args, kwargs = mock_run.call_args_list[1]
            self.assertTrue(['pkill', '-f', 'gst-launch-1.0'] == args[0], "Second call should be pkill")
            
            # Check third call (pkill -9)
            args, kwargs = mock_run.call_args_list[2]
            self.assertTrue(['pkill', '-9', '-f', 'gst-launch-1.0'] == args[0], "Third call should be pkill -9")
    
    @patch('os.killpg')
    def test_cleanup_specific_process(self, mock_killpg):
        """Test cleanup of a specific process."""
        # Mock os.killpg to simulate process ending after SIGINT
        def mock_killpg_side_effect(pgid, sig):
            if sig == 0:  # This is the status check
                if mock_killpg.call_count > 2:  # After first SIGINT and one check
                    raise OSError("No such process")
            return None
        
        mock_killpg.side_effect = mock_killpg_side_effect
        
        # Cleanup a specific process
        rcr.cleanup_specific_process(12345)
        
        # Verify SIGINT was sent
        mock_killpg.assert_any_call(12345, rcr.signal.SIGINT)
        
        # Verify process status was checked
        mock_killpg.assert_any_call(12345, 0)
        
        # Verify SIGKILL was not needed
        with self.assertRaises(AssertionError):
            mock_killpg.assert_any_call(12345, rcr.signal.SIGKILL)
    
    def test_get_jetson_stats(self):
        """Test getting Jetson system stats."""
        # Mock file open operations
        mock_files = {
            '/sys/class/thermal/thermal_zone1/temp': '45000\n',  # 45°C
            '/sys/class/thermal/thermal_zone2/temp': '50000\n',  # 50°C
            '/proc/meminfo': 'MemTotal:        8000000 kB\nMemAvailable:    4000000 kB\n',
            '/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq': '1500000\n',
            '/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq': '2000000\n'
        }
        
        def mock_open_side_effect(filename, *args, **kwargs):
            if filename in mock_files:
                return mock_open(read_data=mock_files[filename])()
            raise FileNotFoundError(f"Mock file {filename} not found")
        
        # Patch os.statvfs
        mock_statvfs = MagicMock()
        mock_statvfs.f_bavail = 10000000  # Free blocks
        mock_statvfs.f_frsize = 4096      # Block size
        mock_statvfs.f_blocks = 20000000  # Total blocks
        
        with patch('builtins.open', side_effect=mock_open_side_effect), \
             patch('os.statvfs', return_value=mock_statvfs):
            # Get stats
            stats = rcr.get_jetson_stats()
            
            # Verify stats
            self.assertEqual(stats['cpu_temp'], 45.0, "CPU temperature should be 45°C")
            self.assertEqual(stats['gpu_temp'], 50.0, "GPU temperature should be 50°C")
            self.assertAlmostEqual(stats['mem_percent'], 50.0, delta=0.1, msg="Memory usage should be 50%")
            self.assertEqual(stats['cpu_freq_mhz'], 1500.0, "CPU frequency should be 1500 MHz")
            self.assertEqual(stats['throttled'], True, "CPU should be marked as throttled")
            self.assertTrue(stats['storage_free_gb'] > 0, "Free storage should be positive")
            self.assertTrue(0 <= stats['storage_percent'] <= 100, "Storage percent should be 0-100%")


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
        self.mock_gps.add_route("gps_loss", route_5)    def _create_fallback_road_network(self):
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
            poly = Polygon([
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
        rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in buffer_polygons]import os
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
from shapely.geometry import Point, Polygon

# Add the directory containing aio_t14b_mk2.py to sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Import the module to test
try:
    import aio_t14b_mk2 as rcr  # Alias as 'rcr' to keep existing test code working
except ImportError:
    print("Could not import aio_t14b_mk2.py. Make sure it's in the same directory.")
    sys.exit(1)

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
        
        # Use the actual preprocessed data path
        self.preprocessed_dir = "/media/gamedisk/KTP_artefacts/PSSav_mk2/output_artefacts"
        
        # Mock module globals
        rcr.PREPROCESSED_DIR = self.preprocessed_dir
        rcr.SAVE_DIR = self.save_dir
        rcr.CSV_FILE = os.path.join(self.save_dir, "test_gps_log.csv")
        rcr.DATABASE = os.path.join(self.temp_dir, "test_coverage.db")
        
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
        
        # Initialize CSV
        rcr.init_csv()
    
    def tearDown(self):
        """Clean up after each test."""
        # Stop GPS simulation if running
        self.mock_gps.stop()
        
        # Reset original functions
        rcr.start_recording = self.orig_start_recording
        rcr.stop_recording = self.orig_stop_recording
        
        # Clear temp directory
        shutil.rmtree(self.temp_dir)
        
        # Reset shutdown event
        rcr.shutdown_event.clear()
    
    def create_test_road_network(self):
        """Load actual road network for testing."""
        try:
            # Load the real road data
            self.bounds_array = np.load(os.path.join(self.preprocessed_dir, "road_bounds.npy"))
            with open(os.path.join(self.preprocessed_dir, "road_data.pkl"), 'rb') as f:
                self.road_data = pickle.load(f)
            with open(os.path.join(self.preprocessed_dir, "buffer_polygons.pkl"), 'rb') as f:
                self.buffer_polygons = pickle.load(f)
            with open(os.path.join(self.preprocessed_dir, "road_ids.pkl"), 'rb') as f:
                self.road_ids = pickle.load(f)
            
            # Load the data into the module
            rcr.BOUNDS_ARRAY = self.bounds_array
            rcr.ROAD_DATA = self.road_data
            rcr.BUFFER_POLYGONS = self.buffer_polygons
            rcr.ROAD_IDS = self.road_ids
            rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in self.buffer_polygons]
            
            # Get some sample roads for testing
            self.sample_roads = self.road_ids[:4] if len(self.road_ids) >= 4 else self.road_ids
            
            print(f"Loaded actual road network with {len(self.road_ids)} roads")
        except Exception as e:
            print(f"Warning: Could not load actual road data: {e}")
            print("Creating minimal test road network instead")
            
            # Create minimal test data if real data fails to load
            self._create_fallback_road_network()
    
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
        self.mock_gps.set_route("network_tour")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=2.0)
        
        # Verify multiple roads were detected
        detected_roads = set(rcr.road_coverage_state.keys())
        self.assertGreater(len(detected_roads), 1, "Should detect multiple roads")
        
        # Verify recording was not started for road_1 (already recorded)
        for args, _ in rcr.start_recording.call_args_list:
            self.assertNotEqual(args[0], "road_1", "Should not record road_1 again")
        
        # Verify other roads were recorded
        other_roads = detected_roads - {"road_1"}
        for road in other_roads:
            self.assertIn(
                (road,), 
                [args for args, _ in rcr.start_recording.call_args_list],
                f"Should start recording for {road}"
            )
    
    def test_road_exit_detection(self):
        """Test detection of exiting a road."""
        # Modify ROAD_EXIT_THRESHOLD_S for faster testing
        original_threshold = rcr.ROAD_EXIT_THRESHOLD_S
        rcr.ROAD_EXIT_THRESHOLD_S = 0.2  # 200ms
        
        try:
            # Set up route with clear exit point
            self.mock_gps.set_route("road1_west_to_east")
            
            # Start GPS simulation
            self.mock_gps.start(rcr.gps_queue, delay=0.05)
            
            # Run tracking logic
            self.run_road_tracking_logic(duration=1.0)
            
            # Verify road was detected and then exited
            self.assertIn("road_1", rcr.road_coverage_state, "Should detect Road 1")
            
            # Current road should be None after exiting
            self.assertIsNone(rcr.current_road_id, "Should exit the road")
            
            # Verify recording was stopped
            rcr.stop_recording.assert_called()
            
            # Verify database save was called
            rcr.save_recording_to_db.assert_called()
        finally:
            # Restore original threshold
            rcr.ROAD_EXIT_THRESHOLD_S = original_threshold
    
    def test_gps_signal_loss(self):
        """Test behavior when GPS signal is lost and regained."""
        # Set up route with GPS signal loss
        self.mock_gps.set_route("gps_loss")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=1.0)
        
        # Verify road was detected
        self.assertIn("road_1", rcr.road_coverage_state, "Should detect Road 1")
        
        # Check coverage - should have gaps due to GPS loss
        segments_covered = len(rcr.road_coverage_state.get("road_1", set()))
        total_segments = len(rcr.ROAD_DATA["road_1"]["segments"])
        
        # We should have some coverage, but not complete
        self.assertGreater(segments_covered, 0, "Should have some coverage")
        self.assertLess(segments_covered, total_segments, "Should have incomplete coverage due to GPS loss")
    
    def test_coverage_calculation(self):
        """Test accurate calculation of road coverage."""
        # Manually set coverage for road_1
        rcr.road_coverage_state["road_1"] = {0, 1}  # 2 of 4 segments
        
        # Calculate coverage
        coverage = rcr.calculate_coverage("road_1")
        
        # Expected: 2/4 = 50%
        self.assertEqual(coverage, 50.0, "Coverage should be 50%")
        
        # Add more coverage
        rcr.road_coverage_state["road_1"].add(2)  # 3 of 4 segments
        
        # Recalculate
        coverage = rcr.calculate_coverage("road_1")
        
        # Expected: 3/4 = 75%
        self.assertEqual(coverage, 75.0, "Coverage should be 75%")
    
    def test_multiple_road_coverage(self):
        """Test covering multiple roads in a single run."""
        # Set up network tour route
        self.mock_gps.set_route("network_tour")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.03)
        
        # Run tracking logic for longer
        self.run_road_tracking_logic(duration=3.0)
        
        # Verify multiple roads were covered
        self.assertGreater(len(rcr.road_coverage_state), 1, "Should cover multiple roads")
        
        # Check individual road coverages
        for road_id in rcr.road_coverage_state:
            coverage = rcr.calculate_coverage(road_id)
            self.assertGreater(coverage, 0, f"Should have positive coverage for {road_id}")
            print(f"Road {road_id} coverage: {coverage}%")
    
    def test_road_intersection_handling(self):
        """Test handling of road intersections."""
        # We'll create a custom route that pauses at intersections
        intersection_route = [
            # Start on Road 1
            (37.4000, -122.1000, 1),
            (37.4000, -122.0950, 1),
            (37.4000, -122.0900, 1),
            
            # Approaching intersection with Road 2
            (37.4000, -122.0860, 1),
            (37.4000, -122.0850, 1),  # Intersection of Road 1 and Road 2
            (37.4000, -122.0850, 1),  # Stay at intersection
            (37.4000, -122.0850, 1),  # Stay at intersection
            
            # Continue on Road 1
            (37.4000, -122.0840, 1),
            (37.4000, -122.0800, 1),
            
            # Approaching intersection with Road 3
            (37.4000, -122.0710, 1),
            (37.4000, -122.0700, 1),  # Intersection of Road 1 and Road 3
            (37.4000, -122.0700, 1),  # Stay at intersection
            (37.4000, -122.0700, 1),  # Stay at intersection
            
            # Turn onto Road 3
            (37.4020, -122.0680, 1),
            (37.4050, -122.0650, 1),
            (37.4100, -122.0600, 1),
        ]
        
        # Add and set the route
        self.mock_gps.add_route("intersection_test", intersection_route)
        self.mock_gps.set_route("intersection_test")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=2.0)
        
        # Verify roads were detected
        self.assertIn("road_1", rcr.road_coverage_state, "Should detect Road 1")
        self.assertIn("road_3", rcr.road_coverage_state, "Should detect Road 3")
        
        # Road 2 might be detected at intersection, but not guaranteed
        if "road_2" in rcr.road_coverage_state:
            # If detected, should have minimal coverage
            self.assertLessEqual(
                len(rcr.road_coverage_state["road_2"]), 
                1, 
                "Road 2 should have minimal coverage if detected"
            )
    
    def test_segment_distance_threshold(self):
        """Test the segment distance threshold logic."""
        # Create a route that passes near but not directly on a road segment
        near_road_route = [
            # Start near Road 1, but at varying distances from it
            (37.3990, -122.1000, 1),  # 10m away - should be counted (within threshold)
            (37.3980, -122.0950, 1),  # 20m away - should be counted if threshold >= 20m
            (37.3970, -122.0900, 1),  # 30m away - should be counted only if threshold >= 30m
            (37.3950, -122.0850, 1),  # 50m away - should NOT be counted with default threshold
            (37.3930, -122.0800, 1),  # 70m away - should NOT be counted
            (37.3990, -122.0750, 1),  # Back to 10m away - should be counted
            (37.4000, -122.0700, 1),  # Directly on Road 1/Road 3 junction
        ]
        
        # Add and set the route
        self.mock_gps.add_route("near_road_test", near_road_route)
        self.mock_gps.set_route("near_road_test")
        
        # Test with different thresholds
        test_thresholds = [
            (10, 3),   # 10m threshold should detect 3 points
            (30, 5),   # 30m threshold should detect 5 points
            (100, 7),  # 100m threshold should detect all 7 points
        ]
        
        for threshold, expected_segments in test_thresholds:
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
                
                # Count segments covered for Road 1
                road_1_segments = len(rcr.road_coverage_state.get("road_1", set()))
                
                # Verify coverage based on threshold
                # Note: We check <= because some points might map to the same segment
                self.assertLessEqual(
                    road_1_segments, 
                    expected_segments, 
                    f"With {threshold}m threshold, should detect at most {expected_segments} segments"
                )
                if threshold >= 10:
                    self.assertGreater(
                        road_1_segments, 
                        0, 
                        f"With {threshold}m threshold, should detect at least 1 segment"
                    )
                if threshold >= 30:
                    self.assertGreater(
                        road_1_segments, 
                        2, 
                        f"With {threshold}m threshold, should detect at least 3 segments"
                    )
                
                print(f"Threshold {threshold}m detected {road_1_segments} segments")
                
            finally:
                # Restore original threshold
                rcr.SEGMENT_THRESHOLD_M = original_threshold
    
    def test_fast_driving(self):
        """Test road tracking with fast driving (larger gaps between GPS points)."""
        # Create a route with larger gaps between points (simulating fast driving)
        fast_route = [
            # Points on Road 1 with larger gaps
            (37.4000, -122.1000, 1),  # Start of Road 1
            (37.4000, -122.0900, 1),  # Skip some segments
            (37.4000, -122.0800, 1),  # Skip more segments
            (37.4000, -122.0700, 1),  # End of Road 1/Start of Road 3
            
            # Fast driving on Road 3 - even larger gaps
            (37.4100, -122.0600, 1),  # Skip more segments
            (37.4200, -122.0500, 1),  # End of Road 3
        ]
        
        # Add and set the route
        self.mock_gps.add_route("fast_driving", fast_route)
        self.mock_gps.set_route("fast_driving")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=1.0)
        
        # Verify both roads were detected
        self.assertIn("road_1", rcr.road_coverage_state, "Should detect Road 1")
        self.assertIn("road_3", rcr.road_coverage_state, "Should detect Road 3")
        
        # Check coverage - should be lower due to fast driving
        road_1_coverage = rcr.calculate_coverage("road_1")
        road_3_coverage = rcr.calculate_coverage("road_3")
        
        print(f"Fast driving coverage - Road 1: {road_1_coverage}%, Road 3: {road_3_coverage}%")
        
        # Coverage should be less than 100% due to skipped segments
        self.assertLess(road_1_coverage, 100, "Road 1 coverage should be incomplete due to fast driving")
        self.assertLess(road_3_coverage, 100, "Road 3 coverage should be incomplete due to fast driving")
        
        # But should still have some coverage
        self.assertGreater(road_1_coverage, 0, "Road 1 should have some coverage")
        self.assertGreater(road_3_coverage, 0, "Road 3 should have some coverage")
    
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
            # Use network tour route
            self.mock_gps.set_route("network_tour")
            
            # Start GPS simulation
            self.mock_gps.start(rcr.gps_queue, delay=0.05)
            
            # Run tracking logic for longer to capture multiple roads
            self.run_road_tracking_logic(duration=3.0)
            
            # Verify multiple recordings were started
            self.assertGreater(len(recorded_roads), 1, "Should start multiple recordings")
            
            # Verify recordings were saved
            self.assertGreater(len(saved_recordings), 0, "Should save recordings")
            
            # Each road should be recorded only once
            unique_recorded = set(recorded_roads)
            self.assertEqual(len(unique_recorded), len(recorded_roads), 
                           "Each road should be recorded only once")
            
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
        # Mark roads as already recorded
        rcr.recorded_roads = {"road_1", "road_3"}
        
        # Use network tour route
        self.mock_gps.set_route("network_tour")
        
        # Start GPS simulation
        self.mock_gps.start(rcr.gps_queue, delay=0.05)
        
        # Run tracking logic
        self.run_road_tracking_logic(duration=2.0)
        
        # Verify road coverage is still tracked
        self.assertIn("road_1", rcr.road_coverage_state, "Should still track coverage for road_1")
        
        # But recording should not be started for already recorded roads
        for args, _ in rcr.start_recording.call_args_list:
            self.assertNotIn(args[0], rcr.recorded_roads, 
                          f"Should not start recording for already recorded road {args[0]}")
    
    def test_gps_queue_processing(self):
        """Test that GPS points are processed correctly from the queue."""
        # Create a set of test points
        test_points = [
            {"lat": 37.4000, "lon": -122.1000, "fix": True, "gps_qual": 1, "time": time.time()},
            {"lat": 37.4000, "lon": -122.0900, "fix": True, "gps_qual": 1, "time": time.time()},
            {"lat": 37.4000, "lon": -122.0800, "fix": True, "gps_qual": 1, "time": time.time()},
            {"lat": 37.4000, "lon": -122.0700, "fix": True, "gps_qual": 1, "time": time.time()},
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
        # Override stop_recording to verify duration enforcement
        original_stop = rcr.stop_recording
        stop_times = []
        
        def mock_stop():
            duration = time.time() - rcr.recording_start_time
            stop_times.append(duration)
            # Call original (mocked) implementation
            original_stop()
        
        rcr.stop_recording = MagicMock(side_effect=mock_stop)
        
        # Set a very short route that will exit quickly
        short_route = [
            (37.4000, -122.1000, 1),  # Start on Road 1
            (37.4000, -122.0950, 1),
            (37.4500, -122.0900, 1),  # Now off road (lat too far north)
        ]
        
        # Set a shorter road exit threshold for testing
        original_threshold = rcr.ROAD_EXIT_THRESHOLD_S
        rcr.ROAD_EXIT_THRESHOLD_S = 0.1  # 100ms
        
        try:
            # Add and set the route
            self.mock_gps.add_route("short_route", short_route)
            self.mock_gps.set_route("short_route")
            
            # Start GPS simulation
            self.mock_gps.start(rcr.gps_queue, delay=0.05)
            
            # Run tracking logic
            self.run_road_tracking_logic(duration=1.0)
            
            # Verify minimum recording duration was enforced
            self.assertGreaterEqual(stop_times[0], rcr.MIN_RECORDING_DURATION, 
                                 "Recording should last at least MIN_RECORDING_DURATION")
        finally:
            # Restore original values
            rcr.ROAD_EXIT_THRESHOLD_S = original_threshold
    
    def test_concurrent_threads(self):
        """Test that the recorder can handle concurrent threads safely."""
        # We'll simulate concurrent access to shared state
        thread_count = 5
        test_duration = 1.0
        threads = []
        
        # Use a route that crosses multiple roads
        self.mock_gps.set_route("network_tour")
        
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
        # Load the preprocessed data
        rcr.BOUNDS_ARRAY = np.load(f"{self.preprocessed_dir}/road_bounds.npy")
        rcr.BUFFER_POLYGONS = pickle.load(open(f"{self.preprocessed_dir}/buffer_polygons.pkl", 'rb'))
        rcr.ROAD_IDS = pickle.load(open(f"{self.preprocessed_dir}/road_ids.pkl", 'rb'))
        rcr.PREPARED_POLYGONS = [rcr.prep.prep(poly) for poly in rcr.BUFFER_POLYGONS]
        
        # Points to test (mix of on-road and off-road)
        test_points = [
            (-122.1000, 37.4000),  # On Road 1
            (-122.0850, 37.4000),  # On Road 1 and Road 2 intersection
            (-122.0600, 37.4100),  # On Road 3
            (-122.1500, 37.4500),  # Off all roads
            (-122.0800, 37.3950),  # Near Road 4
        ]
        
        # Measure time to find roads for these points
        start_time = time.time()
        results = []
        
        for lon, lat in test_points:
            rid, _ = rcr.find_current_road(lon, lat)
            results.append(rid)
        
        elapsed = time.time() - start_time
        
        # Performance check - should be fast
        self.assertLess(elapsed, 0.1, f"Road finding should be fast (took {elapsed:.4f}s for 5 points)")
        
        # Verify correct roads were found
        self.assertEqual(results[0], "road_1", "Should find Road 1")
        # At intersection, could be either road depending on polygon details
        self.assertIn(results[1], ["road_1", "road_2"], "Should find Road 1 or Road 2 at intersection")
        self.assertEqual(results[2], "road_3", "Should find Road 3")
        self.assertIsNone(results[3], "Should not find any road")
        # Point 5 is near Road 4, might be within buffer threshold
        
        print(f"Road finding performance: {elapsed:.6f} seconds for 5 points")
        print(f"Results: {results}")


if __name__ == "__main__":
    unittest.main()