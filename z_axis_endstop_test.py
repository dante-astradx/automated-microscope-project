import requests
import json
import time

# --- Configuration ---
# IP address or hostname of your Raspberry Pi running Moonraker
MOONRAKER_IP = "10.116.9.228"
# Default Moonraker port (usually 7125)
MOONRAKER_PORT = 7125

# Axis identifiers for G-code
TARGET_AXIS_Z = "Z"

# From your printer.cfg:
# rotation_distance for stepper_z is 20mm per motor rotation
ROTATION_DISTANCE_MM_Z = 20

# Speed for the movement (mm/s). Be cautious with high speeds.
MOVEMENT_SPEED_MM_S = 50

# --- Moonraker API Endpoint ---
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
        # print(f"Response: {response.json()}") # Uncomment for more verbose output
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
# Y-axis dial pulley teeth count
Nz = 60

# One full rotation (360 deg) of Z-axis dial results in 200um of linear translation (on microscope)
# Dial rotation in degrees to acheive 1um of z-axis translation
dial_rot_z = 360/200

# Motor rotation in degrees to achieve 1um of z-axis translation
motor_rot_z = (Nz / Nm) * dial_rot_z

# As defined by ROTATION_DISTANCE_MM, one full rotation of the motor equals 20mm of movement for stepper_z
# Movement in mm of 1 degree rotation
one_deg_movement_mm_z = ROTATION_DISTANCE_MM_Z /360

# Calculating the distance movement in MM to achieve motor_rot_z
rot_distance_one_unit_z = motor_rot_z * one_deg_movement_mm_z # mm of motor movement for 1um of z-axis translation

# --- Function to calculate the distance movement in MM for y-axis units ---
def get_motor_distance_z(microscope_z_units):
    """
    Calculates the motor's absolute MM position for a given microscope y-axis unit.
    Applies the offset to map microscope units to printer's (0,0) origin.
    """
    return rot_distance_one_unit_z * microscope_z_units

# --- Main script logic ---
if __name__ == "__main__":
    print("--- Starting Stepper Motor Control Script ---")

    # Home Y axis using G28.
    print(f"\nHoming Z axis (G28)...")
    send_gcode_command("G28 Z")
    send_gcode_command("M400")
    time.sleep(5)

    # Set coordinate system to absolute positioning
    send_gcode_command("G90")
    time.sleep(0.1)

    print(f"n\Homing succesfull and absolute positioning set")

    # Move y-axis forward by a set amount
    #target_y_mm = get_motor_distance_y(20)
    #move_y_command = f"G0 {TARGET_AXIS_Y}{target_y_mm:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
    #send_gcode_command(move_y_command)
    #send_gcode_command("M400") # Wait for movement to stop
    #time.sleep(0.5)

    # --- Loop through positions ---


    # Loop over positions (from start_y to end_y inclusive)
    #for z_current_unit in range(start_z, end_z+1):
        # Calculate the absolute Z position in mm from the current Z unit
        #target_z_um = get_motor_distance_z(z_current_unit)

        #print(f"\n Moving Z position (microscope Z: {z_current_unit}) (to {target_z_um:.3f}um from printer home)")
        #move_y_command = f"G0 {TARGET_AXIS_Z}{target_z_um:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
        #send_gcode_command(move_z_command)
        #send_gcode_command("M400") # Wait for movement to stop
        #time.sleep(1)

    # Final return to absolute positioning (already set, but good to confirm)
    #send_gcode_command("G90")
    #time.sleep(0.1)

    print("\n--- Script Finished ---")
    print("Physical movement should be complete and printer returned to home.")



