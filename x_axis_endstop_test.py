import requests
import json
import time

# --- Configuration ---
# IP address or hostname of your Raspberry Pi running Moonraker
MOONRAKER_IP = "10.116.9.228"
# Default Moonraker port (usually 7125)
MOONRAKER_PORT = 7125

# Axis identifiers for G-code
TARGET_AXIS_X = "X"

# From your printer.cfg:
# rotation_distance for stepper_x is 20mm per motor rotation
ROTATION_DISTANCE_MM_X = 20

# Speed for the movement (mm/s). Be cautious with high speeds.
MOVEMENT_SPEED_MM_S = 50

# --- Microscope to Printer Coordinate Mapping ---
# When printer is at its homed (0,0) position, what are the microscope readings?
# Microscope X=100 corresponds to Printer X=0
MICROSCOPE_X_OFFSET_AT_PRINTER_HOME = 100

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

# --- Math to convert rotation of X axis motor to rotation distance (mm) ---
# Motor pulley teeth count
Nm = 20
# X-axis dial pulley teeth count
Nx = 60

# One full rotation (360 deg) of X-axis dial results in 18 units of linear translation (on microscope)
# Dial rotation in degrees to acheive one unit of x-axis translation
dial_rot_x = 360/18

# Motor rotation in degrees to achieve one unit of x-axis translation
motor_rot_x = (Nx / Nm) * dial_rot_x

# As defined by ROTATION_DISTANCE_MM, one full rotation of the motor equals 20mm of movement for stepper_x
# Movement in mm of 1 degree rotation
one_deg_movement_mm_x = ROTATION_DISTANCE_MM_X /360


# Calculating the distance movement in MM to achieve motor_rot_x
rot_distance_one_unit_x = motor_rot_x * one_deg_movement_mm_x # mm of motor movement for 1 microscope X unit

# --- Function to calculate the distance movement in MM for x-axis units ---
def get_motor_distance_x(microscope_x_units):
    """
    Calculates the motor's absolute MM position for a given microscope x-axis unit.
    Applies the offset to map microscope units to printer's (0,0) origin.
    """
    # Convert microscope X unit to units relative to printer's home (0)
    printer_relative_x_units = microscope_x_units - MICROSCOPE_X_OFFSET_AT_PRINTER_HOME
    # print(f"  Microscope X: {microscope_x_units}, Printer Relative X: {printer_relative_x_units}") # Uncomment for verbose
    return rot_distance_one_unit_x * printer_relative_x_units

# --- Main script logic ---
if __name__ == "__main__":
    print("--- Starting Stepper Motor Control Script ---")

    # --- Get user input for scan range ---
    print("\nPlease enter the desired scan range for the microscope (X-axis: 100-190).")
    
    while True:
        try:
            start_x = int(input("Enter starting X position (e.g., 130): "))
            end_x = int(input("Enter ending X position (e.g., 150): "))
            
            if not (100 <= start_x <= 190 and 100 <= end_x <= 190 and start_x <= end_x):
                print("Invalid X range. X positions must be between 100 and 190, and start_x <= end_x.")
            else:
                break # Valid input, exit loop
        except ValueError:
            print("Invalid input. Please enter integers only.")

    print(f"\nScan range set: X from {start_x} to {end_x}.")

    # Home X axis using G28.
    print(f"\nHoming X axis (G28)...")
    send_gcode_command("G28 X")
    send_gcode_command("M400")
    time.sleep(5)

    # Set coordinate system to absolute positioning
    send_gcode_command("G90")
    time.sleep(0.1)

    print(f"n\Homing succesfull and absolute positioning set")

    # Move x-axis forward by a set amount
    #target_x_mm = get_motor_distance_x(175)
    #move_x_command = f"G0 {TARGET_AXIS_X}{target_x_mm:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
    #send_gcode_command(move_x_command)
    #send_gcode_command("M400") # Wait for movement to stop
    #time.sleep(0.5)

    # --- Loop through positions ---

    # Loop over X positions (from start_x to end_x inclusive)
    #for x_current_unit in range(start_x, end_x+1):
        # Calculate the absolute X position in mm from the current X uit, applying offset
        #target_x_mm = get_motor_distance_x(x_current_unit)

        #print(f"\n Moving X position (microscope X: {x_current_unit}) (to {target_x_mm:.3f}mm from printer home)")
        #move_x_command = f"G0 {TARGET_AXIS_X}{target_x_mm:.3f} F{MOVEMENT_SPEED_MM_S * 60}"
        #send_gcode_command(move_x_command)
        #send_gcode_command("M400") # Wait for movement to stop
        #time.sleep(1)

    # Final return to absolute positioning (already set, but good to confirm)
    send_gcode_command("G90")
    time.sleep(0.1)

    print("\n--- Script Finished ---")
    print("Physical movement should be complete and printer returned to home.")

