import os
import sqlite3
import pytest
import tempfile
import json
import threading
import time
import requests
import socket
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# Import the Flask app and get_db function
import app as myapp

# -------------------------------------------------------------------
# Fixtures & Unit Tests
# -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def override_database(tmp_path, monkeypatch):
    """
    Overrides the DATABASE path in the app module to use a temporary file
    for each test run, ensuring isolation.
    """
    db_file = tmp_path / "test_coverage.db"
    monkeypatch.setattr(myapp, "DATABASE", str(db_file))
    # Ensure migration runs before tests
    from migrate_db import migrate
    migrate()
    yield

@pytest.fixture()
def client():
    """
    Provides a Flask test client with application context.
    """
    myapp.app.config['TESTING'] = True
    with myapp.app.test_client() as client:
        with myapp.app.app_context():
            yield client

def test_get_db_creates_table():
    """get_db must create the covered_roads table."""
    with myapp.app.app_context():
        db = myapp.get_db()
        cur = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='covered_roads';"
        )
        assert cur.fetchone() is not None

def test_api_get_empty(client):
    """GET /api/covered on an empty DB returns []"""
    resp = client.get('/api/covered')
    assert resp.status_code == 200
    assert resp.get_json() == {'covered': []}

def test_api_post_and_get(client):
    """POST an ID, then GET must include it."""
    new_id = 'way_test_1'
    post_resp = client.post('/api/covered', json={'id': new_id})
    assert post_resp.status_code == 200
    assert post_resp.get_json().get('status') == 'ok'

    get_resp = client.get('/api/covered')
    assert new_id in get_resp.get_json()['covered']

def test_api_post_duplicate(client):
    """Posting the same ID twice should not duplicate."""
    dup = 'way_dup'
    client.post('/api/covered', json={'id': dup})
    client.post('/api/covered', json={'id': dup})
    covered = client.get('/api/covered').get_json()['covered']
    assert covered.count(dup) == 1

def test_api_post_missing_id(client):
    """POST /api/covered without 'id' must return 400."""
    resp = client.post('/api/covered', json={})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()

def test_root_and_static_routes(client):
    """Root, static CSS/JS and GeoJSON routes must return 200."""
    idx = client.get('/')
    assert idx.status_code == 200
    assert b'<html' in idx.data.lower()

    # Adjust these paths to match your static assets
    assert client.get('/static/leaflet.css').status_code == 200
    assert client.get('/static/leaflet.js').status_code == 200

    geo = client.get('/static/roads_with_polygons.geojson')
    assert geo.status_code == 200
    assert geo.content_type in ('application/json', 'application/geo+json')

# -------------------------------------------------------------------
# Coverage history tests
# -------------------------------------------------------------------

def test_coverage_history_tracking(client):
    """Test that coverage history is tracked with location data"""
    road_id = 'test_road_123'
    location_data = {
        'id': road_id,
        'lat': 51.5074,
        'lon': -0.1278,
        'accuracy': 10.5
    }
    # First coverage
    resp1 = client.post('/api/covered', json=location_data)
    assert resp1.status_code == 200

    history_resp = client.get('/api/coverage-history')
    history = history_resp.get_json()['history']
    assert len(history) == 1
    entry = history[0]
    assert entry['feature_id'] == road_id
    assert entry['latitude'] == 51.5074
    assert entry['longitude'] == -0.1278
    assert entry['accuracy'] == 10.5

    # Cover again
    time.sleep(0.01)
    client.post('/api/covered', json=location_data)
    history2 = client.get('/api/coverage-history').get_json()['history']
    assert len(history2) == 2

def test_coverage_history_filtering(client):
    """Test coverage history filtering options"""
    roads = ['road_1', 'road_2', 'road_3']
    for r in roads:
        client.post('/api/covered', json={'id': r})
    # filter by feature_id
    resp = client.get(f'/api/coverage-history?feature_id={roads[0]}')
    hist = resp.get_json()['history']
    assert all(h['feature_id'] == roads[0] for h in hist)
    # limit param
    resp2 = client.get('/api/coverage-history?limit=2')
    assert len(resp2.get_json()['history']) <= 2

# -------------------------------------------------------------------
# Manual‐mark endpoint tests
# -------------------------------------------------------------------

def test_manual_mark_and_list(client):
    """Test POST /api/manual-mark and GET /api/manual-marks"""
    fid = 'manual_road_1'
    # initially empty
    assert client.get('/api/manual-marks').get_json() == {}
    # mark complete
    resp = client.post('/api/manual-mark', json={'feature_id': fid, 'status': 'complete'})
    assert resp.status_code == 200
    assert resp.get_json() == {'feature_id': fid, 'status': 'complete'}
    # list
    marks = client.get('/api/manual-marks').get_json()
    assert marks == {fid: 'complete'}
    # mark incomplete (delete)
    resp2 = client.post('/api/manual-mark', json={'feature_id': fid, 'status': 'incomplete'})
    assert resp2.status_code == 200
    assert client.get('/api/manual-marks').get_json() == {}

# -------------------------------------------------------------------
# Recorder‐state endpoint tests
# -------------------------------------------------------------------

def test_recorder_state_in_memory(client):
    """Test POST and GET /api/recorder-state in memory"""
    state = {'lat': 10.0, 'lon': 20.0, 'heading': 123.4, 'orientation': 'NW'}
    # POST state
    post = client.post('/api/recorder-state', json=state)
    assert post.status_code == 204
    # GET state
    get = client.get('/api/recorder-state')
    ret = get.get_json()
    for k in state:
        assert ret[k] == state[k]
    assert 'ts' in ret  # timestamp was added

# -------------------------------------------------------------------
# Export tests
# -------------------------------------------------------------------

def test_export_json(client):
    """Test JSON export of covered roads"""
    test_roads = ['road_a', 'road_b', 'road_c']
    for rid in test_roads:
        client.post('/api/covered', json={'id': rid})
    resp = client.get('/api/export/json')
    assert resp.status_code == 200
    assert resp.content_type == 'application/json'
    data = resp.get_json()
    assert data['total'] == len(test_roads)
    exported = {r['feature_id'] for r in data['covered_roads']}
    assert exported == set(test_roads)

def test_export_csv(client):
    """Test CSV export of covered roads"""
    test_roads = ['x','y']
    for rid in test_roads:
        client.post('/api/covered', json={'id': rid})
    resp = client.get('/api/export/csv')
    assert resp.status_code == 200
    assert resp.content_type.startswith('text/csv')
    text = resp.data.decode('utf-8').splitlines()
    assert text[0] == 'feature_id,first_covered,last_covered,coverage_count'
    assert len(text) == len(test_roads) + 1

def test_export_geojson(client):
    """Test GeoJSON export of covered roads"""
    geo_resp = client.get('/static/roads_with_polygons.geojson')
    if geo_resp.status_code != 200:
        pytest.skip("GeoJSON not available")
    all_geo = geo_resp.get_json()
    if not all_geo.get('features'):
        pytest.skip("No features to test")
    fid = all_geo['features'][0]['properties']['id']
    client.post('/api/covered', json={'id': fid})
    resp = client.get('/api/export/geojson')
    assert resp.status_code == 200
    assert resp.content_type == 'application/geo+json'
    out = resp.get_json()
    assert out['type'] == 'FeatureCollection'
    assert len(out['features']) == 1
    assert out['features'][0]['properties']['id'] == fid

def test_frontend_manual_mark_toggle(live_server):
    """
    Simulate a click on the first 'allowed' road polyline,
    verify it appears in /api/manual-marks and the line color updates.
    """
    host = live_server
    # Set up headless Chrome
    chrome_opts = Options()
    chrome_opts.add_argument("--headless")
    chrome_opts.add_argument("--disable-gpu")
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=chrome_opts)

    try:
        driver.get(f"{host}/")
        wait = WebDriverWait(driver, 10)

        # Wait for GeoJSON and roadsLayer to be initialized
        wait.until(lambda d: d.execute_script(
            "return window.roadsLayer && window.roadsLayer.getLayers().length > 0"
        ))

        # Find the first 'allowed' road featureId via JS
        fid = driver.execute_script("""
            for (let layer of window.roadsLayer.getLayers()) {
                if (layer.featureId 
                    && layer.featureStatus === 'allowed'
                    && layer.options.color === 'blue') {
                    return layer.featureId;
                }
            }
            return null;
        """)
        assert fid, "No clickable 'allowed' road found"

        # Click that layer programmatically
        driver.execute_script(f"""
            const layer = window.roadsLayer.getLayers()
                          .find(l => l.featureId === "{fid}");
            layer.fire('click');
        """)

        # Give a moment for the POST to complete
        time.sleep(1)

        # Verify via API that manual-marks now contains fid
        resp = requests.get(f"{host}/api/manual-marks")
        assert resp.status_code == 200
        marks = resp.json()
        assert fid in marks and marks[fid] == 'complete'

        # Also verify the polyline's color updated to green
        color = driver.execute_script(f"""
            const layer = window.roadsLayer.getLayers()
                          .find(l => l.featureId === "{fid}");
            return layer.options.color;
        """)
        assert color == 'green'

        # Click again to toggle to 'incomplete'
        driver.execute_script(f"""
            const layer = window.roadsLayer.getLayers()
                          .find(l => l.featureId === "{fid}");
            layer.fire('click');
        """)
        time.sleep(1)

        resp2 = requests.get(f"{host}/api/manual-marks")
        marks2 = resp2.json()
        assert fid not in marks2

    finally:
        driver.quit()


def test_recorder_state_widget_updates(live_server):
    """
    POST a fake recorder state, then load the dashboard and confirm
    the recorder box displays the new values.
    """
    host = live_server
    # POST a sample state
    sample = {
        "lat": 51.5007,
        "lon": -0.1246,
        "heading": 45.0,
        "orientation": "NE",
        "ts": datetime.utcnow().isoformat()
    }
    post = requests.post(f"{host}/api/recorder-state", json=sample)
    assert post.status_code == 204

    # Start headless browser
    chrome_opts = Options()
    chrome_opts.add_argument("--headless")
    chrome_opts.add_argument("--disable-gpu")
    driver = webdriver.Chrome(options=chrome_opts)

    try:
        driver.get(f"{host}/")
        wait = WebDriverWait(driver, 10)

        # Wait until recorder-box textContent reflects our sample
        wait.until(lambda d: "Heading: 45.0°" in d.find_element(By.ID, "recorder-box").text)

        txt = driver.find_element(By.ID, "recorder-box").text
        assert "Lat: 51.5007" in txt
        assert "Lon: -0.1246" in txt
        assert "Heading: 45.0°" in txt
        assert "Orientation: NE" in txt

    finally:
        driver.quit()


# -------------------------------------------------------------------
# Fixtures for overriding paths
# -------------------------------------------------------------------
@ pytest.fixture(autouse=True)
def override_paths(tmp_path, monkeypatch):
    # Redirect SAVE_DIR and CSV_FILE to temp directory
    temp_dir = tmp_path / "recordings"
    temp_dir.mkdir()
    monkeypatch.setattr(recorder, 'SAVE_DIR', str(temp_dir))
    monkeypatch.setattr(recorder, 'CSV_FILE', str(temp_dir / 'master_gps_log.csv'))
    # Redirect DATABASE to temp file
    db_file = tmp_path / 'test_recorder.db'
    monkeypatch.setattr(recorder, 'DATABASE', str(db_file))
    yield

# -------------------------------------------------------------------
# Tests for CSV logging
# -------------------------------------------------------------------

def test_init_csv_creates_file_with_header():
    # Ensure CSV does not exist
    if os.path.exists(recorder.CSV_FILE):
        os.remove(recorder.CSV_FILE)
    # Initialize
    recorder.init_csv()
    assert os.path.exists(recorder.CSV_FILE)
    # Read header
    with open(recorder.CSV_FILE, 'r', newline='') as f:
        reader = csv.reader(f)
        header = next(reader)
    expected = [
        'timestamp', 'event_type', 'lat', 'lon', 'fix_good', 'gps_qual',
        'road_id', 'road_name', 'segment_idx', 'segment_distance_m',
        'coverage_percent', 'covered_segments', 'total_segments',
        'recording_file', 'recording_active', 'recording_duration',
        'zone_checks', 'gps_reads', 'thread_state', 'notes'
    ]
    assert header == expected


def test_log_csv_and_flush(tmp_path):
    # Initialize CSV and write a test row
    recorder.init_csv()
    # Log a sample event
    recorder.log_csv('TEST_EVENT', lat=12.34, lon=56.78, fix=True, gps_qual=1, notes='unit test')
    # Force flush any buffered rows
    recorder.flush_csv_buffer()
    # Read back
    rows = list(csv.reader(open(recorder.CSV_FILE)))
    # Header + 1 data row
    assert len(rows) == 2
    data = rows[1]
    assert 'TEST_EVENT' in data
    assert '12.34' in data
    assert '56.78' in data
    assert 'unit test' in data

# -------------------------------------------------------------------
# Tests for database initialization and loading
# -------------------------------------------------------------------

def test_init_database_and_load():
    # Ensure database file does not exist
    if os.path.exists(recorder.DATABASE):
        os.remove(recorder.DATABASE)
    recorder.init_database()
    # Connect and check table
    conn = sqlite3.connect(recorder.DATABASE)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='road_recordings';")
    assert cur.fetchone() is not None
    conn.close()


def test_load_recorded_roads_empty():
    # No entries -> empty set
    recorder.init_database()
    recs = recorder.load_recorded_roads()
    assert isinstance(recs, set)
    assert len(recs) == 0


def test_save_and_load_recording():
    # Initialize and save one recording
    recorder.init_database()
    rid = 'road_abc'
    vf = '/path/to/video.mp4'
    pct = 42.5
    recorder.save_recording_to_db(rid, vf, pct)
    # Load
    recs = recorder.load_recorded_roads()
    assert rid in recs

# -------------------------------------------------------------------
# Test find_current_road with synthetic data
# -------------------------------------------------------------------

def test_find_current_road_none(monkeypatch):
    # Monkeypatch BOUNDS_ARRAY and PREPARED_POLYGONS to empty
    monkeypatch.setattr(recorder, 'BOUNDS_ARRAY', np.empty((0,4)))
    monkeypatch.setattr(recorder, 'PREPARED_POLYGONS', [])
    monkeypatch.setattr(recorder, 'ROAD_IDS', [])
    rid, info = recorder.find_current_road(-0.1,51.5)
    assert rid is None and info is None

# -------------------------------------------------------------------
# Test get_jetson_stats returns expected keys
# -------------------------------------------------------------------

def test_get_jetson_stats_keys():
    stats = recorder.get_jetson_stats()
    expected_keys = [
        'cpu_temp','gpu_temp','mem_percent','mem_used_mb','mem_total_mb',
        'throttled','cpu_freq_mhz','storage_free_gb','storage_percent'
    ]
    for k in expected_keys:
        assert k in stats

# -------------------------------------------------------------------
# Ensure cleanup_orphaned_processes runs without error
# -------------------------------------------------------------------

def test_cleanup_orphaned_processes_smoke():
    # Should not raise
    recorder.cleanup_orphaned_processes()

# -------------------------------------------------------------------
# Test storage speed test logs without raising
# -------------------------------------------------------------------

def test_storage_speed_logs(monkeypatch):
    # Monkeypatch SAVE_DIR to tmp with plenty of space
    monkeypatch.setattr(recorder, 'SAVE_DIR', tempfile.gettempdir())
    # Should not raise
    recorder.test_storage_speed()

# -------------------------------------------------------------------
# Test system health logging
# -------------------------------------------------------------------

def test_check_system_health_runs():
    # Should not raise
    recorder.check_system_health()

# -------------------------------------------------------------------
# Stats tests
# -------------------------------------------------------------------

def test_coverage_statistics(client):
    """Test /api/stats endpoint"""
    roads = ['s1','s2']
    for r in roads:
        client.post('/api/covered', json={'id': r})
        time.sleep(0.01)
    # cover s1 again
    client.post('/api/covered', json={'id': roads[0]})
    resp = client.get('/api/stats')
    assert resp.status_code == 200
    stats = resp.get_json()
    assert stats['total_covered'] == len(roads)
    assert 'daily_coverage' in stats
    assert 'most_covered_roads' in stats
