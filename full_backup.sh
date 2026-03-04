#!/bin/bash

# 1. Define the Source and Destination
SOURCE_DIR="$HOME/project_files"
BACKUP_ROOT="$HOME/project_files_backup"

# 2. Generate the dated folder name
TODAY=$(date +%Y-%m-%d)
DEST_DIR="$BACKUP_ROOT/project_files_backup_$TODAY"

# 3. Create the backup root folder if it doesn't exist
mkdir -p "$BACKUP_ROOT"

# 4. Perform the copy
echo "Backing up $SOURCE_DIR to $DEST_DIR..."

# -r = recursive (folders + files)
# -p = preserve (keeps file timestamps and permissions)
cp -rp "$SOURCE_DIR" "$DEST_DIR"

echo "Backup complete!"
