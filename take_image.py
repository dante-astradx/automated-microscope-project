import requests
import json
import time
import config as c
import sys
from camera import Camera

if __name__ == "__main__":
    camera_server_process_obj = None

    try:
        imager = Camera()
        camera_server_process_obj = imager.start_camera_server()

        if camera_server_process_obj:
            imager.take_rpi_image(10, "test_image_of_bacteria")
            time.sleep(10)

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
