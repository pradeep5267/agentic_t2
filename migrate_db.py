#!/usr/bin/env python3
# migrate_db.py
import sqlite3
import os

DB_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "coverage.db")

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Enable Write-Ahead Logging for concurrency
    cursor.execute("PRAGMA journal_mode=WAL;")

    # Create all required tables
    cursor.executescript("""
    -- 1. Roads marked as covered
    CREATE TABLE IF NOT EXISTS covered_roads (
      feature_id   TEXT PRIMARY KEY
    );

    -- 2. Detailed coverage history
    CREATE TABLE IF NOT EXISTS coverage_history (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      feature_id   TEXT     NOT NULL,
      covered_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      latitude     REAL,
      longitude    REAL,
      accuracy     REAL,
      FOREIGN KEY(feature_id) REFERENCES covered_roads(feature_id)
    );
    CREATE INDEX IF NOT EXISTS idx_coverage_history_feature 
      ON coverage_history(feature_id);
    CREATE INDEX IF NOT EXISTS idx_coverage_history_time 
      ON coverage_history(covered_at);

    -- 3. Video recording metadata
    CREATE TABLE IF NOT EXISTS road_recordings (
      feature_id       TEXT PRIMARY KEY,
      video_file       TEXT,
      started_at       TIMESTAMP,
      coverage_percent REAL
    );

    -- 4. Manual overrides from the dashboard
    CREATE TABLE IF NOT EXISTS manual_marks (
      feature_id   TEXT PRIMARY KEY,
      status       TEXT    NOT NULL,        -- 'complete' or 'incomplete'
      marked_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    -- 5. (Optional) In‐memory recorder state table for fallback
    --    Not strictly necessary if you POST to Flask instead.
    CREATE TABLE IF NOT EXISTS recorder_state (
      ts           TIMESTAMP PRIMARY KEY DEFAULT CURRENT_TIMESTAMP,
      lat          REAL,
      lon          REAL,
      heading      REAL,      -- degrees from north
      orientation  TEXT       -- e.g. 'N', 'NE', etc.
    );
    CREATE INDEX IF NOT EXISTS idx_recorder_state_time 
      ON recorder_state(ts DESC);
    """)
    conn.commit()
    conn.close()
    print(f"Migrated database at {DB_PATH}")

if __name__ == "__main__":
    migrate()