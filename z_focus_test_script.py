import requests
import json
import time
import config as c
import sys
from motor import Motor
from camera import Camera

if __name__ == "__main__":
    print("--- Z Focus Control Script ---")

    # Defining objects
    motor_obj = Motor()
    imager = Camera()

    # Number of trails/tests to run
    t = 5

    # Begining running the camera so that it can be communicated with
    camera_server_process_obj = None
    try:
        print("\nMain script: Starting Camera.")

        # start camera server
        camera_server_process_obj = imager.start_camera_server()

        if camera_server_process_obj:
            print("Main script: Camera server started successfully. Proceeding with focusing z axis", flush=True)

            # --- MAIN SCRIPT HERE ---
            # Asking user for scan range at beginning of script so that the input values can be reused for all subsequent runs
            #motor_obj.set_scan_range()

            use_microscope = True
            # Main loop
            while use_microscope:
                # Home Z axes and set absolute positioning
                motor_obj.home_axis("Z")
#                motor_obj.move_z_axis(516)
                motor_obj.set_objective()
                motor_obj.scan_z_axis_for_focus(f"cocci_20x_et_35k_144.5x_10.5y")

#                for t in range(t):
 #                   motor_obj.home_axis("Z")
  #                  motor_obj.set_objective()
   #                 motor_obj.scan_z_axis_for_focus(f"40x_focus_level_test_#{t+1}")

                # Ask user to continue/exit
                while True:
                    response = input("\nWould you like to continue imaging with a different objective? (yes/no): ").lower().strip()
                    if response == 'yes' or response == 'y':
                        print("Returning to start for next scan. Scan range will be the same")
                        break  # exit the inner while loop
                    elif response == 'no' or response == 'n':
                        use_microscope = False
                        print("Exiting program")
                        break  # exit the inner while loop
                    else:
                        print("Invalid input. Please enter 'yes' or 'no'")

            # Program Completion
            print(f"\nPROGRAM IS COMPLETE. Z-STACK IS FINISHED.")

    except Exception as e:
        print(f"\nMain script: An error occurred: {e}", flush=True)
        import traceback

        traceback.print_exc(file=sys.stdout)
    finally:
        # Stop camera server (ensured to run even if errors occur)
        if camera_server_process_obj:
            print("\nMain script: Attempting final camera server shutdown.", flush=True)
            imager.stop_camera_server(camera_server_process_obj)
        print("\nMain script: Finished execution.", flush=True)


