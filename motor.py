import requests
import json
import time
import random
import os
import subprocess
import numpy as np
import config as c
import pandas as pd
from camera import Camera
from file_transfer import FileTransfer
from milestone5_file_transfer import FileTransfer5
import analysis as a
from datetime import datetime
from datetime import date
from microscope_log import log_output, log_to_file_only, update_status, update_scoreboard
from mac_comms import send_background_image_to_mac, send_darkfield_image_to_mac, send_image_to_mac
from json_handler import (
    read_json, 
    create_zstack_json, 
    append_image_data_to_zstack_json, 
    append_correction_image_to_json, 
    create_correction_json, 
    create_manifest_json,
    append_zstack_metadata_to_manifest,
    populate_zstack_json_from_folder,
    update_failed_qc_zstack_json,
    update_zstack_qc_result,
)
import light_controller as lc
import time
import math
from pathlib import Path
from transfer_manager import enqueue_folder, mark_slide_done
from folder_generator import generate_barcode_folders

# Continuous (motion-blur) series polling, in seconds unless noted.
MOTION_BLUR_POLL_INTERVAL_S = 0.5
MOTION_BLUR_PROGRESS_LOG_S = 5.0
# Fail if captured/written counts (and the current group's frame count) stop
# changing for this long.
MOTION_BLUR_STALL_TIMEOUT_S = 60.0
# Fail after this many consecutive timeout/malformed status replies.
MOTION_BLUR_MAX_BAD_REPLIES = 5
# After a stop request, how long to wait for the cancelled series to finish
# writing already-queued images and reach a terminal state.
MOTION_BLUR_CANCEL_WAIT_S = 60.0

class Motor:
    def __init__(self, filename, logger=print):
        # Moonraker API Endpoint
        self.gcode_api_url = c.GCODE_API_URL

        # Target Axis Identifiers
        self.target_axis_x = c.TARGET_AXIS_X
        self.target_axis_y = c.TARGET_AXIS_Y
        self.target_axis_z = c.TARGET_AXIS_Z

        # Movement Speed
        self.movement_speed_mm_s = c.MOVEMENT_SPEED_MM_S

        # Motor Rotation Distance in MM
        self.rotation_distance_mm_x = c.ROTATION_DISTANCE_MM_X
        self.rotation_distance_mm_y = c.ROTATION_DISTANCE_MM_Y
        self.rotation_distance_mm_z = c.ROTATION_DISTANCE_MM_Z

        # Coordinate Mapping
        self.x_offset = c.MICROSCOPE_X_OFFSET_AT_PRINTER_HOME
        self.y_offset = c.MICROSCOPE_Y_OFFSET_AT_PRINTER_HOME
        self.z_offset = c.MICROSCOPE_Z_OFFSET_AT_PRINTER_HOME

        # Z Axis focus step size
        self.z_inc = c.INC
        self.z_num = c.NUM

        # Distance movement in MM to complete MOTOR_ROT_X,Y,Z in config.py
        self.rot_distance_x = c.ROT_DISTANCE_X
        self.rot_distance_y = c.ROT_DISTANCE_Y
        self.rot_distance_z = c.ROT_DISTANCE_Z

        # Creating an instance of camera class
        self.imager = Camera()

        # Accepting instance of FileTransfer
        self.filename = filename
        self.zstack_folder_path = None

        # Accepting logger function
        self.logger = logger

        # Number of frames to image
        self.z_focus_nframes = c.Z_FOCUS_NFRAMES
        self.nframes = c.NFRAMES

        # Slide x-y range
        self.start_x = 115
        self.end_x = 145
        self.start_y = 10
        self.end_y = 20

        self.sm1_x_min = c.SM1_X_MIN
        self.sm1_x_max = c.SM1_X_MAX
        self.sm2_x_min = c.SM2_X_MIN
        self.sm2_x_max = c.SM2_X_MAX
        self.sm3_x_min = c.SM3_X_MIN
        self.sm3_x_max = c.SM3_X_MAX

        #self.y_min = c.Y_MIN
        #self.y_max = c.Y_MAX
        self.y_range_allowed = c.Y_RANGE_ALLOWED

        self.smear_x_min = None
        self.smear_x_max = None

        # Image directory path
        self.pi_image_dir = c.PI_IMAGE_DIR

        # Current x - y position
        self.current_x = None
        self.current_y = None
        self.current_z = None

        # Focus presets
        self.focus_preset_10x = c.FOCUS_PRESET_10X
        self.focus_preset_20x = c.FOCUS_PRESET_20X
        self.focus_preset_40x = c.FOCUS_PRESET_40X

        # Focus View
        self.focus_view = None

        # Objective
        self.obj = None

        # Exposure time
        self.exposure_time_10x = c.EXPOSURE_TIME_10X
        self.exposure_time_20x = c.EXPOSURE_TIME_20X
        self.exposure_time_40x = c.EXPOSURE_TIME_40X

        # Points before/after to image/scan
        self.points_before = c.POINTS_BEFORE
        self.points_after = c.POINTS_AFTER

        # Date
        self.date  = date.today().strftime("%Y%m%d")

        # Stop Requested
        self.stop_requested = False

        # Auto-stop settings
        self.auto_stop_enabled = c.AUTO_STOP_ENABLED
        self.auto_stop_focus_threshold = c.AUTO_STOP_FOCUS_THRESHOLD
        self.auto_stop_reason = None

        # Two Slide offset - Initialize with 0 by default i.e. Slide 1, will be 25 for Slide 2
        self.slide_y_offset = 0

    def initiate_transfer_queue(self, focus_view, obj):
        """Queue the completed zstack folder for transfer to Mac."""
        folder_path = self.filename.data_path_generator(focus_view, obj)
        basename = os.path.basename(folder_path)
        self.logger(f"Queuing {basename} for upload to Mac")
        enqueue_folder(folder_path, self.filename.barcode, self.filename.date)

    def stop(self):
        # Signal the motor to stop gracefully
        self.stop_requested = True
        self.logger("Motor stop requested.")

    def check_stop(self):
        self.logger(f"Checking stop flag. Flag is {self.stop_requested}")
        # Helper to check stop flag and raise if needed
        if self.stop_requested:
            raise RuntimeError("Motor stop requested.")

    def maybe_trigger_auto_stop(self, stage_name, max_score, context=None):
        if not self.auto_stop_enabled:
            return

        if max_score is None or max_score >= self.auto_stop_focus_threshold:
            return

        context = context or {}
        obj = context.get("obj", self.obj)
        obj_label = f"{obj}x" if obj is not None else "unknown"
        smear = context.get("smear", getattr(self.filename, "smear_id", None))
        fov = context.get("fov", self.focus_view)

        reason = (
            "AUTO-STOP: Focus score below threshold. "
            f"stage={stage_name}, obj={obj_label}, smear={smear}, fov={fov}, "
            f"score={max_score:.2f}, threshold={self.auto_stop_focus_threshold:.2f}. "
            "Possible light/endstop malfunction."
        )

        self.auto_stop_reason = reason
        self.logger(reason)
        update_status(reason)
        update_scoreboard(status="autostopped")
        self.stop()
        raise RuntimeError(reason)

    def start_imaging(self):
        lc.turn_on()
        self.logger("Light ON, starting motor routine...")
        time.sleep(5)

    def stop_imaging(self):
        lc.turn_off()
        self.logger("Light OFF, motor routine complete.")

    # Function to send G code commands
    def send_gcode_command(self, gcode_command: str):
        # Sends a G-code command to Klipper via Moonraker
        print(f"Sending G-code: {gcode_command}")
        payload = {"script": gcode_command}
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(self.gcode_api_url, data=json.dumps(payload), headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError:
            print(f"Error: Could not connect to Moonraker at {self.gcode_api_url}.")
            print("Please ensure Klipper and Moonraker are running and the IP address/port are correct.")
            return None
        except requests.exceptions.HTTPError as e:
            print(f"HTTP Error: {e}")
            print(f"Response content: {response.text}")
            return None
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            return None

    # Function to calculate X-axis motor position for a give location in MM from home
    def calculate_motor_position_x(self, microscope_x_units):
        printer_relative_x_units = microscope_x_units - self.x_offset
        return printer_relative_x_units * self.rot_distance_x

    # Function to calculate Y-axis motor position for a give location in MM from home - Two Slide approach
    def calculate_motor_position_y(self, microscope_y_units):
        # Apply the slide offset (0 for Slide 1, 25 for Slide 2)
        adjusted_y = microscope_y_units + self.slide_y_offset
        printer_relative_y_units = adjusted_y - self.y_offset
        return printer_relative_y_units * self.rot_distance_y

    # Function to calculate Z-axis motor position for a give location in uM (micron) from home
    def calculate_motor_position_z(self, microscope_z_units):
        printer_relative_z_units = microscope_z_units - self.z_offset
        return printer_relative_z_units * self.rot_distance_z

    # Function to home axes
    def home_axis(self, axes: str):
        if "X" in axes or "Y" in axes:
            self.logger("Homing carousel to prevent X and Y-Axis from colliding with objective.")
            self.home_carousel()
        else:
            pass

        self.logger(f"Homing axis: {axes}")
        self.send_gcode_command(f"G28 {axes}")
        self.send_gcode_command("M400")
        time.sleep(5)

        # setting coordinate system to absolute positioning
        self.send_gcode_command("G90")
        time.sleep(0.1)

    def home_carousel(self):
        self.logger("Homing objective carousel...")
        self.send_gcode_command("MANUAL_STEPPER STEPPER=carousel MOVE=-100 SPEED=10 STOP_ON_ENDSTOP=1")
        self.send_gcode_command("MANUAL_STEPPER STEPPER=carousel SET_POSITION=0")
        self.send_gcode_command("M400")
        time.sleep(0.5)
        self.disable_carousel()

    # Function to move X, Y, or Z axis to target location
    def move_command(self, target_axis, target_movement):
        if target_axis.lower() == "carousel":
            move_command = f"MANUAL_STEPPER STEPPER=carousel MOVE={target_movement:.3f} SPEED=10"
        else:
            move_command = f"G0 {target_axis}{target_movement:.3f} F{self.movement_speed_mm_s * 60}"
        self.send_gcode_command(move_command)
        self.send_gcode_command("M400")  # Wait for movement to stop
        time.sleep(0.5)

    # Function to set objective type
    def set_objective(self, pos: str):
        if pos == "1":
            self.logger(f"Setting objective to 10x. Exposure Time:{self.exposure_time_10x}")
            self.obj = 10
            self.imager.set_exposure_time(self.exposure_time_10x)
            time.sleep(2)
            self.imager.get_status()
        elif pos == "2":
            self.logger(f"Setting objective to 20x. Exposure Time:{self.exposure_time_20x}")
            self.obj = 20
            self.imager.set_exposure_time(self.exposure_time_20x)
            time.sleep(2)
            self.imager.get_status()
        elif pos == "3":
            self.logger(f"Setting objective to 40x. Exposure Time:{self.exposure_time_40x}")
            self.obj = 40
            self.imager.set_exposure_time(self.exposure_time_40x)
            time.sleep(2)
            self.imager.get_status()
        else:
            self.logger(f"{pos} is not a valid input. Can't set objective.")

    # Function to set smear id and boundary
    def set_smear_id(self, smear_id):
        if smear_id == "SM1":
            self.smear_x_min = self.sm1_x_min
            self.smear_x_max = self.sm1_x_max
        elif smear_id == "SM2":
            self.smear_x_min = self.sm2_x_min
            self.smear_x_max = self.sm2_x_max
        elif smear_id == "SM3":
            self.smear_x_min = self.sm3_x_min
            self.smear_x_max = self.sm3_x_max
        else:
            self.logger(f"{smear_id} is not a valid smear ID")

        self.logger(f"Smear ID set to {smear_id}")
        self.filename.set_smear_id(smear_id)
        update_scoreboard(smear=smear_id, fov=None, status="imaging")

    # Function to move x-axis
    def move_x_axis(self, x_pos):
        self.logger(f"Moving x-axis to position {x_pos}mm")
        target_x_mm = self.calculate_motor_position_x(x_pos)
        self.move_command(self.target_axis_x, target_x_mm)
        self.current_x = x_pos

    # Function to move y-axis
    def move_y_axis(self, y_pos):
        target_y_mm = self.calculate_motor_position_y(y_pos)

        y_pos = y_pos + self.slide_y_offset
        self.logger(f"Moving y-axis to position {y_pos}mm")
        self.move_command(self.target_axis_y, target_y_mm)
        self.current_y = y_pos

    # Function to move z-axis
    def move_z_axis(self, z_pos):
        self.logger(f"Moving z-axis to position {z_pos}um")
        target_z_um = self.calculate_motor_position_z(z_pos)
        self.move_command(self.target_axis_z, target_z_um)
        self.current_z = z_pos

    def disable_carousel(self):
        self.send_gcode_command("MANUAL_STEPPER STEPPER=carousel ENABLE=0")

    def move_carousel(self, pos: str):
        self.logger("Moving objective motor")
        if pos == "1":
            self.logger("Moving carousel to the 10x objective")
            self.home_carousel()
            self.set_objective(pos)
            self.disable_carousel()
        elif pos == "2":
            self.logger("Moving carousel to the 20x objective")
            self.move_command("carousel", 35)
            self.set_objective(pos)
            self.disable_carousel()
        elif pos == "3":
            self.logger("Moving carousel to the 40x objective")
            self.move_command("carousel", 70)
            self.set_objective(pos)
            self.disable_carousel()
        else:
            self.logger(f"{pos} is not a valid input. Can't move carousel")

    def capture_image(self, z):
        image_filename, file_path = self.filename.data_filename_generator(self.focus_view, self.obj, self.current_x, self.current_y, z)
        self.logger(f"Taking image! Filename: {image_filename}, File Path: {file_path}")
        self.imager.take_rpi_image(100, image_filename, file_path, z_height=z, magnification=self.obj)
        time.sleep(15)

        # sending image to Web UI
        image_tif_path = os.path.join(file_path, f"{image_filename}.tif")
        self.imager.update_latest_image_to_jpg(image_tif_path)

        return image_filename, file_path

    def focus_scan(self, start, end, step, take_image=False):
        self.home_axis("Z")
        focus_scores = []
        for z in range(start, end + 1, step):
            self.move_z_axis(z)
            time.sleep(0.5)
            score = float(self.imager.get_focus_score())
            focus_scores.append((z, score))
            time.sleep(1)

            if take_image:
                self.capture_image(z)

        self.logger("Z (µm)\tFocus Score")
        for z, score in focus_scores:
            self.logger(f"{z}\t{score:.2f}")

        current_z_focus, current_max_score = max(focus_scores, key=lambda x: x[1])
        first_z, first_score = focus_scores[0]
        last_z, last_score = focus_scores[-1]

        if last_z == current_z_focus:
            self.logger("Max score at last Z in range. Extending scan forward...")
            next_z = last_z + step

            while True:
                self.move_z_axis(next_z)
                time.sleep(1)
                score = float(self.imager.get_focus_score())
                focus_scores.append((next_z, score))
                time.sleep(1)

                if take_image:
                    self.capture_image(next_z)

                if score < current_max_score:
                    break
                else:
                    current_z_focus, current_max_score = next_z, score
                    next_z += step

            self.logger("Extended Focus Scores:")
            for z, score in focus_scores:
                self.logger(f"{z}\t{score:.2f}")

        if first_z == current_z_focus:
            self.logger("Max score at first Z in range. Extending scan backward...")
            next_z = first_z - step

            while next_z >= 0:  # Prevent going below zero
                self.move_z_axis(next_z)
                time.sleep(1)
                score = float(self.imager.get_focus_score())
                focus_scores.insert(0, (next_z, score))
                time.sleep(1)

                if take_image:
                    self.capture_image(next_z)

                if score < current_max_score:
                    break
                else:
                    current_z_focus, current_max_score = next_z, score
                    next_z -= step

            self.logger("Extended Focus Scores:")
            for z, score in focus_scores:
                self.logger(f"{z}\t{score:.2f}")

        return current_z_focus, current_max_score, focus_scores

    def scan_z_axis_for_focus(self, take_image=False):
        if self.obj == 10:
            start_scan_pos = self.focus_preset_10x
            end_scan_pos = self.focus_preset_10x + 100
        elif self.obj == 20:
            start_scan_pos = self.focus_preset_20x
            end_scan_pos = self.focus_preset_20x + 100
        elif self.obj == 40:
            start_scan_pos = self.focus_preset_40x
            end_scan_pos = self.focus_preset_40x + 100

        for i in range(3):
            if i == 2:
                coarse_z_focus, coarse_max_score, focus_scores = self.focus_scan(10, 600, 10)
            else:
                coarse_z_focus, coarse_max_score, focus_scores = self.focus_scan(start_scan_pos, end_scan_pos, 10)
            _ , max_score = max(focus_scores, key=lambda x: x[1])
            _ , min_score = min(focus_scores, key=lambda x: x[1])
            if (max_score - min_score) > 5.0 or i == 2:
                self.logger(f"Max coarse score: {coarse_max_score:.2f} at z = {coarse_z_focus} µm")
                break
            else:
                self.logger(f"The max {max_score} and min {min_score} score are too close")
                self.logger("Repeating coarse focus scan")

        for j in range(3):
            fine_start = coarse_z_focus - 10
            fine_end = coarse_z_focus + 10
            fine_z_focus, fine_max_score, _ = self.focus_scan(fine_start, fine_end, 2)
            if fine_max_score > coarse_max_score or j == 2:
                self.logger(f"Max fine score: {fine_max_score:.2f} at z = {fine_z_focus} µm")
                break
            else:
                self.logger(f"Max fine score ({fine_max_score}) is less than max coarse score ({coarse_max_score})")
                self.logger("Repeating fine focus scan")

        for k in range(3):
            # --- New for 20 and 40x zstack ---
            if self.obj == 20 or self.obj == 40:
                super_fine_start = fine_z_focus - self.points_before
                super_fine_end = fine_z_focus + self.points_after
            else:
                super_fine_start = fine_z_focus - 5
                super_fine_end = fine_z_focus + 5

            if take_image:
                super_fine_z_focus, super_fine_max_score, final_focus_scores = self.focus_scan(super_fine_start, super_fine_end, 1, True)
            else:
                super_fine_z_focus, super_fine_max_score, final_focus_scores = self.focus_scan(super_fine_start,super_fine_end, 1)

            if super_fine_max_score >= (0.9 * fine_max_score) or k == 2:
                self.logger(f"Final max score: {super_fine_max_score:.2f} at z = {super_fine_z_focus} µm")
                break
            else:
                self.logger(f"Max super fine score ({super_fine_max_score}) is less than max fine score ({fine_max_score})")
                self.logger("Repeating super fine focus scan")

        self.maybe_trigger_auto_stop("super_fine", super_fine_max_score)

        return super_fine_z_focus, super_fine_max_score, final_focus_scores

    def go_to_random_position(self):
        random_x_pos = random.randint(self.start_x, self.end_x)
        random_y_pos = random.randint(self.start_y, self.end_y)

        self.logger(f"Moving to random position {random_x_pos}, {random_y_pos} to check for bacteria")
        self.move_x_axis(random_x_pos)
        self.move_y_axis(random_y_pos)

        return random_x_pos, random_y_pos

    def go_to_random_position_milestone5(self):
        random_x_pos = random.randint(self.smear_x_min, self.smear_x_max)
        random_y_pos = random.randint(self.y_min, self.y_max)

        self.logger(f"Moving to random position {random_x_pos}, {random_y_pos} to check for bacteria")
        self.move_x_axis(random_x_pos)
        self.move_y_axis(random_y_pos)

        return random_x_pos, random_y_pos

    def take_background_image(self):
        self.home_axis("X")
        self.move_x_axis(120)

        for i in range(3):
            self.check_stop()

            self.move_carousel(f"{i+1}")
            background_filename, file_path = self.filename.background_filename_generator(self.obj)
            self.logger(f"Taking background image for {self.obj}x objective. Filename: {background_filename}")
            self.imager.take_rpi_image(100, background_filename, file_path, magnification=self.obj)
            time.sleep(15)
            self.logger(f"Background image taken for {self.obj}x objective")
            
            # Appending background image metadata to the no-slide JSON file
            correction_json_path = os.path.join(file_path, "no-slide.json")
            image_json_path = os.path.join(file_path, f"{background_filename}.json")
            append_correction_image_to_json("no-slide", correction_json_path, image_json_path)

        self.logger("Background images taken for all objectives")

    def take_darkfield_image(self):
        for i in range(3):
            self.check_stop()

            self.move_carousel(f"{i+1}")
            dark_filename, file_path = self.filename.darkfield_filename_generator(self.obj)
            self.logger(f"Taking darkfield image for {self.obj}x objective. Filename: {dark_filename}")
            self.imager.take_rpi_image(100, dark_filename, file_path, magnification=self.obj)
            time.sleep(15)
            self.logger(f"Darkfield image taken for {self.obj}x objective")

            # Appending darkfield image metadata to the no-light JSON file
            correction_json_path = os.path.join(file_path, "no-light.json")
            image_json_path = os.path.join(file_path, f"{dark_filename}.json")
            append_correction_image_to_json("no-light", correction_json_path, image_json_path)
            
        self.logger("Darkfield images taken for all objectives")

    def take_correction_images(self):
        self.start_imaging()
        time.sleep(15)

        self.take_background_image()

        # send 10x to mac here
        background_filename_10x, background_file_path_10x = self.filename.background_filename_generator(10)
        self.logger(f"Sending {background_filename_10x} to Mac")
        check = send_background_image_to_mac(background_filename_10x, background_file_path_10x)
        if check:
            self.logger(f"{background_filename_10x} succesfully sent to mac")
        else:
            self.logger(f"ERROR: {background_filename_10x} failed to send to mac. CHECK LOGS")

        self.stop_imaging()
        time.sleep(15)
        self.take_darkfield_image()

        darkfield_filename_10x, darkfield_file_path_10x = self.filename.darkfield_filename_generator(10)
        self.logger(f"Sending {darkfield_filename_10x} to Mac")
        check = send_darkfield_image_to_mac(darkfield_filename_10x, darkfield_file_path_10x)
        if check:
            self.logger(f"{darkfield_filename_10x} succesfully sent to mac")
        else:
            self.logger(f"ERROR: {darkfield_filename_10x} failed to send to mac. CHECK LOGS")

    def complete_zstack(self, focus_scores):
        self.logger("Completing the Zstack...")
        self.logger("Z (µm)\tFocus Score")
        for z, score in focus_scores:
            self.logger(f"{z}\t{score:.2f}")

        points_before = self.points_before
        points_after = self.points_after

        z_values = [z for z, score in focus_scores]
        scores = [score for z, score in focus_scores]
        z_focus, max_score = max(focus_scores, key=lambda x: x[1])
        max_index = z_values.index(z_focus)

        self.logger(f"Max focus score at z = {z_focus}. {max_index}th index in list")

        lowest_z = z_values[0]
        highest_z = z_values[-1]
        list_len = len(focus_scores)

        missing = 0
        direction = "none"
        final_target_z = None

        if max_index < points_before:
            missing = points_before - max_index
            final_target_z = lowest_z - missing
            direction = "backward"
            self.logger(f"Need to move {missing} positions from first position. {final_target_z} is new start position")

        elif max_index > list_len - 1 - points_after:
            missing = (max_index + points_after) - (list_len - 1)
            final_target_z = highest_z + missing
            direction = "forward"
            self.logger(f"Need to move {missing} positions from end position. {final_target_z} is new final position ")

        if direction == "forward":
            for step in range(1, missing + 1):
                next_z = highest_z + step
                self.move_z_axis(next_z)
                time.sleep(0.5)
                self.logger(f"Taking zstack image at: z = {next_z} µm")
                self.capture_image(next_z)
            self.filename.image_cleanup(self.focus_view, self.obj, z_focus, self.current_x, self.current_y, points_before, points_after)

        elif direction == "backward":
            self.move_z_axis(lowest_z)
            print(f"Moving back to start position z = {lowest_z}")
            for step in range(1, missing + 1):
                next_z = lowest_z - step
                if next_z >= 0:
                    self.move_z_axis(next_z)
                    time.sleep(0.5)
                    self.logger(f"Taking zstack image at: z = {next_z} µm")
                    self.capture_image(next_z)
            self.filename.image_cleanup(self.focus_view, self.obj, z_focus, self.current_x, self.current_y, points_before, points_after)

        else:
            self.logger("No additional scanning required — max focus score is centered.")

        # Populate zstack JSON with surviving image JSONs (post-cleanup), sorted by z
        self.logger(f"Populating zstack JSON from folder {os.path.basename(self.zstack_folder_path)}...")
        populate_zstack_json_from_folder(self.zstack_folder_path)

    def first_scan_for_focus_preset(self, smear_list):
        self.home_axis("X, Y")

        current_center_y = c.SLIDE_1_CENTER_Y
        self.move_y_axis(current_center_y)

        self.move_carousel("1")

        for smear_id in smear_list:
            if smear_id == "SM1":
                self.move_x_axis(c.SLIDE_1_SM1_CENTER_X)
            elif smear_id == "SM2":
                self.move_x_axis(c.SLIDE_1_SM2_CENTER_X)
            elif smear_id == "SM3":
                self.move_x_axis(c.SLIDE_1_SM3_CENTER_X)
            else:
                self.logger("Smear ID can't be identified. ERROR")

            coarse_z_focus, coarse_max_score, focus_scores = self.focus_scan(0, 600, 20)
            _ , max_score = max(focus_scores, key=lambda x: x[1])
            _ , min_score = min(focus_scores, key=lambda x: x[1])
            self.maybe_trigger_auto_stop("preset_coarse", coarse_max_score, {"smear": smear_id, "obj": 10, "fov": 0})

            if (max_score - min_score) > 10.0:
                self.focus_preset_10x = coarse_z_focus - 60
                self.focus_preset_20x = self.focus_preset_10x + c.FOCUS_OFFSET_20X_FROM_10X_PRESET
                self.focus_preset_40x = self.focus_preset_10x + c.FOCUS_OFFSET_40X_FROM_10X_PRESET
                self.logger(f"Max coarse score: {coarse_max_score:.2f} at z = {coarse_z_focus} µm. Setting focus presets")
                self.logger(f"10x: {self.focus_preset_10x}, 20x: {self.focus_preset_20x}, 40x: {self.focus_preset_40x}")
                return self.focus_preset_10x, self.focus_preset_20x, self.focus_preset_40x 
            else:
                self.logger(f"The max {max_score} and min {min_score} score are too close")
                self.logger("Repeating coarse focus scan")

        # only set variables here after going to SM3 and the focus scores are too close
        self.focus_preset_10x = coarse_z_focus - 40
        self.focus_preset_20x = self.focus_preset_10x + c.FOCUS_OFFSET_20X_FROM_10X_PRESET
        self.focus_preset_40x = self.focus_preset_10x + c.FOCUS_OFFSET_40X_FROM_10X_PRESET
        self.logger(f"Max coarse score: {coarse_max_score:.2f} at z = {coarse_z_focus} µm. Setting focus presets")
        self.logger(f"10x: {self.focus_preset_10x}, 20x: {self.focus_preset_20x}, 40x: {self.focus_preset_40x}")
        return self.focus_preset_10x, self.focus_preset_20x, self.focus_preset_40x

    def collect_data_xy(self, smear_list, xy_coords):
        self.start_imaging()

        for i in range(len(smear_list)):
            smear_id = smear_list[i]
            self.set_smear_id(smear_id)
            self.focus_view = 0

            self.first_scan_for_focus_preset([smear_id])

            for j in range(len(xy_coords[i])):
                x_pos = xy_coords[i][j][0]
                y_pos = xy_coords[i][j][1]

                self.logger(f"Collecting data at {x_pos}, {y_pos + self.slide_y_offset} in {smear_id}")

                self.check_stop()
                self.focus_view += 1
                update_scoreboard(fov=self.focus_view, status="imaging")

                self.home_axis("X, Y")
                self.move_x_axis(x_pos)
                self.move_y_axis(y_pos)
                self.check_stop()

                # code logic for implementation with the focus check QC
                #stages = [(self.collect_data_with_10x, "10x", None), (self.collect_data_with_20x_40x, "20x", 2), (self.collect_data_with_20x_40x, "40x", 3)]
                stages = [(self.collect_data_with_20x_40x, "20x", 2), (self.collect_data_with_20x_40x, "40x", 3)]
                #stages = [(self.collect_data_with_20x_40x, "40x", 3)]
                for collect_func, name, arg in stages:
                    passed_qc = False

                    # Try up to 2 times
                    for attempt in range(1, 3):
                        self.logger(f"Running {name} collection (Attempt {attempt}/2)")

                        # Run the collection (with or without argument)
                        if arg is not None:
                            collect_func(arg)
                        else:
                            collect_func()

                        self.logger("Running QC check on zstack")
                        if self.qc_check_focus():
                            self.logger(f"{name} QC Passed.")
                            self.initiate_transfer_queue(self.focus_view, self.obj)
                            passed_qc = True
                            break # Success! Exit the retry loop and go to next stage
                        else:
                            self.logger(f"{name} QC Failed on attempt {attempt}.")
                            self.handle_failed_qc()
                            if attempt == 1:
                                self.logger(f"Retrying {name} collection...")
                            else:
                                self.logger(f"{name} failed twice. Moving to next objective/step.")
                                self.initiate_transfer_queue(self.focus_view, self.obj)

        self.logger("Data collection finished. All images have been taken and saved to Images folder")
        self.filename.copy_correction_folders_to_slide_case()
        mark_slide_done(self.filename.barcode, self.filename.date)
        self.stop_imaging()

        # move x-y axis to position that allows for easier removal of slides only if 2nd slide was imaged
        if self.slide_y_offset == 25: # this is how we determine if 2nd slide was being imaged
            self.move_x_axis(130)
            self.move_y_axis(15)
        else:
            pass

    def collect_data_with_search_algorithm(self, smear_list, fov_list):
        self.start_imaging()
        for i in range(len(smear_list)):
            smear_id = smear_list[i]
            self.set_smear_id(smear_id)
            fov_target = fov_list[i]

            self.first_scan_for_focus_preset([smear_id])

            center_x, center_y = self.get_smear_center(smear_id)
            search_coords = self.generate_spiral(center_x, center_y)

            self.focus_view = 0
            total_positions = len(search_coords)
            for k in range(len(search_coords)):
                if self.focus_view >= fov_target:
                    self.logger(f"Reached target of {fov_target} FOVs for smear {smear_id}. Moving to next smear.")
                    break

                x_pos = search_coords[k][0]
                y_pos = search_coords[k][1]
                self.logger(f"Position {k+1}/{total_positions}: Searching for good fov at {x_pos},{y_pos + self.slide_y_offset} (Found: {self.focus_view}/{fov_target})")

                #self.home_axis("X, Y")
                self.move_x_axis(x_pos)
                self.move_y_axis(y_pos)
                self.check_stop()

                z_focus_10x, _, _ = self.scan_z_axis_for_focus()
                self.move_z_axis(z_focus_10x)

                scanning_filename = self.filename.scanning_filename_generator(self.current_x, self.current_y, z_focus_10x)
                self.logger(f"Taking image! Filename: {scanning_filename}")
                self.imager.take_rpi_image(100, scanning_filename, magnification=self.obj)
                time.sleep(15)
                self.imager.update_latest_image_to_jpg(os.path.join(c.PI_IMAGE_DIR, f"{scanning_filename}.tif"))

                json_path = send_image_to_mac(f"{scanning_filename}.tif")
                if json_path:
                    self.logger(f"Received json from mac: {os.path.basename(json_path)}")
                    result, x_coord, y_coord = read_json(json_path)
                    self.logger(f"Data found in json file: tile_5 = {result}, at {x_coord},{y_coord}")
                    if result == "1":
                        self.logger(f"Good fov detected at {x_pos},{y_coord}. Taking zstacks!")
                        self.filename.append_csv(self.current_x, self.current_y, self.current_z, True)
                        self.focus_view += 1
                        update_scoreboard(fov=self.focus_view, status="imaging")

                        # code logic for implementation with the focus check QC
                        #stages = [(self.collect_data_with_10x, "10x", None), (self.collect_data_with_20x_40x, "20x", 2), (self.collect_data_with_20x_40x, "40x", 3)]
                        stages = [(self.collect_data_with_10x, "20x", 2), (self.collect_data_with_20x_40x, "40x", 3)]
                        #stages = [(self.collect_data_with_20x_40x, "40x", 3)]
                        for collect_func, name, arg in stages:
                            passed_qc = False

                            # Try up to 2 times
                            for attempt in range(1, 3):
                                self.logger(f"Running {name} collection (Attempt {attempt}/2)")

                                # Run the collection (with or without argument)
                                if arg is not None:
                                    collect_func(arg)
                                else:
                                    collect_func()

                                self.logger("Running QC check on zstack")
                                if self.qc_check_focus():
                                    self.logger(f"{name} QC Passed.")
                                    self.initiate_transfer_queue(self.focus_view, self.obj)
                                    passed_qc = True
                                    break # Success! Exit the retry loop and go to next stage
                                else:
                                    self.logger(f"{name} QC Failed on attempt {attempt}.")
                                    self.handle_failed_qc()
                                    if attempt == 1:
                                        self.logger(f"Retrying {name} collection...")
                                    else:
                                        self.logger(f"{name} failed twice. Moving to next objective/step.")
                                        self.initiate_transfer_queue(self.focus_view, self.obj)
                        self.move_carousel("1")
                    else:
                        self.logger(f"Fov rejected at {x_pos},{y_coord}. Moving to next position")
                        self.filename.append_csv(self.current_x, self.current_y, self.current_z, False)
                else:
                    self.logger(f"No json file received. CHECK LOGS FOR ERROR")

            else:
                # EXHAUSTION CONDITION: This runs ONLY if the loop finishes without hitting the 'break' (meaning focus_view < fov_target)
                self.logger(f"ATTENTION: Exhausted all {len(search_coords)} positions "
                            f"but only found {self.focus_view}/{fov_target} good FOVs "
                            f"for smear {smear_id}.")

        self.logger("Data collection finished. All images have been taken and saved to Images folder")
        self.filename.copy_correction_folders_to_slide_case()
        mark_slide_done(self.filename.barcode, self.filename.date)
        self.stop_imaging()

        # move x-y axis to position that allows for easier removal of slides only if 2nd slide was imaged
        if self.slide_y_offset == 25: # this is how we determine if 2nd slide was imaged
            self.move_x_axis(130)
            self.move_y_axis(15)
        else:
            pass

    def collect_data_manual_fov(self, x_pos, y_pos, objectives, smear_id, fov_target, transfer=False):
        """Image a single FOV at the supplied (x, y) without moving X/Y motors.

        The caller must initialize the FileTransfer5 instance and set the barcode
        before calling this method. Folders, manifest, z-stacks, QC, and transfer
        are all handled inside this method.
        """
        self.start_imaging()

        self.set_smear_id(smear_id)
        self.focus_view = fov_target

        # Use the supplied coordinates without moving the X/Y motors
        self.current_x = x_pos
        self.current_y = y_pos

        # Generate folder structure for this barcode / smear / single FOV
        generate_barcode_folders(
            self.filename.barcode,
            [smear_id],
            [self.focus_view],
            run_date=self.filename.date,
        )

        # Create the manifest for this barcode run
        create_manifest_json(self.filename)

        # Run a 10x focus-preset scan at the current position
        self.move_carousel("1")
        coarse_z_focus, coarse_max_score, focus_scores = self.focus_scan(0, 600, 20)
        _, max_score = max(focus_scores, key=lambda x: x[1])
        _, min_score = min(focus_scores, key=lambda x: x[1])
        self.maybe_trigger_auto_stop("manual_preset_coarse", coarse_max_score, {"smear": smear_id, "obj": 10, "fov": 1})

        if (max_score - min_score) > 10.0:
            self.focus_preset_10x = coarse_z_focus - 60
        else:
            self.focus_preset_10x = coarse_z_focus - 40
        self.focus_preset_20x = self.focus_preset_10x + c.FOCUS_OFFSET_20X_FROM_10X_PRESET
        self.focus_preset_40x = self.focus_preset_10x + c.FOCUS_OFFSET_40X_FROM_10X_PRESET
        self.logger(f"Manual FOV focus presets set - 10x: {self.focus_preset_10x}, 20x: {self.focus_preset_20x}, 40x: {self.focus_preset_40x}")

        # Collect z-stacks for each requested objective
        for obj in objectives:
            self.check_stop()

            if obj == 10:
                self.collect_data_with_10x()
            elif obj == 20:
                self.collect_data_with_20x_40x(2)
            elif obj == 40:
                self.collect_data_with_20x_40x(3)
            else:
                self.logger(f"Objective {obj}x is not supported. Skipping.")
                continue

            if transfer:
                self.initiate_transfer_queue(self.focus_view, self.obj)

        self.logger("Manual FOV data collection finished. All images have been taken and saved to Images folder")
        if transfer:
            self.filename.copy_correction_folders_to_slide_case()
            mark_slide_done(self.filename.barcode, self.filename.date)
        self.stop_imaging()

    def collect_exposure_series_manual_fov(
        self, x_pos, y_pos, z_pos, magnification, smear_id, fov_number
    ):
        """Capture 100 separate one-frame images at a manually positioned FOV.

        X, Y, and Z are recorded as image metadata; the corresponding stage axes
        are not moved. The objective carousel is moved to the requested 20x or
        40x magnification. Images use the standard data filename and folder
        schema, with ``_image1`` through ``_image100`` appended to each name.
        """
        if magnification not in (20, 40):
            raise ValueError("Exposure-series capture supports 20x and 40x only")
        if fov_number < 1:
            raise ValueError("fov_number must be at least 1")

        self.focus_view = fov_number
        self.current_x = x_pos
        self.current_y = y_pos
        self.current_z = z_pos
        self.set_smear_id(smear_id)

        # Reuse the current folder schema; do not add any magnification folders.
        generate_barcode_folders(
            self.filename.barcode,
            [smear_id],
            [fov_number],
            run_date=self.filename.date,
        )
        create_manifest_json(self.filename)

        image_filenames = []
        self.start_imaging()
        try:
            carousel_position = "2" if magnification == 20 else "3"
            self.move_carousel(carousel_position)

            base_filename, file_path = self.filename.data_filename_generator(
                fov_number, magnification, x_pos, y_pos, z_pos
            )

            for image_number in range(1, 101):
                self.check_stop()
                image_filename = f"{base_filename}_image{image_number}"
                tif_path = os.path.join(file_path, f"{image_filename}.tif")
                json_path = os.path.join(file_path, f"{image_filename}.json")
                previous_stamps = {
                    path: os.stat(path).st_mtime_ns if os.path.exists(path) else None
                    for path in (tif_path, json_path)
                }

                self.logger(
                    f"Capturing image {image_number}/100 at {magnification}x: "
                    f"{image_filename}"
                )
                self.imager.take_rpi_image(
                    1,
                    image_filename,
                    file_path,
                    z_height=z_pos,
                    magnification=magnification,
                )

                # Camera capture is asynchronous. Wait for this image's TIFF and
                # metadata JSON before submitting the next capture request.
                deadline = time.monotonic() + 60
                while True:
                    self.check_stop()
                    current_stamps = {
                        path: os.stat(path).st_mtime_ns if os.path.exists(path) else None
                        for path in (tif_path, json_path)
                    }
                    if all(
                        current_stamps[path] is not None
                        and current_stamps[path] != previous_stamps[path]
                        for path in (tif_path, json_path)
                    ):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for camera output for {image_filename}"
                        )
                    time.sleep(0.1)

                image_filenames.append(image_filename)

            self.logger("100-image exposure series complete")
            return image_filenames
        finally:
            self.stop_imaging()

    def collect_motion_blur_data(
        self,
        x_pos=None,
        y_pos=None,
        z_pos=None,
        magnification=40,
        smear_id=None,
        fov_number=1,
        nframes=1,
        image_count=100,
        exposure_time=None,
        queue_capacity=None,
        width=None,
        series_count=1,
        inter_series_pause_s=0.0,
    ):
        """Capture a continuous series of multi-frame averages for motion-blur analysis.

        The slide barcode must already be set on ``self.filename``. X, Y, and Z
        are metadata only and the stage axes are not moved; omitted coordinates
        are stored as JSON null and represented as ``NA`` in image filenames.

        One ``start_series`` request is sent to the camera service, which
        captures ``image_count`` output images of ``nframes`` frames each and
        writes one TIFF plus JSON sidecar per image (``_image1`` ... ``_imageN``).
        Individual image JSON files are retained and are not consolidated into
        the z-stack JSON. ``queue_capacity`` and ``width`` are sent only when
        given, so the camera service defaults apply otherwise.

        Returns ``{"series_id": ..., "status": <final status snapshot>}`` only
        when the series is complete and every requested image was written.
        Raises RuntimeError on rejection, timeout, stall, stop, overload, or any
        other non-complete outcome. The light is turned off on every exit path
        once imaging has started.

        Repeated series: with ``series_count`` > 1, one ``start_series_batch``
        request asks the camera to capture ``series_count`` series of
        ``image_count`` images each. The camera schedules the pause itself:
        the first frame of series *i + 1* has a sensor timestamp at least
        ``inter_series_pause_s`` after the last frame of series *i*. The pause
        does not include client polling or file writing, and is a minimum that
        is rounded up to the camera's frame cadence. Files are named
        ``<basename>_series<S>_image<I>``. The light stays on for the whole
        batch. On success this returns
        ``{"batch_id": ..., "series_ids": [...], "status": ...}``.

        With ``series_count`` == 1 (the default), the single-series behavior
        above is unchanged and ``inter_series_pause_s`` is not used.
        """
        if magnification not in (20, 40):
            raise ValueError("Motion-blur capture supports 20x and 40x only")
        if not isinstance(smear_id, str) or not smear_id:
            raise ValueError("smear_id must be provided")
        if isinstance(fov_number, bool) or not isinstance(fov_number, int) or fov_number < 1:
            raise ValueError("fov_number must be a positive integer")
        if isinstance(nframes, bool) or not isinstance(nframes, int) or nframes < 1:
            raise ValueError("nframes must be a positive integer")
        if isinstance(image_count, bool) or not isinstance(image_count, int) or image_count < 1:
            raise ValueError("image_count must be a positive integer")
        if exposure_time is not None and (
            isinstance(exposure_time, bool)
            or not isinstance(exposure_time, int)
            or exposure_time <= 0
        ):
            raise ValueError("exposure_time must be a positive integer in microseconds")
        for name, value in (("queue_capacity", queue_capacity), ("width", width)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(series_count, bool) or not isinstance(series_count, int) or series_count < 1:
            raise ValueError("series_count must be a positive integer")
        if (
            isinstance(inter_series_pause_s, bool)
            or not isinstance(inter_series_pause_s, (int, float))
            or not math.isfinite(inter_series_pause_s)
            or inter_series_pause_s < 0
        ):
            raise ValueError("inter_series_pause_s must be a finite, non-negative number of seconds")
        is_batch = series_count > 1
        inter_series_pause_us = int(round(inter_series_pause_s * 1_000_000))
        total_images = image_count * series_count

        self.focus_view = fov_number
        self.current_x = x_pos
        self.current_y = y_pos
        self.current_z = z_pos
        self.set_smear_id(smear_id)

        # Use the current barcode/smear/FOV/objective folder schema unchanged.
        generate_barcode_folders(
            self.filename.barcode,
            [smear_id],
            [fov_number],
            run_date=self.filename.date,
        )
        create_manifest_json(self.filename)

        zstack_folder_path = self.filename.data_path_generator(fov_number, magnification)
        create_zstack_json(
            zstack_folder_path,
            x_pos,
            y_pos,
            fov_number,
            magnification,
            smear_id,
        )

        # Preserve numeric coordinate tokens when supplied, and use NA for
        # missing coordinates without changing FileTransfer5's naming schema.
        filename_x = "NA" if x_pos is None else x_pos
        filename_y = "NA" if y_pos is None else y_pos
        filename_z = "NA" if z_pos is None else z_pos
        base_filename, file_path = self.filename.data_filename_generator(
            fov_number,
            magnification,
            filename_x,
            filename_y,
            filename_z,
        )

        series_metadata = {
            "x_pos": x_pos,
            "y_pos": y_pos,
            "z_pos": z_pos,
            "smear_id": smear_id,
            "fov": fov_number,
            "barcode": self.filename.barcode,
        }
        optional_args = {}
        if queue_capacity is not None:
            optional_args["queue_capacity"] = queue_capacity
        if width is not None:
            optional_args["width"] = width

        # Shared with the polling helper: whether a series is running, whether a
        # stop was seen, and whether the camera acknowledged a cancel.
        # For a batch, "id" holds the batch ID and the batch commands are used.
        series = {"id": None, "stop_seen": False, "cancel_sent": False, "batch": is_batch}
        noun = "batch" if is_batch else "series"
        try:
            self.start_imaging()

            carousel_position = "2" if magnification == 20 else "3"
            self.move_carousel(carousel_position)

            # move_carousel/set_objective sets the objective's default exposure;
            # an explicit experiment exposure must be applied afterward.
            if exposure_time is not None:
                self.imager.set_exposure_time(exposure_time)
                time.sleep(2)

            if self.stop_requested:
                raise RuntimeError(f"Motor stop requested before the motion-blur {noun} started.")

            if is_batch:
                self.logger(
                    f"Starting motion-blur batch: {series_count} series x {image_count} "
                    f"image(s) x {nframes} frame(s) at {magnification}x, minimum pause "
                    f"{inter_series_pause_s} s between series, basename {base_filename}, "
                    f"folder {file_path}"
                )
                reply = self.imager.start_series_batch(
                    image_count,
                    series_count,
                    nframes,
                    base_filename,
                    file_path,
                    inter_series_pause_us=inter_series_pause_us,
                    z_height=z_pos,
                    magnification=magnification,
                    metadata=series_metadata,
                    **optional_args,
                )
                id_key = "batch_id"
            else:
                self.logger(
                    f"Starting motion-blur series: {image_count} image(s) x {nframes} frame(s) "
                    f"at {magnification}x, basename {base_filename}, folder {file_path}"
                )
                reply = self.imager.start_series(
                    image_count,
                    nframes,
                    base_filename,
                    file_path,
                    z_height=z_pos,
                    magnification=magnification,
                    metadata=series_metadata,
                    **optional_args,
                )
                id_key = "series_id"
            if isinstance(reply, dict) and "timeout" in reply:
                raise RuntimeError(
                    "Timed out waiting for the camera service to accept the motion-blur "
                    f"{noun} ({reply.get('timeout')}). A {noun} may still be running on "
                    f"the camera; it cannot be cancelled because no {noun} ID was received."
                )
            if not isinstance(reply, dict) or reply.get("ok") is not True or not reply.get(id_key):
                code, message = self._series_error_fields(reply)
                raise RuntimeError(
                    f"Camera service rejected the motion-blur {noun}: {code}: {message}"
                )

            series["id"] = reply[id_key]
            self.logger(f"Motion-blur {noun} {series['id']} accepted (state={reply.get('state')})")

            status = self._poll_motion_blur_series(series, total_images)

            counts = self._series_counts_text(status, total_images)
            if is_batch:
                counts += (
                    f", completed series {status.get('completed_series')}/{series_count}"
                )
            if series["stop_seen"]:
                raise RuntimeError(
                    f"Motion-blur {noun} {series['id']} cancelled by stop request: "
                    f"state={status.get('state')}, {counts}"
                )
            if status.get("state") == "complete" and status.get("written_images") == total_images:
                if is_batch:
                    self._log_batch_boundaries(series["id"], status)
                    self.logger(
                        f"Motion-blur batch {series['id']} complete: {series_count} series x "
                        f"{image_count} image(s) written, {nframes} frame(s) per image"
                    )
                    return {
                        "batch_id": series["id"],
                        "series_ids": status.get("series_ids"),
                        "status": status,
                    }
                self.logger(
                    f"Motion-blur series {series['id']} complete: {image_count} image(s) "
                    f"written, {nframes} frame(s) per image"
                )
                return {"series_id": series["id"], "status": status}

            code, message = self._series_error_fields(status)
            error = status.get("error") if isinstance(status.get("error"), dict) else {}
            failing = f"failing image={error.get('image_index')} ({error.get('filename')})"
            if is_batch:
                failing += (
                    f", series {error.get('series_index')} image "
                    f"{error.get('series_image_index')}"
                )
            raise RuntimeError(
                f"Motion-blur {noun} {series['id']} did not complete: "
                f"state={status.get('state')}, {counts}, error={code}: {message}, {failing}"
            )
        except BaseException:
            # Never leave a started series running after a failure. A series that
            # already reached a terminal state just acknowledges the cancel.
            if series["id"] is not None and not series["cancel_sent"]:
                self._cancel_motion_blur_series(series)
            raise
        finally:
            self.stop_imaging()

    @staticmethod
    def _series_error_fields(reply):
        """Return (code, message) from a series reply's error field."""
        if not isinstance(reply, dict):
            return None, f"unexpected reply {reply!r}"
        error = reply.get("error")
        if isinstance(error, dict):
            return error.get("code"), error.get("message")
        if error is not None:
            return None, str(error)
        if reply.get("ok") is True:
            return None, None
        return None, f"unexpected reply {reply!r}"

    @staticmethod
    def _series_counts_text(status, image_count):
        if not status:
            return "no valid status received"
        return (
            f"captured {status.get('captured_images')}/{image_count}, "
            f"written {status.get('written_images')}/{image_count}"
        )

    @staticmethod
    def _series_status_problem(reply, series_id, id_key="series_id"):
        """Return why a series_status reply is unusable, or None if it is valid.

        ``id_key`` is "batch_id" when checking a series_batch_status reply.
        """
        if not isinstance(reply, dict):
            return f"malformed status reply {reply!r}"
        if "timeout" in reply:
            return f"status request timed out ({reply.get('timeout')})"
        if reply.get("ok") is not True:
            code, message = Motor._series_error_fields(reply)
            return f"status request failed: {code}: {message}"
        if reply.get(id_key) != series_id:
            noun = "batch" if id_key == "batch_id" else "series"
            return f"status reply is for {noun} {reply.get(id_key)!r}"
        if not isinstance(reply.get("state"), str) or not isinstance(reply.get("is_terminal"), bool):
            return f"malformed status reply {reply!r}"
        for key in ("requested_images", "captured_images", "written_images"):
            value = reply.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return f"malformed status reply ({key}={value!r})"
        return None

    @staticmethod
    def _format_mib(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "n/a"
        return f"{value / (1024 * 1024):.0f} MiB"

    def _log_series_progress(self, series_id, status, image_count, batch=False):
        if batch:
            prefix = (
                f"Motion-blur batch {series_id}: state={status.get('state')}, "
                f"series {status.get('current_series_index')}/{status.get('series_count')} "
                f"({status.get('series_captured_images')}/{status.get('images_per_series')} "
                f"captured"
                f"{', pausing' if status.get('awaiting_next_series') else ''}), total "
            )
        else:
            prefix = f"Motion-blur series {series_id}: state={status.get('state')}, "
        self.logger(
            f"{prefix}"
            f"{self._series_counts_text(status, image_count)}, "
            f"queue {status.get('queue_depth')}/{status.get('queue_capacity')} "
            f"(peak {status.get('queue_high_watermark')}), "
            f"process RSS {self._format_mib(status.get('process_rss_bytes'))}, "
            f"available RAM {self._format_mib(status.get('system_available_bytes'))}"
        )

    def _log_batch_boundaries(self, batch_id, status):
        """Log the measured sensor-timestamp gap at each series boundary."""
        requested = status.get("inter_series_pause_us")
        for boundary in status.get("boundaries") or []:
            gap = boundary.get("observed_gap_us")
            gap_text = "n/a" if gap is None else f"{gap / 1000:.1f} ms"
            req_text = "n/a" if requested is None else f"{requested / 1000:.1f} ms"
            self.logger(
                f"Motion-blur batch {batch_id}: gap before series "
                f"{boundary.get('before_series_index')} = {gap_text} "
                f"(requested minimum {req_text}, {boundary.get('frames_skipped')} frame(s) skipped)"
            )

    def _cancel_motion_blur_series(self, series):
        """Send cancel_series (or cancel_series_batch). Return True if acknowledged."""
        noun = "batch" if series.get("batch") else "series"
        try:
            if series.get("batch"):
                reply = self.imager.cancel_series_batch(series["id"])
            else:
                reply = self.imager.cancel_series(series["id"])
        except Exception as e:
            self.logger(
                f"Could not cancel motion-blur {noun} {series['id']}: {type(e).__name__}: {e}"
            )
            return False
        if isinstance(reply, dict) and reply.get("ok") is True:
            series["cancel_sent"] = True
            self.logger(
                f"Cancel sent for motion-blur {noun} {series['id']} (state={reply.get('state')})"
            )
            return True
        self.logger(f"Cancel of motion-blur {noun} {series['id']} not acknowledged: {reply!r}")
        return False

    def _poll_motion_blur_series(self, series, image_count):
        """Poll a started series until the camera reports a terminal state.

        Returns the final status snapshot. Progress is logged at the first
        status, on each state change, about every MOTION_BLUR_PROGRESS_LOG_S
        seconds, and at the end; never per frame. The stop flag is read
        directly because check_stop() logs on every call. On stop the series is
        cancelled and polling continues, for at most MOTION_BLUR_CANCEL_WAIT_S,
        while queued images are written. Raises RuntimeError if progress
        stalls, too many consecutive status replies are unusable, or a
        cancelled series does not finish in time.
        """
        series_id = series["id"]
        is_batch = bool(series.get("batch"))
        noun = "batch" if is_batch else "series"
        last_progress = None
        last_progress_time = time.monotonic()
        last_log_time = None
        last_state = None
        last_status = None
        bad_replies = 0
        cancel_deadline = None

        while True:
            if self.stop_requested and not series["stop_seen"]:
                series["stop_seen"] = True
                cancel_deadline = time.monotonic() + MOTION_BLUR_CANCEL_WAIT_S
                self.logger(f"Stop requested; cancelling motion-blur {noun} {series_id}")
            if series["stop_seen"] and not series["cancel_sent"]:
                # Retried on later polls if the service did not acknowledge it.
                self._cancel_motion_blur_series(series)

            if is_batch:
                reply = self.imager.series_batch_status(series_id)
                problem = self._series_status_problem(reply, series_id, id_key="batch_id")
            else:
                reply = self.imager.series_status(series_id)
                problem = self._series_status_problem(reply, series_id)
            now = time.monotonic()
            if problem is not None:
                bad_replies += 1
                self.logger(
                    f"Motion-blur {noun} {series_id}: {problem} "
                    f"({bad_replies}/{MOTION_BLUR_MAX_BAD_REPLIES})"
                )
                if bad_replies >= MOTION_BLUR_MAX_BAD_REPLIES:
                    raise RuntimeError(
                        f"Motion-blur {noun} {series_id}: {bad_replies} consecutive unusable "
                        f"status replies (last: {problem}); last known "
                        f"{self._series_counts_text(last_status, image_count)}"
                    )
            else:
                bad_replies = 0
                last_status = reply
                state = reply["state"]
                # frames_in_current_group also counts as progress, so a long
                # group (large nframes or exposure) is not mistaken for a stall.
                # For a batch, frames skipped during a pause also count, so a
                # long requested pause is not mistaken for a stall.
                progress = (
                    reply["captured_images"],
                    reply["written_images"],
                    reply.get("frames_in_current_group"),
                    reply.get("frames_skipped_in_pauses"),
                )
                if progress != last_progress:
                    last_progress = progress
                    last_progress_time = now

                if reply["is_terminal"]:
                    self._log_series_progress(series_id, reply, image_count, batch=is_batch)
                    return reply
                if (
                    last_log_time is None
                    or state != last_state
                    or now - last_log_time >= MOTION_BLUR_PROGRESS_LOG_S
                ):
                    self._log_series_progress(series_id, reply, image_count, batch=is_batch)
                    last_log_time = now
                last_state = state

            if cancel_deadline is not None and now >= cancel_deadline:
                raise RuntimeError(
                    f"Motion-blur {noun} {series_id} cancelled by stop request, but the camera "
                    f"did not report a terminal state within {MOTION_BLUR_CANCEL_WAIT_S:.0f} s; "
                    f"last known state={last_state}, "
                    f"{self._series_counts_text(last_status, image_count)}"
                )
            if now - last_progress_time >= MOTION_BLUR_STALL_TIMEOUT_S:
                raise RuntimeError(
                    f"Motion-blur {noun} {series_id} made no progress for "
                    f"{MOTION_BLUR_STALL_TIMEOUT_S:.0f} s: state={last_state}, "
                    f"{self._series_counts_text(last_status, image_count)}"
                )
            time.sleep(MOTION_BLUR_POLL_INTERVAL_S)

    def handle_failed_qc(self):
        self.logger(f"Zstack failed QC at path: {self.zstack_folder_path}")

        new_folder_path = self.filename.failed_qc_path_generator(self.focus_view, self.obj)
        self.logger(f"Creating new folder path to store failed zstack")
        self.logger(f"Changing the folder name to: {os.path.basename(new_folder_path)} ")
        os.rename(self.zstack_folder_path, new_folder_path)

        # Update the zStack json in the newly created failed QC folder 
        # Rename the zstack json file to match the new folder name
        original_zstack_json_name = f"{os.path.basename(self.zstack_folder_path)}.json"
        original_zstack_json_path = os.path.join(new_folder_path, original_zstack_json_name)
        new_zstack_json_name = f"{os.path.basename(new_folder_path)}.json"
        new_zstack_json_path = os.path.join(new_folder_path, new_zstack_json_name)

        os.rename(original_zstack_json_path, new_zstack_json_path)

        # Update the zstack_name, zstack_json_path, and zstack_path in the zstack JSON file
        new_zstack_name = f"{os.path.basename(new_folder_path)}"
        update_failed_qc_zstack_json(new_zstack_name, new_zstack_json_path, new_folder_path)

        # Add the failed QC zstack metadata to the manifest json file
        manifest_json_path = os.path.join(c.PI_IMAGE_DIR, self.filename.slide_case_folder, "manifest.json")
        self.logger(f"Appending zstack metadata from {new_zstack_json_path} to manifest.json")
        append_zstack_metadata_to_manifest(manifest_json_path, new_zstack_json_path)

        # Recreate the original folder path for the next zstack collection to be saved in the correct location
        self.logger(f"Recreating the original folder path: {self.zstack_folder_path}")
        Path(self.zstack_folder_path).mkdir(parents=True, exist_ok=True)

        """Queue the failed zstack folder for transfer to Mac."""
        self.logger(f"Queuing {os.path.basename(new_folder_path)} for upload to Mac")
        enqueue_folder(new_folder_path, self.filename.barcode, self.filename.date)

    def qc_check_focus(self):
        self.logger(f"Running QC check on path: {self.zstack_folder_path}")

        background_filename, background_file_path = self.filename.background_filename_generator(self.obj)
        darkfield_filename, darkfield_file_path = self.filename.darkfield_filename_generator(self.obj)

        dark_path = f"{darkfield_file_path}/{darkfield_filename}.tif"
        back_path = f"{background_file_path}/{background_filename}.tif"

        result = a.check_focus(self.zstack_folder_path, self.current_x, self.current_y, dark_path, back_path)

        # Paths to the different json files
        manifest_json_path = os.path.join(c.PI_IMAGE_DIR, self.filename.slide_case_folder, "manifest.json")
        zstack_json_name = f"{os.path.basename(self.zstack_folder_path)}.json"
        zstack_json_path = os.path.join(self.zstack_folder_path, zstack_json_name)

        # Add the QC result to the zstack JSON file. Passed or Failed
        if result:
            update_qc_result = "Passed"
            update_zstack_qc_result(zstack_json_path, update_qc_result)

            # If the QC check passes, append the zstack metadata to the manifest.json file
            self.logger(f"Appending zstack metadata from {zstack_json_path} to manifest.json")
            append_zstack_metadata_to_manifest(manifest_json_path, zstack_json_path)
        else:
            # QC result failed, update the zstack JSON file with the failed QC result
            update_qc_result = "Failed"
            update_zstack_qc_result(zstack_json_path, update_qc_result)

        return result

    def get_smear_center(self, smear_id):
        y_center = c.SLIDE_1_CENTER_Y
        if smear_id == "SM1":
            x_center = c.SLIDE_1_SM1_CENTER_X
        elif smear_id == "SM2":
            x_center = c.SLIDE_1_SM2_CENTER_X
        elif smear_id == "SM3":
            x_center = c.SLIDE_1_SM3_CENTER_X
        else:
            self.logger(f"{smear_id} is not a valid smear ID")
            return None

        return x_center, y_center

    def generate_spiral(self, start_x, start_y, num_points=20, spacing=1.0):
        points = [[float(start_x), float(start_y)]]
        if num_points <= 1:
            return points

        curr_x, curr_y = float(start_x), float(start_y)

        # Directions: Down, Right, Up, Left
        # Based on your example: (100, 10) -> (100, 9.5) is Down (y - spacing)
        directions = [(0, -1), (1, 0), (0, 1), (-1, 0)]

        step_size = 1
        dir_idx = 0

        while len(points) < num_points:
            # We move in the current direction 'step_size' times
            # But we must check the point count after every single step
            for _ in range(2): # In a square spiral, you increase step size every 2 directions
                for _ in range(step_size):
                    if len(points) >= num_points:
                        return points

                    dx, dy = directions[dir_idx]
                    curr_x += dx * spacing
                    curr_y += dy * spacing
                    points.append([round(curr_x, 4), round(curr_y, 4)])

                # Change direction
                dir_idx = (dir_idx + 1) % 4

            step_size += 1
        return points

    def collect_data_with_10x(self):
        self.move_carousel("1")
        self.zstack_folder_path = self.filename.data_path_generator(self.focus_view, self.obj)
        create_zstack_json(self.zstack_folder_path, self.current_x, self.current_y, self.focus_view, self.obj, self.filename.smear_id)

        z_focus_10x, _, _ = self.scan_z_axis_for_focus(True)
        self.filename.image_cleanup(self.focus_view, self.obj, z_focus_10x, self.current_x, self.current_y, 1, 1)

        # Populate zstack JSON with surviving image JSONs (post-cleanup), sorted by z
        self.logger(f"Populating zstack JSON from folder {os.path.basename(self.zstack_folder_path)}...")
        populate_zstack_json_from_folder(self.zstack_folder_path)

        self.check_stop()

    def collect_data_with_20x_40x(self, obj = int):
        self.move_carousel(f"{obj}")
        self.zstack_folder_path = self.filename.data_path_generator(self.focus_view, self.obj)
        create_zstack_json(self.zstack_folder_path, self.current_x, self.current_y, self.focus_view, self.obj, self.filename.smear_id)
        
        z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
        self.complete_zstack(focus_scores)
        self.check_stop()

    def test_carousel(self):
        self.home_carousel()
        self.move_carousel("2")
        time.sleep(5)
        self.move_carousel("3")
        time.sleep(5)
        self.move_carousel("1")

    def registration_test(self):
        self.logger("registration_test: Starting 40x zstack at current position")
        #self.start_imaging()

        self.set_smear_id("SM1")

        if self.current_x is None:
            self.current_x = 141.5
        if self.current_y is None:
            self.current_y = 18.0

        self.home_carousel()
        self.move_carousel("3")

        self.logger("registration_test: Running coarse z-scan to locate focus plane")
        coarse_z_focus, coarse_max_score, coarse_scores = self.focus_scan(10, 600, 20)
        _, max_score_coarse = max(coarse_scores, key=lambda x: x[1])
        _, min_score_coarse = min(coarse_scores, key=lambda x: x[1])
        if (max_score_coarse - min_score_coarse) > 5.0:
            self.focus_preset_40x = coarse_z_focus - 60
        else:
            self.focus_preset_40x = coarse_z_focus - 40
        self.logger(f"registration_test: Coarse focus at z={coarse_z_focus} µm. Setting 40x preset to {self.focus_preset_40x}")

        for stack_index in range(1, 3):
            self.focus_view = stack_index
            self.logger(f"registration_test: Starting zstack {stack_index}/2 with focus_view={self.focus_view}")

            z_focus, max_score, focus_scores = self.scan_z_axis_for_focus(take_image=True)
            self.logger(f"registration_test: Focus found at z={z_focus} µm, score={max_score:.2f}")

            self.complete_zstack(focus_scores)
            self.logger(f"registration_test: Completed zstack {stack_index}/2")

        #self.stop_imaging()
        self.logger("registration_test: Complete")


if __name__ == "__main__":
    pass
    file = FileTransfer5()
    file.set_barcode("E.COLI-2")
    motor = Motor(filename = file)
    #motor.collect_data_manual_fov(126, 15, [20, 40], "SM1", 1, transfer=False)
    #motor.collect_exposure_series_manual_fov(131, 13, 300, 40, "SM2", 1)
    motor.collect_motion_blur_data(
        x_pos=None,
        y_pos=None,
        z_pos=None,
        magnification=20,
        smear_id="SM1",
        fov_number=2,
        nframes=1,
        image_count=30,
        exposure_time=None,
        queue_capacity=50,
        width=None,
        series_count=1,
        inter_series_pause_s=0.0,
    )

    # --- Exposure Time Pre-set Test ---
    #motor.home_carousel()

    # 10x Pre-set
    #motor.move_carousel("1")

    # 20x Pre-set
    #motor.move_carousel("2")

    # 40x Pre-set
    #motor.move_carousel("3")

    #z, _, _ = motor.scan_z_axis_for_focus()
    #motor.move_z_axis(z)

    # --- Test Objective Carousel ---
    #motor.home_carousel()
    #motor.move_carousel("2")
    #time.sleep(5)
    #motor.move_carousel("3")
    #time.sleep(5)
    #motor.move_carousel("1")
    #time.sleep(5)
    #motor.move_carousel("3")
    #time.sleep(5)
    #motor.move_carousel("2")

    # --- Basic Motor Control Test ---
    #motor.home_axis("X, Y")
    #motor.move_x_axis(c.SLIDE_1_SM3_CENTER_X)
    #motor.move_y_axis(c.SLIDE_1_CENTER_Y)
    #motor.move_x_axis(137)
    #motor.move_y_axis(13.5)
    #motor.move_z_axis(200)
    #motor.move_carousel("3")
