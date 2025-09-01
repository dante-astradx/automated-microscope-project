import requests
import json
import time

# --- Configuration ---
# IP address or hostname of your Raspberry Pi running Moonraker
MOONRAKER_IP = "10.116.9.228"
# Default Moonraker port (usually 7125)
MOONRAKER_PORT = 7125
# The axis you want to control (e.g., 'X', 'Y', 'Z')
TARGET_AXIS_Z = "Z"

# From your printer.cfg: rotation_distance for stepper_z is 20
# This means 20mm of movement equals one full rotation of the stepper motor.
# Adjust this value if you change the TARGET_AXIS or your printer.cfg
ROTATION_DISTANCE_MM_Z = 20 

# Speed for the movement (mm/s). Be cautious with high speeds.
# This value will be used with G0/G1 commands.
MOVEMENT_SPEED_MM_S = 50

# Fixed interval at which we want to move z-axis at
# Interval is in Microns
# Can be user input if we want in the future
z_axis_interval = 10
#z_axis_interval = int(input("Enter interval (in microns) at which to move the Z-axis at:"))

# --- Moonraker API Endpoint ---
# The endpoint for sending G-code commands, now using the direct IP address
GCODE_API_URL = f"http://{MOONRAKER_IP}:{MOONRAKER_PORT}/printer/gcode/script"

# --- Function to send G-code commands ---
def send_gcode_command(gcode_command: str):
    """
    Sends a G-code command to the Klipper printer via Moonraker.
    """
    print(f"Sending G-code: {gcode_command}")
    payload = {"script": gcode_command}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(GCODE_API_URL, data=json.dumps(payload), headers=headers)
        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
        print(f"Response: {response.json()}")
        return response.json()
    except requests.exceptions.ConnectionError:
        print(f"Error: Could not connect to Moonraker at {GCODE_API_URL}.")
        print("Please ensure Klipper and Moonraker are running and the IP address/port are correct.")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        print(f"Response content: {response.text}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None

# --- Math to convert rotation of Z axis motor to rotation distance (mm) ---
# Motor pulley teeth count
Nm = 60
# Z-axis dial pulley teeth count
Nz = 60

# One full rotation (360 deg) of Z-axis dial results in 200 microns of linear translation (on microscope)
# Dial rotation in degrees to acheive one micron of z-axis translation
dial_rot_z = 360/200

# Motor rotation in degrees to achieve a z-axis translation of 1um
motor_rot_z = (Nz / Nm) * dial_rot_z

# As defined by ROTATION_DISTANCE_MM, one full rotation of the motor equals 20mm of movement for stepper_z
# Movement in mm of 1 degree rotation
one_deg_movement_mm_z = ROTATION_DISTANCE_MM_Z /360

# Calculating the distance movement in MM to achieve motor_rot_z
rot_distance_z = motor_rot_z * one_deg_movement_mm_z # mm of motor movement to achieve 1um translation

# --- Function to calculate the distance movement in MM for z-axis units ---
def get_motor_distance_z(microscope_z_units):
    """
    Calculates the motor's absolute MM position for a given microscope z-axis unit.
    Applies the offset to map microscope units to printer's (0,0) origin.
    """
    return rot_distance_z * microscope_z_units

# --- Main script logic ---
if __name__ == "__main__":
    print("--- Starting Stepper Motor Control Script ---")

    # --- Get user input for the microscope objective ---
    print("\nPlease enter which microscope objective you are using: 10x, 20x, 40x.")
    
    while True:
        try:
            obj = int(input("Enter the objective magnification as an integer (i.e 10, 20, or 40): "))
            if not (obj == 10 or obj == 20 or obj == 40):
                print("Invalid input. The objective must be 10, 20, or 40x. Remeber to input objective as an integer")
            else:
                break # Valid input, exit loop
        except ValueError:
            print("Invalid input. Please enter integers only.")

    # Home Z axis using G28.
    print(f"\nHoming Z axis (G28)...")
    send_gcode_command("G28 Z")
    send_gcode_command("M400")
    time.sleep(5)

    # Set coordinate system to absolute positioning
    send_gcode_command("G90")
    time.sleep(0.1)

    print(f"n\Homing succesfull and absolute positioning set")

    # --- Logic to move z-axis to pre-set focus location ----
    # Based on the objective, the z-axis moves to a pre-set location and begins to focus the camera
    # From pre-set location z-axis will move up/down in increments of 10um
    # Pre-set for each objective was determined by manually finding the approx. distance from home to focus on the 10um slide.

    # movement increment
    inc = 10

    # number of locations above/below preset that images will be taken to determine focus point
    num = 5

    if obj == 40:
        # 40x objective is near focus at 520um
        preset_z = 520
        target_z_um = get_motor_distance_z(preset_z)
        move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_um:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
        send_gcode_command(move_z_command)
        send_gcode_command("M400")
        time.sleep(2)

        # Calculate the top and bottom positions
        top_z_um = preset_z - (num * inc)
        bottom_z_um = preset_z + (num * inc)

        # Move to the lowest location where image will be taken to determine focus point
        #target_z_bottom = get_motor_distance_z(bottom_z_um)
        #print(f"\n Moving Z position to {bottom_z_um:.3f}um")
        #move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_bottom:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
        #send_gcode_command(move_z_command)
        #send_gcode_command("M400")
        #time.sleep(0.5)

        # Loop to move Z-axis upwards through all positions
        for current_z in range(bottom_z_um, (top_z_um - inc), -inc):
            target_z_um = get_motor_distance_z(current_z)
            print(f"\n Moving Z position to {current_z:.3f}um")
            move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_um:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
            send_gcode_command(move_z_command)
            send_gcode_command("M400")
            time.sleep(0.5)

    elif obj == 20:
        # 20x objective is near focus at 570um
        preset_z = 570
        target_z_um = get_motor_distance_z(preset_z)
        move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_um:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
        send_gcode_command(move_z_command)
        send_gcode_command("M400")
        time.sleep(2)

        # Calculate the top and bottom positions
        top_z_um = preset_z - (num * inc)
        bottom_z_um = preset_z + (num * inc)

        # Loop to move Z-axis upwards through all positions
        for current_z in range(bottom_z_um, (top_z_um - inc), -inc):
            target_z_um = get_motor_distance_z(current_z)
            print(f"\n Moving Z position to {current_z:.3f}um")
            move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_um:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
            send_gcode_command(move_z_command)
            send_gcode_command("M400")
            time.sleep(0.5)

    elif obj == 10:
        # 10x objective is near focus at 520um
        preset_z = 570
        target_z_um = get_motor_distance_z(preset_z)
        move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_um:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
        send_gcode_command(move_z_command)
        send_gcode_command("M400")
        time.sleep(2)

        # Calculate the top and bottom positions
        top_z_um = preset_z - (num * inc)
        bottom_z_um = preset_z + (num * inc)

        # Loop to move Z-axis upwards through all positions
        for current_z in range(bottom_z_um, (top_z_um - inc), -inc):
            target_z_um = get_motor_distance_z(current_z)
            print(f"\n Moving Z position to {current_z:.3f}um")
            move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_um:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
            send_gcode_command(move_z_command)
            send_gcode_command("M400")
            time.sleep(0.5)



    # Move z-axis forward by a set amount
    #target_z_mm = get_motor_distance_z(520)
    #move_z_command = f"G0 {TARGET_AXIS_Z}{target_z_mm:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
    #send_gcode_command(move_z_command)
    #send_gcode_command("M400") # Wait for movement to stop
    #time.sleep(0.5)

    # Final return to absolute positioning (already set, but good to confirm)
    send_gcode_command("G90")
    time.sleep(0.1)

    print("\n--- Script Finished ---")
    print("Physical movement should be complete and printer returned to home.")
   
