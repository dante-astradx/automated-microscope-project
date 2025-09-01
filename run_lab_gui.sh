#!/bin/bash

# Define laptop user and IP
LAPTOP_USER="dantemuzila"
LAPTOP_IP="192.168.50.3"

# Command to run on the laptop (backgrounded so SSH returns immediately)
REMOTE_CMD="cd '/Users/dantemuzila/Documents/Microscope X-Y Axis Mechanism/lab_GUI' && source ~/pyenv/zstack_env/bin/activate && python3 zstack_folders_generator.py &"

# SSH into the laptop and run the command
ssh ${LAPTOP_USER}@${LAPTOP_IP} "${REMOTE_CMD}"
