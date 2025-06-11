#!/bin/bash
#
# A script to fully automate the extraction of multiple areas defined in a
# single KML file from a large OSM.PBF file.
#

# --- PRE-REQUISITES ---
# This script requires 'osmium-tool' and 'gdal-bin' (for ogr2ogr).
# On Debian/Ubuntu: sudo apt-get update && sudo apt-get install -y osmium-tool gdal-bin

# --- CONFIGURATION ---
# The full path to your large PBF file of England.
ENGLAND_PBF_FILE="/media/gamedisk/KTP_artefacts/recorder_t14b_v1/PSSav_mk2/map_parser/wide_area_polygon/england-latest.osm.pbf"


# --- SCRIPT LOGIC ---
set -e # Exit immediately if a command exits with a non-zero status.

# Check if a KML file was provided as an argument.
if [ -z "$1" ]; then
  echo "ERROR: You must provide the path to your KML file."
  echo "Usage: ./kml_to_osm_creator.sh <your_file.kml>"
  exit 1
fi

KML_INPUT_FILE="$1"
BASENAME=$(basename "$KML_INPUT_FILE" .kml)
GEOJSON_FILE="${BASENAME}.geojson"
FINAL_OUTPUT_PBF="${BASENAME}_extracted.osm.pbf"
FINAL_OUTPUT_XML="${BASENAME}_extracted.osm"

echo "--- Starting Process for ${KML_INPUT_FILE} ---"

echo "[STEP 1/3] Converting KML to 2D GeoJSON..."
# The "-dim 2" flag is added to force 2D output for osmium compatibility.
ogr2ogr -f "GeoJSON" "$GEOJSON_FILE" "$KML_INPUT_FILE" -dim 2
echo "Success. Created ${GEOJSON_FILE}"

echo "[STEP 2/3] Extracting all polygons from PBF using osmium..."
osmium extract --polygon "$GEOJSON_FILE" "$ENGLAND_PBF_FILE" --output "$FINAL_OUTPUT_PBF" --overwrite
echo "Success. Extracted data written to ${FINAL_OUTPUT_PBF}"

echo "[STEP 3/3] Converting final PBF to OSM XML format..."
osmium cat "$FINAL_OUTPUT_PBF" -o "$FINAL_OUTPUT_XML" --overwrite
echo "Success. Converted to ${FINAL_OUTPUT_XML}"

echo "--- PROCESS COMPLETE ---"