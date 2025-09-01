#!/bin/bash
# Activate the virtual environment
source /home/microscope_auto/pyenv/rpitest/bin/activate

# Start camera_zmq.py in the background
python /home/microscope_auto/project_files/camera_zmq.py &

# Start the Flask app (this will keep the service running)
python /home/microscope_auto/project_files/microscope_app.py
