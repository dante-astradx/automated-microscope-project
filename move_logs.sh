#!/bin/bash

# 1. Define the destination and ensure it exists
BACKUP_DIR="$HOME/log_files_backup"
mkdir -p "$BACKUP_DIR"

# 2. Get today's date in the format YYYY-MM-DD
TODAY=$(date +%Y-%m-%d)
TODAY_FILE="microscope_log.txt.$TODAY"

echo "Starting log cleanup. Today's file ($TODAY_FILE) will be skipped."

# 3. Loop through all matching log files
for file in microscope_log.txt.*; do

    # Check if the file actually exists (handles empty directory case)
    [ -e "$file" ] || continue

    # 4. If the file is NOT today's log, move it
    if [ "$file" != "$TODAY_FILE" ]; then
        mv "$file" "$BACKUP_DIR/"
        echo "Moved: $file"
    else
        echo "Skipped: $file (Active log)"
    fi
done

echo "Cleanup complete."
