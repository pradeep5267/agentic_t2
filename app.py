# app.py

import os
import sys
import sqlite3
import json
import csv
from datetime import datetime
from io import StringIO
from flask import Flask, g, jsonify, request, send_from_directory, Response

# === Configuration ===
BASE_DIR   = os.path.abspath(os.path.dirname(__file__))
DATABASE   = os.path.join(BASE_DIR, "coverage.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Add custom timestamp handling for SQLite
def adapt_datetime_iso(val):
    """Adapt datetime.datetime to timezone-naive ISO 8601 format."""
    return val.isoformat()

def convert_timestamp(val):
    """Convert ISO 8601 string to datetime object."""
    try:
        return datetime.fromisoformat(val.decode())
    except ValueError:
        # Try other formats
        try:
            return datetime.strptime(val.decode(), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            # Return as-is if we can't parse it
            return val.decode()

# Register the adapter and converter
sqlite3.register_adapter(datetime, adapt_datetime_iso)
sqlite3.register_converter("timestamp", convert_timestamp)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

# In-memory recorder state
current_recorder_state = {}


def get_db():
    """
    Opens a new database connection if there is none yet for the 
    current application context. Also ensures the original tables exist.
    """
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row

        # Create the original and history tables (if missing)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS covered_roads (
                feature_id TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS coverage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_id TEXT NOT NULL,
                covered_at TIMESTAMP NOT NULL,
                latitude REAL,
                longitude REAL,
                accuracy REAL,
                FOREIGN KEY (feature_id) REFERENCES covered_roads(feature_id)
            );

            CREATE INDEX IF NOT EXISTS idx_coverage_history_feature_id 
                ON coverage_history(feature_id);
            CREATE INDEX IF NOT EXISTS idx_coverage_history_covered_at 
                ON coverage_history(covered_at);

            -- Ensure manual_marks exists
            CREATE TABLE IF NOT EXISTS manual_marks (
              feature_id   TEXT PRIMARY KEY,
              status       TEXT    NOT NULL,
              marked_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            
            -- Ensure road_recordings exists
            CREATE TABLE IF NOT EXISTS road_recordings (
              feature_id       TEXT PRIMARY KEY,
              video_file       TEXT,
              started_at       TIMESTAMP,
              coverage_percent REAL
            );
        """)
        db.commit()
    return db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database connection at the end of the request."""
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


# --- Serve main dashboard page ---
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "track_coverage.html")


# --- API: List or add covered roads ---
@app.route("/api/covered", methods=["GET", "POST"])
def api_covered():
    db = get_db()
    if request.method == "POST":
        data = request.get_json(force=True)
        fid  = data.get("id")
        if not fid:
            return jsonify({"error": "Missing 'id'"}), 400

        try:
            db.execute("INSERT OR IGNORE INTO covered_roads(feature_id) VALUES(?)", (fid,))
            db.execute(
                "INSERT INTO coverage_history(feature_id, covered_at, latitude, longitude, accuracy) VALUES(?,?,?,?,?)",
                (
                    fid,
                    datetime.utcnow().isoformat(),
                    data.get("lat"),
                    data.get("lon"),
                    data.get("accuracy")
                )
            )
            db.commit()
            return jsonify({"status": "ok", "added": fid})
        except sqlite3.Error as e:
            return jsonify({"error": str(e)}), 500

    # --- FIX: MODIFIED to query ALL THREE tables for coverage ---
    if request.method == "GET":
        query = """
        SELECT feature_id FROM covered_roads
        UNION
        SELECT feature_id FROM road_recordings WHERE video_file IS NOT NULL
        UNION
        SELECT feature_id FROM manual_marks WHERE status = 'complete'
        """
        rows = db.execute(query).fetchall()
        return jsonify({"covered": [r["feature_id"] for r in rows]})


# --- API: Get coverage history ---
@app.route("/api/coverage-history")
def get_coverage_history():
    db = get_db()
    feature_id = request.args.get("feature_id")
    start_date = request.args.get("start_date")
    end_date   = request.args.get("end_date")
    limit      = request.args.get("limit", 1000, type=int)

    query  = "SELECT * FROM coverage_history WHERE 1=1"
    params = []
    if feature_id:
        query += " AND feature_id = ?";        params.append(feature_id)
    if start_date:
        query += " AND covered_at >= ?";        params.append(start_date)
    if end_date:
        query += " AND covered_at <= ?";        params.append(end_date)
    query += " ORDER BY covered_at DESC LIMIT ?"; params.append(limit)

    rows = db.execute(query, params).fetchall()
    history = [dict(r) for r in rows]
    return jsonify({"history": history})


# --- API: Manual mark/unmark roads ---
@app.route("/api/manual-mark", methods=["POST"])
def manual_mark():
    """
    Body: { feature_id: "...", status: "complete"|"incomplete" }
    """
    data = request.get_json(force=True)
    fid    = data.get("feature_id")
    status = data.get("status")
    if not fid or status not in ("complete", "incomplete"):
        return jsonify({"error": "Bad request"}), 400

    db = get_db()
    if status == "complete":
        db.execute("INSERT OR REPLACE INTO manual_marks(feature_id,status) VALUES(?,?)", (fid, status))
    else:
        db.execute("DELETE FROM manual_marks WHERE feature_id = ?", (fid,))
    db.commit()
    return jsonify({"feature_id": fid, "status": status})


@app.route("/api/manual-marks", methods=["GET"])
def manual_marks():
    db = get_db()
    rows = db.execute("SELECT feature_id, status FROM manual_marks").fetchall()
    return jsonify({r["feature_id"]: r["status"] for r in rows})


# --- API: In-memory recorder state (no DB writes) ---
@app.route("/api/recorder-state", methods=["GET", "POST"])
def recorder_state():
    global current_recorder_state

    if request.method == "POST":
        payload = request.get_json(force=True)
        payload.setdefault("ts", datetime.utcnow().isoformat())
        current_recorder_state = payload
        return "", 204

    return jsonify(current_recorder_state)


# --- API: Stats endpoint with improved error handling ---
@app.route("/api/stats")
def get_coverage_stats():
    db = get_db()
    
    # Total covered roads
    total_covered = db.execute("SELECT COUNT(*) as count FROM covered_roads").fetchone()["count"]
    
    # Daily coverage counts
    daily_query = """
    SELECT 
        date(covered_at) as date,
        COUNT(DISTINCT feature_id) as roads_covered,
        COUNT(*) as total_passes
    FROM coverage_history
    GROUP BY date(covered_at)
    ORDER BY date DESC
    LIMIT 30
    """
    daily_rows = db.execute(daily_query).fetchall()
    daily_coverage = [{"date": r["date"], "roads_covered": r["roads_covered"], 
                      "total_passes": r["total_passes"]} for r in daily_rows]
    
    # Most covered roads
    frequent_query = """
    SELECT 
        feature_id,
        COUNT(*) as coverage_count
    FROM coverage_history
    GROUP BY feature_id
    ORDER BY coverage_count DESC
    LIMIT 10
    """
    frequent_rows = db.execute(frequent_query).fetchall()
    most_covered_roads = [{"feature_id": r["feature_id"], 
                           "coverage_count": r["coverage_count"]} for r in frequent_rows]
    
    # Recent recordings - Handle timestamp conversion in Python instead of SQLite
    recordings_query = """
    SELECT 
        feature_id,
        video_file,
        started_at,
        coverage_percent
    FROM road_recordings
    ORDER BY started_at DESC
    LIMIT 5
    """
    try:
        recordings_rows = db.execute(recordings_query).fetchall()
        # Convert each row to a dict and handle the timestamp manually if needed
        recent_recordings = []
        for row in recordings_rows:
            rec = dict(row)
            # If needed, convert started_at to a proper format
            if isinstance(rec['started_at'], str) and 'T' in rec['started_at']:
                # Convert ISO format to datetime and then to string SQLite expects
                dt = datetime.fromisoformat(rec['started_at'])
                rec['started_at'] = dt.strftime('%Y-%m-%d %H:%M:%S')
            recent_recordings.append(rec)
    except Exception as e:
        # Log the error and return empty list
        print(f"Error processing recordings: {e}")
        recent_recordings = []
    
    return jsonify({
        "total_covered": total_covered,
        "daily_coverage": daily_coverage,
        "most_covered_roads": most_covered_roads,
        "recent_recordings": recent_recordings
    })


# --- API: Export endpoints ---
@app.route("/api/export/<format>")
def export_covered_roads(format):
    db = get_db()
    # Get a combined list of all covered roads from all sources
    query = """
    SELECT feature_id FROM covered_roads
    UNION
    SELECT feature_id FROM road_recordings WHERE video_file IS NOT NULL
    UNION
    SELECT feature_id FROM manual_marks WHERE status = 'complete'
    """
    rows = db.execute(query).fetchall()
    covered_ids = [r["feature_id"] for r in rows]
    
    if format == "json":
        resp = jsonify({"covered_roads": covered_ids})
        resp.headers["Content-Disposition"] = "attachment; filename=covered_roads.json"
        return resp
        
    elif format == "csv":
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["feature_id"])
        for fid in covered_ids:
            writer.writerow([fid])
        
        resp = Response(output.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=covered_roads.csv"
        return resp
        
    elif format == "geojson":
        # Load the full GeoJSON from static folder
        geojson_path = os.path.join(STATIC_DIR, "roads_with_polygons.geojson")
        try:
            with open(geojson_path, 'r') as f:
                full_geojson = json.load(f)
            
            # Filter for only covered roads
            covered_features = []
            for feature in full_geojson.get("features", []):
                if feature.get("properties", {}).get("id") in covered_ids:
                    covered_features.append(feature)
            
            export_geojson = {
                "type": "FeatureCollection",
                "features": covered_features
            }
            
            resp = jsonify(export_geojson)
            resp.headers["Content-Disposition"] = "attachment; filename=covered_roads.geojson"
            return resp
        except Exception as e:
            return jsonify({"error": f"GeoJSON error: {str(e)}"}), 500
    
    return jsonify({"error": "Invalid format requested"}), 400


# --- Database maintenance tool ---
def fix_timestamps():
    """Fix timestamp format in road_recordings table"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Get all records
    cursor.execute("SELECT feature_id, started_at FROM road_recordings")
    records = cursor.fetchall()
    
    # Update each record with an ISO format timestamp
    for feature_id, started_at in records:
        if started_at and isinstance(started_at, str) and 'T' in started_at:  # ISO format detected
            try:
                # Parse ISO format
                dt = datetime.fromisoformat(started_at)
                # Convert to SQLite-compatible format
                new_timestamp = dt.strftime('%Y-%m-%d %H:%M:%S')
                
                # Update the record
                cursor.execute(
                    "UPDATE road_recordings SET started_at = ? WHERE feature_id = ?",
                    (new_timestamp, feature_id)
                )
                print(f"Fixed timestamp for {feature_id}: {started_at} -> {new_timestamp}")
            except Exception as e:
                print(f"Error fixing {feature_id}: {e}")
    
    # Commit changes
    conn.commit()
    conn.close()
    print("Timestamp fix complete")


if __name__ == "__main__":
    # Migrate DB if missing
    if not os.path.exists(DATABASE):
        from migrate_db import migrate
        migrate()
        
    # Fix timestamps if requested
    if "--fix-db" in sys.argv:
        fix_timestamps()
        sys.exit()
        
    # Get port from command line argument
    port = 5000
    for i, arg in enumerate(sys.argv):
        if arg == "-p" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i + 1])
            except ValueError:
                pass

    app.run(host="0.0.0.0", port=port, debug=True)