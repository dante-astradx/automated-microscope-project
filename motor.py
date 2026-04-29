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
from json_handler import read_json
import light_controller as lc
import time
import math
from pathlib import Path
from transfer_manager import enqueue_folder, mark_slide_done

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
        enqueue_folder(folder_path, self.filename.barcode)

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

    # Function to calculate Y-axis motor position for a give location in MM from home
    #def calculate_motor_position_y(self, microscope_y_units):
        #printer_relative_y_units = microscope_y_units - self.y_offset
        #return printer_relative_y_units * self.rot_distance_y

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
    #def move_y_axis(self, y_pos):
        #self.logger(f"Moving y-axis to position {y_pos}mm")
        #target_y_mm = self.calculate_motor_position_y(y_pos)
        #self.move_command(self.target_axis_y, target_y_mm)
        #self.current_y = y_pos

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
        self.imager.take_rpi_image(100, image_filename, file_path)
        time.sleep(15)
        self.imager.update_latest_image_to_jpg(os.path.join(file_path, f"{image_filename}.tif"))

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

    def dynamic_focus(self, ref_z_focus):
        # Move to reference focus level
        self.move_z_axis(ref_z_focus)
        time.sleep(1)
        ref_score = float(self.imager.get_focus_score())
        self.logger(f"Focus score at {ref_z_focus}z: {ref_score}")

        # Test moving upward first
        self.move_z_axis(ref_z_focus + 2)
        time.sleep(1)
        up_score = float(self.imager.get_focus_score())
        self.logger(f"Focus score at {ref_z_focus + 1}: {up_score}")

        # Determine direction based on whether score improved
        if up_score > ref_score:
            direction = 1  # move up
            current_score = up_score
            self.logger(f"Moving up to find focus level: {up_score} > {ref_score}")
        else:
            # Try moving down instead
            self.logger(f"Moving down to find focus level: {up_score} < {ref_score}")
            self.move_z_axis(ref_z_focus - 2)
            time.sleep(1)
            down_score = float(self.imager.get_focus_score())
            self.logger(f"Focus score at {ref_z_focus - 3}: {down_score}")
            if down_score > ref_score:
                direction = -1  # move down
                current_score = down_score
                self.logger(f"Moving down to find focus level: {down_score} > {ref_score}")
            else:
                # Neither direction improves → already at best focus
                self.logger(f"Already at focus level. Moving back to {ref_z_focus}")
                self.move_z_axis(ref_z_focus)
                return ref_z_focus

        # Keep scanning in the chosen direction until the focus drops
        prev_z = self.current_z
        while True:
            next_z = self.current_z + direction
            self.move_z_axis(next_z)
            time.sleep(1)
            new_score = float(self.imager.get_focus_score())
            self.logger(f"Focus score at {next_z}: {new_score}")

            if new_score > current_score:
                # still improving → continue
                self.logger(f"{new_score} > {current_score}: Continue moving")
                current_score = new_score
                prev_z = next_z
            else:
                # focus dropped → move back one step and stop
                self.logger(f"{new_score} < {current_score}: Score has dropped. Move back to {prev_z}")
                self.move_z_axis(prev_z)
                return prev_z

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
            self.imager.take_rpi_image(100, background_filename, file_path)
            time.sleep(15)
            self.logger(f"Background image taken for {self.obj}x objective")

        self.logger("Background images taken for all objectives")

    def take_darkfield_image(self):
        for i in range(3):
            self.check_stop()

            self.move_carousel(f"{i+1}")
            dark_filename, file_path = self.filename.darkfield_filename_generator(self.obj)
            self.logger(f"Taking darkfield image for {self.obj}x objective. Filename: {dark_filename}")
            self.imager.take_rpi_image(100, dark_filename, file_path)
            time.sleep(15)
            self.logger(f"Darkfield image taken for {self.obj}x objective")

        self.logger("Darkfield images taken for all objectives")

    def take_dark_background_image(self):
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

    def first_scan_for_focus_preset(self, smear_list):
        self.home_axis("X, Y")

        current_center_y = c.SLIDE_1_CENTER_Y
        self.move_y_axis(current_center_y)
        #self.move_y_axis(14)

        self.move_carousel("1")

        for smear_id in smear_list:
            if smear_id == "SM1":
                self.move_x_axis(145)
            elif smear_id == "SM2":
                self.move_x_axis(128)
            elif smear_id == "SM3":
                self.move_x_axis(112)
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

    def wbc_imaging_xy(self, smear_list, xy_coords):
        self.start_imaging()
        smear_id = smear_list[0]
        self.set_smear_id(smear_id)
        self.focus_view = 0
        self.first_scan_for_focus_preset([smear_id])

        for i in range(len(xy_coords[0])):
            x_pos = xy_coords[0][i][0]
            y_pos = xy_coords[0][i][1]

            self.logger(f"Collecting data at {x_pos}, {y_pos + self.slide_y_offset} in {smear_id}")

            self.check_stop()
            self.focus_view += 1
            update_scoreboard(fov=self.focus_view, status="imaging")

            self.home_axis("X, Y")
            self.move_x_axis(x_pos)
            self.move_y_axis(y_pos)
            self.check_stop()

            # 10x imaging at x,y
            self.collect_data_with_10x()
            self.initiate_transfer_queue(self.focus_view, self.obj)

        self.logger("Data collection finished. All images have been taken and saved to Images folder")
        mark_slide_done(self.filename.barcode)
        self.stop_imaging()

    def collect_data_milestone5_xy(self, smear_list, xy_coords):
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
                stages = [(self.collect_data_with_20x_40x, "40x", 3)]
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
        mark_slide_done(self.filename.barcode)
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
                self.imager.take_rpi_image(100, scanning_filename)
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
                        stages = [(self.collect_data_with_20x_40x, "40x", 3)]
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
        mark_slide_done(self.filename.barcode)
        self.stop_imaging()

        # move x-y axis to position that allows for easier removal of slides only if 2nd slide was imaged
        if self.slide_y_offset == 25: # this is how we determine if 2nd slide was being imaged
            self.move_x_axis(130)
            self.move_y_axis(15)
        else:
            pass

    def handle_failed_qc(self):
        zstack_folder_path = self.filename.data_path_generator(self.focus_view, self.obj)
        self.logger(f"Zstack failed QC at path: {zstack_folder_path}")

        new_folder_path = self.filename.failed_qc_path_generator(self.focus_view, self.obj)
        self.logger(f"Creating new folder path to store failed zstack")
        self.logger(f"Changing the folder name to: {os.path.basename(new_folder_path)} ")
        os.rename(zstack_folder_path, new_folder_path)

        self.logger("Recreating the original folder path: {zstack_folder_path}")
        Path(zstack_folder_path).mkdir(parents=True, exist_ok=True)

        """Queue the failed zstack folder for transfer to Mac."""
        self.logger(f"Queuing {os.path.basename(new_folder_path)} for upload to Mac")
        enqueue_folder(new_folder_path, self.filename.barcode)

    def qc_check_focus(self):
        zstack_folder_path = self.filename.data_path_generator(self.focus_view, self.obj)
        self.logger(f"Running QC check on path: {zstack_folder_path}")

        background_filename, background_file_path = self.filename.background_filename_generator(self.obj)
        darkfield_filename, darkfield_file_path = self.filename.darkfield_filename_generator(self.obj)

        dark_path = f"{darkfield_file_path}/{darkfield_filename}.tif"
        back_path = f"{background_file_path}/{background_filename}.tif"

        result = a.check_focus(zstack_folder_path, self.current_x, self.current_y, dark_path, back_path)
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

    ### Original version where only a single 10x image is taken 
    #def collect_data_with_10x(self):
        #self.move_carousel("1")
        #z_focus_10x, _, _ = self.scan_z_axis_for_focus()
        #self.move_z_axis(z_focus_10x)
        #self.capture_image(z_focus_10x)
        #self.check_stop()

    def collect_data_with_10x(self):
        self.move_carousel("1")
        z_focus_10x, _, _ = self.scan_z_axis_for_focus(True)
        self.filename.image_cleanup(self.focus_view, self.obj, z_focus_10x, self.current_x, self.current_y, 1, 1)
        self.check_stop()

    def collect_data_with_20x_40x(self, obj = int):
        self.move_carousel(f"{obj}")
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

    def smear_analysis_test(self, smear_list):
        self.start_imaging()
        self.move_carousel("1")
        self.focus_view = 1

        start_y = 7
        end_y = 22

        background_filename, background_file_path = self.filename.background_filename_generator(self.obj)
        darkfield_filename, darkfield_file_path = self.filename.darkfield_filename_generator(self.obj)

        dark_path = f"{darkfield_file_path}/{darkfield_filename}.tif"
        back_path = f"{background_file_path}/{background_filename}.tif"

        for i in range(len(smear_list)):
            smear_id = smear_list[i]
            self.set_smear_id(smear_id)

            self.first_scan_for_focus_preset([smear_id])

            # find focus level at midpoint of smear
            ref_z_focus, ref_max_score, _ = self.scan_z_axis_for_focus()

            even_step = 2

            if smear_id == "SM1":
                start_x = 141
                end_x = 151
            elif smear_id == "SM2":
                start_x = 123
                end_x = 133
            elif smear_id == "SM3":
                start_x = 107
                end_x = 117
            else:
                self.logger("Smear ID can't be identified. ERROR")

            for current_y in range(start_y, end_y + 1, 1):
                self.move_y_axis(current_y)

                if (current_y - start_y) % 2 == 0:
                    # Even row (0-based): left -> right, step 2
                    x_sweep_range = range(start_x, end_x + 1, even_step)
                    self.logger(f"Scanning Y: {current_y}, X: {start_x} to {end_x} (step={even_step})")
                else:
                    # Odd row: start at end_x-1 if the horizontal span is an even number of mm,
                    # otherwise start at end_x. This makes the odd row hit the "in-between" positions.
                    if (end_x - start_x) % even_step == 0:
                        odd_start = end_x - 1
                    else:
                        odd_start = end_x

                    # Right -> left with step -2
                    x_sweep_range = range(odd_start, start_x - 1, -even_step)
                    self.logger(f"Scanning Y: {current_y}, X: {odd_start} down to {start_x} (step={even_step})")

                for current_x in x_sweep_range:
                    self.move_x_axis(current_x)
                    z_focus_level = self.dynamic_focus(ref_z_focus)
                    image_filename, image_path = self.capture_image(self.current_z)
                    ref_z_focus = z_focus_level

                    is_there_bacteria = False
                    im_path = f"{image_path}/{image_filename}.tif"
                    is_there_bacteria = a.is_good_for_ID(im_path, dark_path, back_path)

                    if is_there_bacteria:
                        self.logger("BACTERIA IDENTIFIED")
                    else:
                        self.logger("No bacteria found")

                    self.filename.append_csv(self.current_x, self.current_y, self.current_z, is_there_bacteria)

        self.stop_imaging()

if __name__ == "__main__":
    pass
    file = FileTransfer5()
    file.set_barcode("M5JBIP")
    motor = Motor(filename = file)

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
    motor.home_axis("X, Y")
    motor.move_x_axis(142)
    motor.move_y_axis(18)
    #motor.move_z_axis(200)
    motor.move_carousel("3")
