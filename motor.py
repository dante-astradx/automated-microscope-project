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
import analysis as a
from datetime import datetime
from datetime import date
from microscope_log import log_output, log_to_file_only, update_status

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

        # Image directory path
        self.pi_image_dir = c.PI_IMAGE_DIR

        # Current x - y position
        self.current_x = None
        self.current_y = None

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

    def stop(self):
        # Signal the motor to stop gracefully
        self.stop_requested = True
        self.logger("Motor stop requested.")

    def check_stop(self):
        self.logger(f"Checking stop flag. Flag is {self.stop_requested}")
        # Helper to check stop flag and raise if needed
        if self.stop_requested:
            raise RuntimeError("Motor stop requested.")

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
        image_filename = self.filename.data_filename_generator(self.focus_view, self.obj, self.current_x, self.current_y, z)
        self.logger(f"Taking image! Filename: {image_filename}")
        self.imager.take_rpi_image(10, image_filename)
        time.sleep(10)

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
        threshold = 100
        coarse_z_focus, coarse_max_score, _ = self.focus_scan(180, 400, 20)
        self.logger(f"Max coarse score: {coarse_max_score:.2f} at z = {coarse_z_focus} µm")

        fine_counter = 0
        while True:
            fine_counter += 1
            fine_start = coarse_z_focus - 10
            fine_end = coarse_z_focus + 10
            fine_z_focus, fine_max_score, _ = self.focus_scan(fine_start, fine_end, 2)

            if fine_max_score > (coarse_max_score - threshold):
                self.logger(f"Max fine score: {fine_max_score:.2f} at z = {fine_z_focus} µm")
                break

            if fine_counter == 3:
                break

            self.logger(f"Max fine score ({fine_max_score}) is less than max coarse score ({coarse_max_score})")
            self.logger("Repeating fine focus scan")

        super_fine_counter = 0
        while True:
            super_fine_counter += 1

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

            if super_fine_max_score > (fine_max_score - threshold):
                self.logger(f"Final max score: {super_fine_max_score:.2f} at z = {super_fine_z_focus} µm")
                break

            if super_fine_counter == 3:
                break

            self.logger(f"Max super fine score ({super_fine_max_score}) is less than max fine score ({fine_max_score})")
            self.logger("Repeating super fine focus scan")

        return super_fine_z_focus, super_fine_max_score, final_focus_scores

    def scan_z_axis_for_grant_video(self):
        z_focus, scores, _ = self.focus_scan(150, 300, 10)

    def grant_video_procedure(self):
        time.sleep(10)
        motor.home_axis("X, Y")

        motor.move_x_axis(120)
        motor.move_y_axis(10)
        motor.move_carousel("1")
        time.sleep(1)
        motor.scan_z_axis_for_grant_video()

        motor.move_x_axis(145)
        motor.move_y_axis(15)
        motor.move_carousel("2")
        time.sleep(1)
        motor.scan_z_axis_for_grant_video()

        motor.move_x_axis(120)
        motor.move_y_axis(11)
        motor.move_carousel("3")
        time.sleep(1)
        motor.scan_z_axis_for_grant_video()

    def find_average_focus_level(self):
        self.home_axis("X, Y")
        z_focus_list = []
        focus_score_list = []
        for i in range(6):
            self.go_to_random_position()
            z_focus, focus_score, _  = self.scan_z_axis_for_focus()
            z_focus_list.append(z_focus)
            focus_score_list.append(focus_score)
        # outlier removal using IQR method
        scores_np = np.array(focus_score_list)
        z_values_np = np.array(z_focus_list)

        # Calculate Q1, Q3, and IQR for focus scores
        Q1 = np.percentile(scores_np, 25)
        Q3 = np.percentile(scores_np, 75)
        IQR = Q3 - Q1

        # Define outlier bounds
        lower_bound = Q1 - 1.5 * IQR
        upper_bound = Q3 + 1.5 * IQR

        # Create a boolean mask to identify non-outliers
        non_outlier_mask = (scores_np >= lower_bound) & (scores_np <= upper_bound)

        # Filter out outliers from both scores and Z-values
        filtered_scores = scores_np[non_outlier_mask]
        filtered_z_values = z_values_np[non_outlier_mask]

        self.logger(f"--- Outlier Removal Results ---")
        self.logger(f"  Original focus scores: {focus_score_list}")
        self.logger(f"  Original focus levels: {z_focus_list}")
        self.logger(f"  Q1: {Q1:.2f}, Q3: {Q3:.2f}, IQR: {IQR:.2f}")
        self.logger(f"  Outlier bounds: [{lower_bound:.2f}, {upper_bound:.2f}]")
        self.logger(f"  Filtered focus scores (non-outliers): {filtered_scores}")
        self.logger(f"  Filtered Z-values (non-outliers): {filtered_z_values}")

        if len(filtered_z_values) == 0:
            self.logger("WARNING: All focus levels were identified as outliers. Cannot calculate robust average.")
            return None

        # Calculate the robust average Z-focus from the filtered data
        robust_average_z = np.mean(filtered_z_values)
        self.logger(f"--- Robust Average Optimal Z-Focus Level (after outlier removal): {robust_average_z:.2f}um  ---")

        return round(robust_average_z, 2)

    def go_to_random_position(self):
        random_x_pos = random.randint(self.start_x, self.end_x)
        random_y_pos = random.randint(self.start_y, self.end_y)

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
            background_filename = self.filename.background_filename_generator(self.obj)
            self.logger(f"Taking background image for {self.obj}x objective. Filename: {background_filename}")
            self.imager.take_rpi_image(10, background_filename)
            time.sleep(10)
            self.logger(f"Background image taken for {self.obj}x objective")

        self.logger("Background images taken for all objectives")

    def take_darkfield_image(self):
        for i in range(3):
            self.check_stop()

            self.move_carousel(f"{i+1}")
            dark_filename = self.filename.darkfield_filename_generator(self.obj)
            self.logger(f"Taking darkfield image for {self.obj}x objective. Filename: {dark_filename}")
            self.imager.take_rpi_image(10, dark_filename)
            time.sleep(10)
            self.logger(f"Darkfield image taken for {self.obj}x objective")

        self.logger("Darkfield images taken for all objectives")

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
                self.move_z_axis(next_z)
                time.sleep(0.5)
                self.logger(f"Taking zstack image at: z = {next_z} µm")
                self.capture_image(next_z)
            self.filename.image_cleanup(self.focus_view, self.obj, z_focus, self.current_x, self.current_y, points_before, points_after)

        else:
            self.logger("No additional scanning required — max focus score is centered.")

    def collect_data(self, fov):
        self.move_carousel("1")
        avg_z_level = self.find_average_focus_level()
        self.move_z_axis(avg_z_level)
        bacteria_locations = []

        self.logger("Background and darkfield filenames used for bacteria detection:")
        background_filename = self.filename.background_filename_generator(self.obj)
        self.logger(background_filename)
        darkfield_filename = self.filename.darkfield_filename_generator(self.obj)
        self.logger(darkfield_filename)

        counter = 0
        while True:
            self.home_axis("X, Y")
            self.current_x, self.current_y = self.go_to_random_position()

            scanning_filename = self.filename.scanning_filename_generator(self.current_x, self.current_y, avg_z_level)
            self.logger(f"Taking image for bacteria detection. Filename: {scanning_filename}")
            self.imager.take_rpi_image(self.z_focus_nframes, scanning_filename)
            time.sleep(10)

            is_there_bacteria = False
            impath = f"/home/microscope_auto/Images/{scanning_filename}.tif"
            background_path = f"/home/microscope_auto/Images/{background_filename}.tif"
            dark_path = f"/home/microscope_auto/Images/{darkfield_filename}.tif"

            if counter > 12 and len(bacteria_locations) < 2:
                is_there_bacteria, intensity_range, _ = a.cell_counter_alt(impath, dark_path, background_path, 500.0)
            else:
                is_there_bacteria, intensity_range, _ = a.cell_counter_alt(impath, dark_path, background_path, 700.0)

            self.logger(f"Intensity range: {intensity_range}")

            if is_there_bacteria:
                bacteria_locations.append([self.current_x, self.current_y])

            if len(bacteria_locations) == fov:
                break

            counter += 1

        self.logger(f"Scanning complete. {fov} location/s identified with bacteria")
        self.logger(bacteria_locations)

        self.focus_view = 0
        for i in range(len(bacteria_locations)):
            self.home_axis("X, Y")
            self.current_x = bacteria_locations[i][0]
            self.current_y = bacteria_locations[i][1]
            self.move_x_axis(self.current_x)
            self.move_y_axis(self.current_y)
            self.focus_view += 1

            # 10x imaging at x,y
            self.move_carousel("1")
            z_focus_10x, _, _ = self.scan_z_axis_for_focus()
            self.move_z_axis(z_focus_10x)
            self.capture_image(z_focus_10x)

# --- 3rd Version: Z-stack at 20x twice, only
            # 20x imaging at x,y
            #self.move_carousel("2")
            #z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
            #self.complete_zstack(focus_scores)

            # 20x imaging at x+0.25, y+0.25
            #self.current_x += 0.25
            #self.current_y += 0.25
            #self.move_x_axis(self.current_x)
            #self.move_y_axis(self.current_y)

            #z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
            #self.complete_zstack(focus_scores)

# --- 2nd Version: Z-stack at 20x twice, 40x four times
            # 20x, 40x imaging at x,y
            for j in range(2, 4):
                self.move_carousel(f"{j}")
                z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
                self.complete_zstack(focus_scores)

            # 20x, 40x imaging at x+0.25, y+0.25
            self.current_x += 0.25
            self.current_y += 0.25
            self.move_x_axis(self.current_x)
            self.move_y_axis(self.current_y)

            for j in range(2, 4):
                self.move_carousel(f"{j}")
                z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
                self.complete_zstack(focus_scores)

            # 40x imaging at x+0.25, y-0.25
            #self.current_y -= 0.5
            #self.move_y_axis(self.current_y)
            #z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
            #self.complete_zstack(focus_scores)

            # 40x imaging at x-0.25, y-0.25
            #self.current_x -= 0.5
            #self.move_x_axis(self.current_x)
            #z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
            #self.complete_zstack(focus_scores)

# ---- 1st Version: Z-stack at every objective ----
        #for i in range(len(bacteria_locations)):
            #self.home_axis("X, Y")
            #self.current_x = bacteria_locations[i][0]
            #self.current_y = bacteria_locations[i][1]
            #self.move_x_axis(self.current_x)
            #self.move_y_axis(self.current_y)
            #self.focus_view += 1

            #for j in range(3):
                #self.move_carousel(f"{j + 1}")
                #z_focus, _ , focus_scores = self.scan_z_axis_for_focus(True)

                #self.complete_zstack(focus_scores)

        self.logger("Data collection finished. All images have been taken and saved to Images folder")

    def search_for_bacteria(self, fov):
        self.move_carousel("1")
        scanned_locations = []
        bacteria_locations = []

        background_filename = self.filename.background_filename_generator(self.obj)
        darkfield_filename = self.filename.darkfield_filename_generator(self.obj)
        #background_filename =  "10x_background_20250829_M1"
        #darkfield_filename = "10x_darkfield_20250829_M1"

        dark_path = f"/home/microscope_auto/Images/{darkfield_filename}.tif"
        back_path = f"/home/microscope_auto/Images/{background_filename}.tif"

        counter = 0

        while True:
            self.check_stop()
            counter += 1

            self.home_axis("X, Y")
            self.current_x, self.current_y = self.go_to_random_position()
            self.logger(f"moving to {self.current_x}, {self.current_y}")

            coarse_z_focus, coarse_max_score, coarse_focus_scores = self.focus_scan(160, 400, 20)
            self.check_stop()

            fine_z_focus, fine_max_score, fine_focus_scores = self.focus_scan(coarse_z_focus-6, coarse_z_focus+6, 1)
            self.move_z_axis(fine_z_focus)
            self.check_stop()

            scanning_filename = self.filename.scanning_filename_generator(self.current_x, self.current_y, fine_z_focus)
            self.imager.take_rpi_image(10, scanning_filename)
            time.sleep(10)

            is_there_bacteria = False
            im_path = f"/home/microscope_auto/Images/{scanning_filename}.tif"
            is_there_bacteria, intensity_range, cell_radius = a.cell_counter_alt(im_path, dark_path, back_path, 600.0)

            scanned_locations.append((intensity_range, cell_radius, self.current_x, self.current_y))
            self.logger(f"Intensity range: {intensity_range}, Cell Radius: {cell_radius}")

            if is_there_bacteria:
                bacteria_locations.append([self.current_x, self.current_y])
                self.logger("BACTERIA IDENTIFIED")

            if len(bacteria_locations) == fov:
                break

            if counter == 10:
                intensity_range, cell_radius, self.current_x, self.current_y =  max((t for t in scanned_locations if t[0] < 600.0),key=lambda x: x[0])
                print(scanned_locations)
                self.logger(f"Stopping search. Highest recorded IR: {intensity_range} at {self.current_x}, {self.current_y}")
                bacteria_locations.append([self.current_x, self.current_y])
                self.move_x_axis(self.current_x)
                self.move_y_axis(self.current_y)
                break

        return bacteria_locations

    def collect_20x_data(self, fov):
        bacteria_locations = self.search_for_bacteria(fov)

        self.check_stop()
        self.focus_view = 0
        for i in range(len(bacteria_locations)):
            self.check_stop()

            self.home_axis("X, Y")
            self.current_x = bacteria_locations[i][0]
            self.current_y = bacteria_locations[i][1]
            self.move_x_axis(self.current_x)
            self.move_y_axis(self.current_y)
            self.focus_view += 1

            # 10x imaging at x,y
            self.move_carousel("1")
            z_focus_10x, _, _ = self.scan_z_axis_for_focus()
            self.move_z_axis(z_focus_10x)
            self.capture_image(z_focus_10x)
            self.check_stop()

            # 20x imaging at x,y
            self.move_carousel("2")
            z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
            self.complete_zstack(focus_scores)
            self.check_stop()

            # 20x imaging at x+0.25, y+0.25
            self.current_x += 0.25
            self.current_y += 0.25
            self.move_x_axis(self.current_x)
            self.move_y_axis(self.current_y)

            z_focus, _, focus_scores = self.scan_z_axis_for_focus(True)
            self.complete_zstack(focus_scores)

        self.logger("Data collection finished. All images have been taken and saved to Images folder")

if __name__ == "__main__":
    pass
    #file = FileTransfer()
    #file.set_filename("AR0249_20250724_NA_0.0_F1_S1_M1")
    #motor = Motor(filename = file)
    #imager = Camera()

    #motor.search_for_bacteria(1)
    #motor.collect_20x_data()
    #pattern = "AR0249_*.tif"
    #file.upload_to_dante_laptop(pattern)

    #motor.home_carousel()
    #motor.move_carousel("1")
    #print("taking background image")
    #back_filename = "AR0222_10x_background"
    #imager.take_rpi_image(10, back_filename)
    #time.sleep(10)

    #input("Turn off light and take darkfield image")
    #dark_filename = "AR0222_10x_darkfield"
    #imager.take_rpi_image(10, dark_filename)
    #time.sleep(10)
    #input("Turn on light and put in slide")

    #list = [[110, 10], [120, 10], [130, 20]]
    #for i in range(len(list)):
        #motor.home_axis("X, Y")
        #motor.move_x_axis(list[i][0])
        #motor.move_y_axis(list[i][1])

        #super_fine_z_focus, super_fine_max_score, final_focus_scores = motor.scan_z_axis_for_focus()
        #motor.move_z_axis(super_fine_z_focus)
        #filename = f"AR0222_IR_test_{list[i][0]}x_{list[i][1]}y_{super_fine_z_focus}z"
        #imager.take_rpi_image(10, filename)
        #time.sleep(10)

        #impath = f"/home/microscope_auto/Images/{filename}.tif"
        #background_path = f"/home/microscope_auto/Images/{back_filename}.tif"
        #dark_path = f"/home/microscope_auto/Images/{dark_filename}.tif"

        #is_there_bacteria, intensity_range = a.cell_counter_alt(impath, dark_path, background_path, 500.0)

    #motor.grant_video_procedure()
