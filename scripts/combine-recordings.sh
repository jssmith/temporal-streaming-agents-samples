#!/usr/bin/env bash
#
# Combine the app and temporal UI recordings into a side-by-side video.
# Finds the two .webm files from the last recording run and merges them.
#
set -euo pipefail

RECORDING_DIR="test-results/recording"
OUTPUT="$RECORDING_DIR/combined.mp4"

# Find videos by project directory name
APP_VIDEO=$(find "$RECORDING_DIR" -path "*app*" -name "video.webm" | head -1)
TEMPORAL_VIDEO=$(find "$RECORDING_DIR" -path "*temporal*" -name "video.webm" | head -1)

if [[ -z "$APP_VIDEO" ]]; then
  echo "Error: could not find app recording in $RECORDING_DIR" >&2
  exit 1
fi

if [[ -z "$TEMPORAL_VIDEO" ]]; then
  echo "Error: could not find temporal recording in $RECORDING_DIR" >&2
  exit 1
fi

echo "App video:      $APP_VIDEO"
echo "Temporal video: $TEMPORAL_VIDEO"
echo "Output:         $OUTPUT"

# Side-by-side: scale both to same height, stack horizontally
ffmpeg -y \
  -i "$APP_VIDEO" \
  -i "$TEMPORAL_VIDEO" \
  -filter_complex " \
    [0:v]scale=1280:720[left]; \
    [1:v]scale=1280:720[right]; \
    [left][right]hstack=inputs=2" \
  -c:v libx264 -preset fast -crf 23 \
  "$OUTPUT"

echo "Combined video saved to $OUTPUT"
