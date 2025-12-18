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
from microscope_log import log_output, log_to_file_only, update_status
import light_controller as lc
import time
import fnmatch

class Motor:
    def __init__(self, logger=print):
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

        self.y_min = c.Y_MIN
        self.y_max = c.Y_MAX

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

        self.trail_num = None

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
    def calculate_motor_position_y(self, microscope_y_units):
        printer_relative_y_units = microscope_y_units - self.y_offset
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

    # Function to move x-axis
    def move_x_axis(self, x_pos):
        self.logger(f"Moving x-axis to position {x_pos}mm")
        target_x_mm = self.calculate_motor_position_x(x_pos)
        self.move_command(self.target_axis_x, target_x_mm)
        self.current_x = x_pos

    # Function to move y-axis
    def move_y_axis(self, y_pos):
        self.logger(f"Moving y-axis to position {y_pos}mm")
        target_y_mm = self.calculate_motor_position_y(y_pos)
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

    def capture_image(self):
        image_filename = f"Trail{self.trail_num}_{self.current_z}z_{c.MICROSCOPE_ID}_{self.date}"
        self.logger(f"Taking image! Filename: {image_filename}")
        self.imager.take_rpi_image(100, image_filename)
        time.sleep(15)

    def first_scan_for_focus_preset(self):
        self.home_carousel()
        self.move_carousel("3")

        coarse_z_focus, coarse_max_score, focus_scores = self.focus_scan(0, 600, 20)
        _ , max_score = max(focus_scores, key=lambda x: x[1])
        _ , min_score = min(focus_scores, key=lambda x: x[1])

        self.focus_preset_40x = coarse_z_focus - 60
        self.logger(f"Max coarse score: {coarse_max_score:.2f} at z = {coarse_z_focus} µm. Setting focus presets")
        self.logger(f"40x: {self.focus_preset_40x}")

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
                self.capture_image()

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
                    self.capture_image()

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
                    self.capture_image()

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

        return super_fine_z_focus, super_fine_max_score, final_focus_scores

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
                self.capture_image()
            self.image_cleanup(z_focus, points_before, points_after)
        elif direction == "backward":
            self.move_z_axis(lowest_z)
            print(f"Moving back to start position z = {lowest_z}")
            for step in range(1, missing + 1):
                next_z = lowest_z - step
                if next_z >= 0:
                    self.move_z_axis(next_z)
                    time.sleep(0.5)
                    self.logger(f"Taking zstack image at: z = {next_z} µm")
                    self.capture_image()
            self.image_cleanup(z_focus, points_before, points_after)

        else:
            self.logger("No additional scanning required — max focus score is centered.")

    def collect_data_with_20x_40x(self, obj = int):
        self.move_carousel(f"{obj}")
        z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
        self.complete_zstack(focus_scores)

    def axial_accuracy_test(self):
        self.first_scan_for_focus_preset()
        for i in range(5):
            self.trail_num = i + 1
            self.collect_data_with_20x_40x(3)

    def image_cleanup(self, z_focus, points_before, points_after):
        self.logger("Removing extra images from zstack")
        keep_range = range(z_focus - points_before, z_focus + points_after + 1)

        folder_path = c.PI_IMAGE_DIR
        pi_files = os.listdir(folder_path)
        pattern = f"Trail{self.trail_num}_*_{c.MICROSCOPE_ID}_{self.date}.*"
        matching_files = fnmatch.filter(pi_files, pattern)
        if not matching_files:
            print("Error: no files found to delete")

        for filename in matching_files:
            parts = filename.rsplit("_")
            try:
                z_part = parts[1]
                z = int(z_part.rstrip("z"))  # strip trailing 'z'
            except (ValueError, IndexError):
                continue  # skip malformed filenames

            if z not in keep_range:
                file_path = os.path.join(folder_path, filename)
                os.remove(file_path)
                self.logger(f"Deleted: {filename}")

if __name__ == "__main__":
    motor = Motor()
    motor.axial_accuracy_test()
    #motor.image_cleanup()
