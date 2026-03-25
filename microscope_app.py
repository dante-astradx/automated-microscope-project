from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file
from folder_name_logger import add_entry, clear_log, clear_last_entry, check_barcode, lookup_smear_coordinates, csv_lookup
from microscope_log import log_output, update_status, get_log_queue, get_status_message
from folder_generator import generate_barcode_folders, generate_background_folders, generate_darkfield_folders, delete_barcode_folders, check_pre_imaging
from light_controller import toggle_light
from google_sheet_editor import log_milestone_run
import subprocess
import time
import json
import threading
from motor import Motor
from file_transfer import FileTransfer
from milestone5_file_transfer import FileTransfer5
from pathlib import Path
import config as c
from transfer_manager import start_worker

app = Flask(__name__)
app.secret_key = "a_very_secret_key_here"

motor_instance = None
current_step = None

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", status_message=get_status_message())

@app.route("/status", methods=["GET"])
def status():
    """AJAX endpoint to fetch the latest status message."""
    return jsonify({"status_message": get_status_message()})

@app.route("/stream")
def stream():
    """SSE endpoint for streaming Motor output."""
    def event_stream():
        while True:
            # Re-fetch new messages every loop
            msgs = get_log_queue()   # should return and DRAIN any pending items
            if msgs:
                for msg in msgs:
                    # one SSE event per message
                    yield f"data: {msg}\n\n"
            else:
                # keep the connection alive and avoid busy-waiting
                yield ": keep-alive\n\n"
                time.sleep(0.5)

    # Important SSE headers; X-Accel-Buffering disables nginx buffering if present
    return Response(
        event_stream(),
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@app.route("/start", methods=["POST"])
def start():
    global motor_instance, current_step

    num_slides = request.form.get("num_slides")
    imaging_mode = request.form.get("imaging_mode")

    # Gather data for all slides into a list of dictionaries
    slides_to_run = []

    # Slide 1 (Always expected)
    s1_barcode = request.form.get("barcode_1")
    s1_smears = request.form.getlist("smears_1")
    if s1_barcode:
        slides_to_run.append({
            "barcode": s1_barcode,
            "smears": s1_smears,
            "offset": 0 # Top slide center
        })

    # Slide 2 (Only if selected in dropdown)
    if num_slides == "2":
        s2_barcode = request.form.get("barcode_2")
        s2_smears = request.form.getlist("smears_2")
        if s2_barcode:
            slides_to_run.append({
                "barcode": s2_barcode,
                "smears": s2_smears,
                "offset": c.SLIDE_HEIGHT_MM # 25mm offset from config
            })

    #Validate barcode foramts
    for slide in slides_to_run:
        if len(slide["smears"]) == 0:
            flash(f"You must select at least one smear to image {slide['barcode']}!")
            return redirect(url_for("index"))

    if not imaging_mode:
        flash("You must select an imaging mode!")
        return redirect(url_for("index"))

    if not check_pre_imaging():
        flash("Background and darkfield images must be taken first. Select Pre-Imaging Button")
        return redirect(url_for("index"))

    for slides in slides_to_run:
        flash(f"Imaging {slides['barcode']} \n")

    # define actual imaging data task
    def data_task():
        for slide in slides_to_run:
            # Check for stop request between slides
            #if motor_instance and motor_instance.stop_requested:
            #    break

            #update_status(f"Starting imaging for Barcode: {slide['barcode']}")
            # Add to folder log
            try:
                add_entry(slide["barcode"])
            except Exception as e:
                log_output(f"Error adding to log: {str(e)}")

            # Initialize file transfer for this specific barcode
            file = FileTransfer5(logger=log_output)
            file.set_barcode(slide["barcode"])

            # Initialize motor with the specific Y-offset for this slide
            motor_instance = Motor(filename=file, logger=log_output)
            motor_instance.slide_y_offset = slide["offset"] # Passed to motor for coordinate math

            log_milestone_run(slide["barcode"], "10, 20, 40x zstack")

            if imaging_mode == "XY_Coordinate":
                smear_ids, coords = csv_lookup(slide["barcode"], slide["smears"])

                fovs = [len(c) for c in coords]
                generate_barcode_folders(slide["barcode"], slide["smears"], fovs)

                update_status(f"Imaging barcode {slide['barcode']} & imaging coordinates: {coords}")
                motor_instance.collect_data_milestone5_xy(smear_ids, coords)

            elif imaging_mode == "Search_Algorithm":
                desired_fov = 5
                fovs = [desired_fov for _ in slide["smears"]]

                generate_barcode_folders(slide["barcode"], slide["smears"], fovs)

                update_status(f"Imaging barcode {slide['barcode']}. Searching for {desired_fov} FOV's at {slide['smears']}")
                motor_instance.collect_data_with_search_algorithm(slide["smears"], fovs)

        #update_status("Multi-slide data collection complete")

    threading.Thread(target=data_task, daemon=True).start()
    return redirect(url_for("index"))

@app.route("/check_light", methods=["POST"])
def check_light():
    toggle_light()
    return redirect(url_for("index"))

@app.route("/pre_imaging", methods=["POST"])
def pre_imaging():
    global motor_instance

    generate_background_folders()
    generate_darkfield_folders()

    file = FileTransfer5(logger=log_output)
    motor_instance = Motor(filename=file, logger=log_output)

    def pre_imaging_task():
        motor_instance.take_dark_background_image()
        update_status("Background and darkfield images complete")

    threading.Thread(target=pre_imaging_task).start()

    #current_step = "background_in_progress"
    return redirect(url_for("index"))

@app.route("/test_carousel", methods=["POST"])
def test_carousel():
    global motor_instance

    file = FileTransfer5(logger=log_output)
    motor_instance = Motor(filename=file, logger=log_output)

    motor_instance.test_carousel()

    return redirect(url_for("index"))

LATEST_IMAGE_PATH = Path(f"{c.PI_IMAGE_DIR}/latest_image.jpg")
@app.route("/latest_image")
def latest_image():
    if LATEST_IMAGE_PATH.exists():
        return send_file(str(LATEST_IMAGE_PATH), mimetype="image/jpeg")
    else:
        return "No image captured yet", 404

@app.route("/save_all", methods=["POST"])
def save_all():
    log_file_path = "folder_name_log.json"

    try:
        with open(log_file_path, "r") as f:
            folder_log = json.load(f)

        ft = FileTransfer5(logger=log_output)
        ft.save_all_data(folder_log)

        clear_log()
        update_status("Data saved and log cleared")
        return jsonify({"status": "success", "message": "Data saved and log cleared"}), 200

    except FileNotFoundError:
        return jsonify({"status": "error", "message": "Log file not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stop", methods=["POST"])
def stop_script():
    global motor_instance, current_step

    try:
        if motor_instance:
            # Attempt to stop hardware safely
            try:
                motor_instance.stop()
                log_output("Motor hard stop triggered.")
            except Exception as e:
                log_output(f"Error stopping motor: {str(e)}")

        # Clear the last folder name entry from the log
        clear_last_entry()

        # Reset the state
        motor_instance = None
        current_step = None
        update_status("Microscope run stopped. Last entry cleared.")

        flash("Microscope stopped safely and last log entry cleared.", "success")
    except Exception as e:
        flash(f"Error while stopping: {str(e)}", "danger")

    return redirect(url_for("index"))

if __name__ == "__main__":
    start_worker()
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
