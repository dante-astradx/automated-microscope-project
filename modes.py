
from picamera2 import Picamera2

picam2 = Picamera2()
for ii, mode in enumerate(picam2.sensor_modes):
    print(ii, mode)

for ii, ctrl in enumerate(picam2.camera_controls):
    print(ii, ctrl, picam2.camera_controls[ctrl])


