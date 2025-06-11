#!/usr/bin/env python3
"""
road_coverage_recorder.py - Road coverage video recorder with CSV batching,
GPS reconnection, process cleanup, system monitoring,
thread-safe operations, non-blocking recording stop, and database integration.
"""

import subprocess
import signal
import time
import csv
import os
import threading
import queue
import sqlite3
import numpy as np
import pickle
import serial
import pynmea2
import sys
import requests
import shutil
from datetime import datetime
from shapely.geometry import Point
import shapely.prepared as prep

# Configuration
# --- MODIFIED: Changed from single GPS_PORT to primary and fallback ports ---
GPS_PRIMARY_PORT = "/dev/ttyACM0"
GPS_FALLBACK_PORT = "/dev/ttyUSB0"
BAUD_RATE = 115200
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "coverage.db")
SAVE_DIR = "/media/road_coverage_recordings"
CSV_FILE = f"{SAVE_DIR}/master_gps_log.csv"
PREPROCESSED_DIR = os.path.join(BASE_DIR, "preprocessed_roads")

# Flask recorder-state endpoint (for dashboard)
RECORDER_STATE_URL = "http://localhost:5000/api/recorder-state"
STATE_POST_INTERVAL = 1.0  # seconds

# Timing Configuration
MIN_RECORDING_DURATION = 3
RECORDING_STATE_DELAY = 0.5
PIPELINE_START_WAIT = 0.5
GPS_RECONNECT_DELAY = 5  # Seconds before retry

# CSV Batching Configuration
CSV_BUFFER_SIZE = 30      # Flush every 30 entries (~1s)
CSV_FLUSH_INTERVAL = 1.0  # Flush at least every 1 second

# System Monitoring Configuration
SYSTEM_HEALTH_INTERVAL = 30    # seconds
STORAGE_TEST_SIZE_MB = 10      # reduced test size for speed
STORAGE_WARNING_GB = 10
TEMP_WARNING_C = 80
TEMP_CRITICAL_C = 90

# Thresholds
SEGMENT_THRESHOLD_M = 15
ROAD_EXIT_THRESHOLD_S = 3

# Global state
gps_queue = queue.Queue()
gps_data = {}
recorded_roads = set()
road_coverage_state = {}
current_road_id = None
recording_proc = None
recording_file = None
recording_start_time = None
last_recording_stop = 0

# Thread coordination
shutdown_event = threading.Event()

# CSV batching structures
csv_buffer = []
csv_buffer_lock = threading.Lock()
last_csv_flush = time.time()

# Debug counters
counter_lock = threading.Lock()
zone_check_counter = 0
gps_read_counter = 0

# Recording stop request timestamp
recording_stop_requested = None

# Process-group tracking for cleanups
recording_pgids = set()

# Load preprocessed GIS data
print("Loading preprocessed road data...")
BOUNDS_ARRAY = np.load(f"{PREPROCESSED_DIR}/road_bounds.npy")
with open(f"{PREPROCESSED_DIR}/road_data.pkl", 'rb') as f:
    ROAD_DATA = pickle.load(f)
with open(f"{PREPROCESSED_DIR}/buffer_polygons.pkl", 'rb') as f:
    BUFFER_POLYGONS = pickle.load(f)
with open(f"{PREPROCESSED_DIR}/road_ids.pkl", 'rb') as f:
    ROAD_IDS = pickle.load(f)
PREPARED_POLYGONS = [prep.prep(poly) for poly in BUFFER_POLYGONS]

# Helper: Initialize CSV
def init_csv():
    os.makedirs(SAVE_DIR, exist_ok=True)
    if not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0:
        with open(CSV_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'event_type', 'lat', 'lon', 'fix_good', 'gps_qual',
                'road_id', 'road_name', 'segment_idx', 'segment_distance_m',
                'coverage_percent', 'covered_segments', 'total_segments',
                'recording_file', 'recording_active', 'recording_duration',
                'zone_checks', 'gps_reads', 'thread_state', 'notes'
            ])
        print("Initialized CSV log with header.")

# Helper: Write CSV buffer
def flush_csv_buffer():
    global csv_buffer
    buffer_to_flush = []
    with csv_buffer_lock:
        if csv_buffer:
            buffer_to_flush = list(csv_buffer)
            csv_buffer.clear()
    if buffer_to_flush:
        try:
            with open(CSV_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerows(buffer_to_flush)
        except Exception as e:
            print(f"[CSV] Error flushing buffer: {e}")

# Core logging function
def log_csv(event_type, **kwargs):
    global last_csv_flush
    with counter_lock:
        zc = zone_check_counter
        gr = gps_read_counter
    rec_dur = ''
    if recording_start_time and recording_proc:
        rec_dur = f"{time.time() - recording_start_time:.1f}s"
    percent = covered = total = ''
    if current_road_id and current_road_id in road_coverage_state:
        cov = len(road_coverage_state[current_road_id])
        tot = len(ROAD_DATA[current_road_id]['segments'])
        percent = f"{cov/tot*100:.1f}" if tot else ''
        covered, total = cov, tot
    row = [
        datetime.now().isoformat(), event_type,
        kwargs.get('lat', gps_data.get('lat', '')),
        kwargs.get('lon', gps_data.get('lon', '')),
        kwargs.get('fix', gps_data.get('fix', '')),
        kwargs.get('gps_qual', gps_data.get('gps_qual', '')),
        kwargs.get('road_id', current_road_id or ''),
        kwargs.get('road_name', ''),
        kwargs.get('segment_idx', ''),
        kwargs.get('segment_distance', ''),
        percent, covered, total,
        kwargs.get('recording_file', recording_file or ''),
        bool(recording_proc), rec_dur,
        zc, gr,
        kwargs.get('thread_state', 'MAIN'),
        kwargs.get('notes', '')
    ]
    flush = False
    with csv_buffer_lock:
        csv_buffer.append(row)
        if len(csv_buffer) >= CSV_BUFFER_SIZE or time.time() - last_csv_flush >= CSV_FLUSH_INTERVAL:
            to_write = list(csv_buffer)
            csv_buffer.clear()
            last_csv_flush = time.time()
            flush = True
    if flush:
        try:
            with open(CSV_FILE, 'a', newline='') as f:
                csv.writer(f).writerows(to_write)
        except Exception as e:
            print(f"[CSV] Error writing rows: {e}")

# POST current state to dashboard
def post_state(lat, lon, heading, orientation):
    now = time.time()
    if now - getattr(post_state, 'last_post_time', 0) < STATE_POST_INTERVAL:
        return
    payload = {
        'lat': lat, 'lon': lon, 'heading': heading, 'orientation': orientation,
        'ts': datetime.utcnow().isoformat()
    }
    try:
        requests.post(RECORDER_STATE_URL, json=payload, timeout=0.5)
        post_state.last_post_time = now
    except requests.exceptions.RequestException:
        log_csv('STATE_POST_ERROR', notes='Failed to POST recorder state')
post_state.last_post_time = 0

# System Health and Monitoring Functions...
def test_storage_speed():
    test_file = f"{SAVE_DIR}/.speed_test.tmp"
    size_mb = STORAGE_TEST_SIZE_MB
    try:
        data = os.urandom(size_mb * 1024 * 1024)
        start = time.time()
        with open(test_file, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        dur = time.time() - start
        speed = size_mb / dur
        os.remove(test_file)
        log_csv('STORAGE_TEST', notes=f"Speed {speed:.1f} MB/s")
        if speed < 50:
            log_csv('STORAGE_WARNING', notes=f"Slow storage: {speed:.1f} MB/s")
    except Exception as e:
        log_csv('STORAGE_TEST_ERROR', notes=str(e))

def get_jetson_stats():
    stats = dict(cpu_temp=-1, gpu_temp=-1, mem_percent=-1, mem_used_mb=-1,
                 mem_total_mb=-1, throttled=False, cpu_freq_mhz=-1,
                 storage_free_gb=-1, storage_percent=-1)
    try:
        with open('/sys/class/thermal/thermal_zone1/temp') as f:
            stats['cpu_temp'] = int(f.read())/1000
    except: pass
    try:
        with open('/sys/class/thermal/thermal_zone2/temp') as f:
            stats['gpu_temp'] = int(f.read())/1000
    except: pass
    try:
        minfo = {}
        with open('/proc/meminfo') as f:
            for l in f:
                k,v = l.split(None,1)
                minfo[k.rstrip(':')] = int(v.split()[0])
        tot = minfo.get('MemTotal',0)
        avl = minfo.get('MemAvailable',0)
        stats['mem_total_mb'] = tot/1024
        stats['mem_used_mb'] = (tot-avl)/1024
        stats['mem_percent'] = (tot-avl)/tot*100 if tot else -1
    except: pass
    try:
        with open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq') as f:
            cf = int(f.read())
        with open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq') as f:
            mf = int(f.read())
        stats['cpu_freq_mhz'] = cf/1000
        stats['throttled'] = cf < mf*0.9
    except: pass
    try:
        st = os.statvfs(SAVE_DIR)
        free = st.f_bavail * st.f_frsize
        totb = st.f_blocks * st.f_frsize
        stats['storage_free_gb'] = free/(1024**3)
        stats['storage_percent'] = (totb-free)/totb*100
    except: pass
    return stats

def check_system_health():
    stats = get_jetson_stats()
    parts, warns = [], []
    if stats['cpu_temp']>0:
        parts.append(f"CPU:{stats['cpu_temp']:.1f}°C")
        if stats['cpu_temp']>TEMP_CRITICAL_C: warns.append(f"CPU CRIT {stats['cpu_temp']:.1f}°C")
        elif stats['cpu_temp']>TEMP_WARNING_C: warns.append(f"CPU HIGH {stats['cpu_temp']:.1f}°C")
    if stats['gpu_temp']>0:
        parts.append(f"GPU:{stats['gpu_temp']:.1f}°C")
        if stats['gpu_temp']>TEMP_CRITICAL_C: warns.append(f"GPU CRIT {stats['gpu_temp']:.1f}°C")
    if stats['mem_percent']>0:
        parts.append(f"Mem:{stats['mem_percent']:.0f}%")
        if stats['mem_percent']>90: warns.append(f"MEM HIGH {stats['mem_percent']:.0f}%")
    if stats['cpu_freq_mhz']>0:
        parts.append(f"CPUfreq:{stats['cpu_freq_mhz']:.0f}MHz")
    if stats['throttled']: warns.append("CPU THROTTLED")
    if stats['storage_free_gb']>0:
        parts.append(f"Disk:{stats['storage_free_gb']:.1f}GB")
        if stats['storage_free_gb']<STORAGE_WARNING_GB:
            warns.append(f"LOW DISK {stats['storage_free_gb']:.1f}GB")
    log_csv('SYSTEM_HEALTH', notes=" ".join(parts))
    for w in warns:
        log_csv('SYSTEM_WARN', notes=w)

def system_monitor_thread():
    log_csv('MONITOR_START')
    test_storage_speed()
    check_system_health()
    cnt=0
    while not shutdown_event.is_set():
        if shutdown_event.wait(timeout=1): break
        cnt+=1
        if cnt % SYSTEM_HEALTH_INTERVAL == 0:
            check_system_health()
    log_csv('MONITOR_EXIT')

# Process cleanup
def cleanup_orphaned_processes():
    try:
        r = subprocess.run(['pgrep','-f','gst-launch-1.0'], capture_output=True, text=True)
        if r.stdout.strip():
            subprocess.run(['pkill','-f','gst-launch-1.0'])
            time.sleep(1)
            subprocess.run(['pkill','-9','-f','gst-launch-1.0'])
            log_csv('CLEANUP_ORPHANS')
    except: pass

def cleanup_specific_process(pgid):
    try:
        os.killpg(pgid, signal.SIGINT)
        for _ in range(5):
            time.sleep(0.1)
            try: os.killpg(pgid, 0)
            except OSError: return
        os.killpg(pgid, signal.SIGKILL)
        log_csv('PROCESS_KILLED', notes=f"PGID {pgid}")
    except: pass

# --- MODIFIED: GPS thread with fallback port logic ---
def gps_thread():
    global gps_data, gps_read_counter
    log_csv('GPS_THREAD_START', thread_state='GPS')
    attempts = 0
    current_port = GPS_PRIMARY_PORT
    
    while not shutdown_event.is_set():
        try:
            log_csv('GPS_PORT_TRYING', thread_state='GPS', notes=f"Trying {current_port}")
            ser = serial.Serial(current_port, BAUD_RATE, timeout=1)
            log_csv('GPS_PORT_OPENED', thread_state='GPS', notes=f"Connected to {current_port}")
            attempts = 0
            
            # Process GPS data
            while not shutdown_event.is_set():
                line = ser.readline().decode('utf-8','ignore').strip()
                if line.startswith('$G'):
                    with counter_lock:
                        gps_read_counter += 1
                    try:
                        msg = pynmea2.parse(line)
                        if hasattr(msg,'latitude') and hasattr(msg,'longitude'):
                            gps_data = {
                                'lat': msg.latitude, 'lon': msg.longitude,
                                'fix': getattr(msg,'gps_qual',0)>0,
                                'gps_qual': getattr(msg,'gps_qual',0),
                                'time': time.time()
                            }
                            gps_queue.put(gps_data.copy())
                    except: pass
                    
        except serial.SerialException as e:
            attempts += 1
            log_csv('GPS_PORT_ERROR', thread_state='GPS', notes=f"{current_port}: {str(e)}")
            
            # Toggle between primary and fallback port
            current_port = GPS_FALLBACK_PORT if current_port == GPS_PRIMARY_PORT else GPS_PRIMARY_PORT
            
            # Wait before retry with increasing backoff
            d = min(30, GPS_RECONNECT_DELAY * attempts)
            log_csv('GPS_RETRY', thread_state='GPS', notes=f"Switching to {current_port}, retry in {d}s")
            shutdown_event.wait(timeout=d)
            
        finally:
            try:
                ser.close()
            except:
                pass
                
    log_csv('GPS_THREAD_EXIT', thread_state='GPS')

# Road-finding
def find_current_road(lon, lat):
    global zone_check_counter
    with counter_lock:
        zone_check_counter+=1
        local_z = zone_check_counter
    pt = Point(lon,lat)
    idxs = np.where(
        (BOUNDS_ARRAY[:,0] <= lon)&(BOUNDS_ARRAY[:,2] >= lon)&
        (BOUNDS_ARRAY[:,1] <= lat)&(BOUNDS_ARRAY[:,3] >= lat)
    )[0]
    for i in idxs:
        if PREPARED_POLYGONS[i].contains(pt):
            rid = ROAD_IDS[i]
            if local_z % 50 == 0:
                log_csv('ZONE_CHECK', lat=lat, lon=lon, road_id=rid, notes=f"check #{local_z}")
            return rid, ROAD_DATA[rid]
    return None, None

def find_nearest_segment(rid, lat, lon):
    segs = ROAD_DATA[rid]['segments']
    md,mi = float('inf'),-1
    for i,(slon,slat) in enumerate(segs):
        d=((lon-slon)**2+(lat-slat)**2)**0.5*111320
        if d<md: md,mi=d,i
    return mi,md

def calculate_coverage(road_id):
    if road_id not in road_coverage_state: return 0.0
    covered = len(road_coverage_state[road_id])
    total = len(ROAD_DATA[road_id]['segments'])
    return (covered / total * 100) if total > 0 else 0.0

def save_recording_to_db(road_id, video_file, coverage_percent):
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute('''
            INSERT OR REPLACE INTO road_recordings 
            (feature_id, video_file, started_at, coverage_percent)
            VALUES (?, ?, ?, ?)
        ''', (road_id, video_file, datetime.now().isoformat(), coverage_percent))
        
        # Also update the covered_roads table for consistency with the web app
        conn.execute('''
            INSERT OR IGNORE INTO covered_roads
            (feature_id) VALUES (?)
        ''', (road_id,))
        
        conn.commit()
        conn.close()
        log_csv('DB_RECORDING_SAVED', road_id=road_id, notes=f'Coverage: {coverage_percent:.1f}%')
    except Exception as e:
        log_csv('DB_SAVE_ERROR', road_id=road_id, notes=f'DB Error: {e}')

def stop_recording():
    global recording_proc, recording_file, last_recording_stop, recording_start_time
    if not recording_proc: return
    duration = time.time() - recording_start_time
    if duration < MIN_RECORDING_DURATION:
        time.sleep(MIN_RECORDING_DURATION - duration)
        duration = MIN_RECORDING_DURATION
    log_csv('RECORDING_STOPPING', notes=f'duration={duration:.1f}s')
    try:
        pgid = os.getpgid(recording_proc.pid)
        os.killpg(pgid, signal.SIGINT)
        recording_proc.wait(timeout=5)
        log_csv('RECORDING_STOPPED', notes=f'exit code {recording_proc.returncode}')
    except Exception as e:
        log_csv('RECORDING_KILLED', notes=f'killed on error: {e}')
        cleanup_specific_process(os.getpgid(recording_proc.pid))
    finally:
        recording_proc, recording_file, recording_start_time = None, None, None
        last_recording_stop = time.time()

# Recording control
def start_recording(rid):
    global recording_proc, recording_file, recording_start_time, last_recording_stop
    cleanup_orphaned_processes()
    now=time.time()
    if now-last_recording_stop<RECORDING_STATE_DELAY:
        time.sleep(RECORDING_STATE_DELAY-(now-last_recording_stop))
    ts=datetime.now().strftime("%Y%m%d_%H%M%S")
    safe=ROAD_DATA[rid]['name'].replace('/','_')[:30]
    recording_file=f"{SAVE_DIR}/road_{rid}_{safe}_{ts}.mp4"
    log_csv('PIPELINE_CREATE', road_id=rid)
    cmd = [
        'gst-launch-1.0','-e','nvarguscamerasrc','sensor-id=0','!',
        'video/x-raw(memory:NVMM),width=1280,height=720,framerate=30/1','!',
        'nvv4l2h265enc','bitrate=2000000','!','h265parse','!','mp4mux','!',
        'filesink',f'location={recording_file}'
    ]
    try:
        recording_proc = subprocess.Popen(
            cmd, preexec_fn=os.setsid, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        recording_start_time = time.time()
        pgid = os.getpgid(recording_proc.pid)
        recording_pgids.add(pgid)
        time.sleep(PIPELINE_START_WAIT)
        if recording_proc.poll() is None:
            log_csv('RECORDING_STARTED', road_id=rid, notes=f'PGID {pgid}')
        else:
            log_csv('RECORDING_FAILED', road_id=rid); recording_proc=None
    except Exception as e:
        log_csv('RECORDING_ERROR', road_id=rid, notes=str(e)); recording_proc=None
    return recording_file

def force_stop_recording():
    if recording_proc:
        log_csv('RECORDING_FORCE_STOP')
        cleanup_specific_process(os.getpgid(recording_proc.pid))

# --- MODIFIED: Enhanced signal handler with emergency data preservation ---
def signal_handler(signum, frame):
    """Enhanced signal handler with emergency data preservation."""
    # Get signal name for better logging
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    log_csv('SIGNAL_RECEIVED', notes=f'{sig_name}')
    
    # Ensure CSV data is saved immediately
    flush_csv_buffer()
    
    # Create emergency backup of CSV
    try:
        timestamp = int(time.time())
        safe_csv = f"{SAVE_DIR}/emergency_save_{timestamp}.csv"
        shutil.copy2(CSV_FILE, safe_csv)
        log_csv('EMERGENCY_BACKUP', notes=f"CSV backed up to {safe_csv}")
    except Exception as e:
        log_csv('BACKUP_ERROR', notes=f"Failed to create emergency backup: {e}")
    
    # Mark for shutdown
    shutdown_event.set()
    
    # Block additional signals during shutdown
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

# --- MODIFIED: Initialize database with all required tables for web app integration ---
def init_database():
    """Initialize database with all required tables for integration with web app."""
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute('PRAGMA journal_mode=WAL')
        
        # Create all tables needed for full integration
        conn.executescript('''
            -- For video recordings
            CREATE TABLE IF NOT EXISTS road_recordings(
                feature_id TEXT PRIMARY KEY,
                video_file TEXT,
                started_at TEXT,
                coverage_percent REAL
            );
            
            -- For manually marked roads (from dashboard)
            CREATE TABLE IF NOT EXISTS manual_marks(
                feature_id TEXT PRIMARY KEY,
                status TEXT,
                marked_at TEXT
            );
            
            -- For proximity-based coverage (from dashboard)
            CREATE TABLE IF NOT EXISTS covered_roads(
                feature_id TEXT PRIMARY KEY
            );
            
            -- For detailed coverage history (from dashboard)
            CREATE TABLE IF NOT EXISTS coverage_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_id TEXT NOT NULL,
                covered_at TIMESTAMP NOT NULL,
                latitude REAL,
                longitude REAL,
                accuracy REAL,
                FOREIGN KEY (feature_id) REFERENCES covered_roads(feature_id)
            );
        ''')
        
        conn.commit()
        conn.close()
        log_csv('DB_INITIALIZED')
    except Exception as e:
        log_csv('DB_INIT_ERROR', notes=str(e))

def load_recorded_roads():
    """Load roads that have been recorded OR manually marked as complete."""
    combined_roads = set()
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Query for roads with a video file
        cursor.execute("SELECT feature_id FROM road_recordings WHERE video_file IS NOT NULL")
        for row in cursor.fetchall():
            combined_roads.add(row[0])
            
        # Query for roads manually marked as complete
        cursor.execute("SELECT feature_id FROM manual_marks WHERE status = 'complete'")
        for row in cursor.fetchall():
            combined_roads.add(row[0])
            
        conn.close()
        log_csv("DB_LOADED", notes=f"Loaded {len(combined_roads)} roads to skip")
        return combined_roads
    except Exception as e:
        log_csv('DB_LOAD_ERROR', notes=str(e))
        return set()

# Main loop
def main():
    global current_road_id, recorded_roads
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    cleanup_orphaned_processes()
    init_csv()
    init_database()
    recorded_roads = load_recorded_roads()
    log_csv('SYSTEM_START')
    gps_t = threading.Thread(target=gps_thread, daemon=True)
    monitor_t = threading.Thread(target=system_monitor_thread, daemon=True)
    gps_t.start()
    monitor_t.start()
    last_on_road, exit_logged = None, False
    try:
        while not shutdown_event.is_set():
            try:
                gps = gps_queue.get(timeout=0.1)
            except queue.Empty:
                time.sleep(0.01)
                continue
            rid, info = find_current_road(gps['lon'], gps['lat'])
            if rid:
                seg_idx, seg_dist = find_nearest_segment(rid, gps['lat'], gps['lon'])
                if seg_dist <= SEGMENT_THRESHOLD_M:
                    road_coverage_state.setdefault(rid, set()).add(seg_idx)
            log_csv('GPS_POSITION', lat=gps['lat'], lon=gps['lon'], fix=gps['fix'], gps_qual=gps['gps_qual'])
            post_state(gps['lat'], gps['lon'], 0.0, 'N')
            if rid:
                last_on_road = time.time()
                exit_logged = False
                if rid != current_road_id:
                    if recording_proc:
                        stop_recording()
                        save_recording_to_db(current_road_id, recording_file, calculate_coverage(current_road_id))
                    log_csv('ROAD_ENTER', road_id=rid)
                    if rid not in recorded_roads:
                        start_recording(rid)
                    current_road_id = rid
            else:
                if current_road_id and last_on_road and not exit_logged and time.time() - last_on_road > ROAD_EXIT_THRESHOLD_S:
                    pct = calculate_coverage(current_road_id)
                    log_csv('ROAD_EXIT', road_id=current_road_id, notes=f"coverage={pct:.1f}")
                    if recording_proc:
                        stop_recording()
                        save_recording_to_db(current_road_id, recording_file, pct)
                    current_road_id, exit_logged = None, True
    except Exception as e:
        log_csv('SYSTEM_ERROR', notes=str(e))
    finally:
        shutdown_event.set()
        if recording_proc: force_stop_recording()
        gps_t.join(timeout=2)
        monitor_t.join(timeout=2)
        flush_csv_buffer()
        log_csv('SYSTEM_EXIT')

if __name__ == "__main__":
    main()