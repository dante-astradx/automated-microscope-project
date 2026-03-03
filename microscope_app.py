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

    folder_name = request.form["folder_name"]
    selected_smears = request.form.getlist("smears")
    imaging_mode = request.form.get("imaging_mode")

    if not folder_name:
        flash("Barcode is required to start!")
        return redirect(url_for("index"))

    if len(selected_smears) == 0:
        flash("You must select at least one smear to image!")
        return redirect(url_for("index"))

    if not imaging_mode:
        flash("You must select an imaging mode!")
        return redirect(url_for("index"))

    if not check_pre_imaging():
        flash("Background and darkfield images must be take first. Select Pre-Imaging Button")
        return redirect(url_for("index"))

    if check_barcode(folder_name):
        try:
            add_entry(folder_name)
            update_status("Folder name successfully added to log")
        except Exception as e:
            update_status(f"Error saving to log: {str(e)}")
            return redirect(url_for("index"))
    else:
        flash("Barcode is not in correct format")
        return redirect(url_for("index"))

    file = FileTransfer5(logger=log_output)
    file.set_barcode(folder_name)

    motor_instance = Motor(filename=file, logger=log_output)

    #log_milestone_run(folder_name, "10x scan")
    log_milestone_run(folder_name, "10, 20, 40x zstack")

    if imaging_mode == "XY_Coordinate":
        smear_ids, coords = csv_lookup(folder_name, selected_smears)

        fovs = []
        for i in range(len(coords)):
            number_of_fovs = len(coords[i])
            fovs.append(number_of_fovs)

        generate_barcode_folders(folder_name, selected_smears, fovs)
        def task_fn():
            update_status(f"Imaging barcode {folder_name} & imaging coordinates: {coords}")
            motor_instance.collect_data_milestone5_xy(smear_ids, coords)

    elif imaging_mode == "Search_Algorithm":
        desired_fov = 5
        fovs = []
        for i in range(len(selected_smears)):
            fovs.append(desired_fov)

        generate_barcode_folders(folder_name, selected_smears, fovs)
        def task_fn():
            update_status(f"Imaging barcode {folder_name}. Searching for {desired_fov} FOV's at {selected_smears}")
            motor_instance.collect_data_with_search_algorithm(selected_smears, fovs)
            #motor_instance.collect_data_milestone5(1, selected_smears)

    def data_task():
        task_fn()
        update_status("Data collection complete")

    threading.Thread(target=data_task, daemon=True).start()

    return redirect(url_for("index"))

    #smear_ids, coords = csv_lookup(folder_name)
    #log_milestone_run(folder_name, "10x scan")
    #log_milestone_run(folder_name, "10, 20, 40x zstack")

    #def data_task():
        #motor_instance.smear_analysis_test(selected_smears)
        #motor_instance.collect_data_milestone2()
        #motor_instance.collect_data_milestone5(1, selected_smears)
        #motor_instance.collect_data_milestone5_xy(1, smear_ids, coords)
        #update_status("Data collection complete")

    #threading.Thread(target=data_task).start()

    #return redirect(url_for("index"))

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
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
