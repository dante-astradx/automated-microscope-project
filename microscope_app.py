from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from folder_name_logger import add_entry, clear_log, clear_last_entry
from microscope_log import log_output, update_status, get_log_queue, get_status_message
import subprocess
import time
import json
import threading
from motor import Motor
from file_transfer import FileTransfer

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
        log_queue = get_log_queue()
        while True:
            if log_queue:
                msg = log_queue.pop(0)
                yield f"data: {msg}\n\n"
            time.sleep(0.5)

    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/start", methods=["POST"])
def start():
    global motor_instance, current_step

    folder_name = request.form["folder_name"]

    if not folder_name:
        flash("Folder name is required to start!")
        return redirect(url_for("index"))

    try:
        add_entry(folder_name)
        update_status("Folder name successfully added to log")
    except Exception as e:
        update_status(f"Error saving to log: {str(e)}")
        return redirect(url_for("index"))

    file = FileTransfer(logger=log_output)
    file.set_filename(folder_name)

    motor_instance = Motor(filename=file, logger=log_output)

    def background_task():
        motor_instance.take_background_image()
        update_status("Background images complete. Turn off microscope light before pressing 'GO'.")

    threading.Thread(target=background_task).start()

    current_step = "background_in_progress"
    return redirect(url_for("index"))

@app.route("/next_step", methods=["POST"])
def next_step():
    global motor_instance, current_step

    if motor_instance is None:
        update_status("Please start first by entering a folder name.")
        return redirect(url_for("index"))

    if current_step == "background_in_progress":
        def darkfield_task():
            motor_instance.take_darkfield_image()
            update_status("Darkfield images complete. Turn on the microscope light and put in the slide before pressing 'GO'.")

        threading.Thread(target=darkfield_task).start()
        current_step = "darkfield_in_progress"

    elif current_step == "darkfield_in_progress":
        def data_task():
            motor_instance.collect_20x_data(1)
            update_status("Data collection complete!")

        threading.Thread(target=data_task).start()
        current_step = "complete"

    elif current_step == "complete":
        update_status("All steps already completed.")

    else:
        update_status("Unknown state. Please start over.")

    return redirect(url_for("index"))

@app.route("/lab_gui", methods=["POST"])
def lab_gui():
    try:
        subprocess.Popen(["/home/microscope_auto/project_files/run_lab_gui.sh"])
        flash("Lab GUI started successfully on laptop!", "success")
    except Exception as e:
        flash(f"Error running Lab GUI script: {str(e)}", "danger")
    return redirect(url_for("index"))

@app.route("/save_all", methods=["POST"])
def save_all():
    log_file_path = "folder_name_log.json"

    try:
        with open(log_file_path, "r") as f:
            folder_log = json.load(f)

        ft = FileTransfer(logger=log_output)
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
    app.run(host="0.0.0.0", port=5001, debug=True, threaded=True)
