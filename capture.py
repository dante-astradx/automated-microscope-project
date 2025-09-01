#
# Capture image stack from a Raspberry Pi camera.
#
# Hazen 06/25
#

from picamera2 import Picamera2
import numpy as np
import time

import image_analysis_c as iac


def capture(nframes, exposureTime, verbose = True):
    picam2 = Picamera2()

    config = picam2.create_still_configuration(raw = {'format': 'SRGGB12', 'size' : (4056, 3040)})
    picam2.configure(config)
    picam2.set_controls({"AeEnable" : False, "ExposureTime" : exposureTime, "AnalogueGain" : 1.0})

    picam2.start()
    time.sleep(1)

    if verbose:
        metadata = picam2.capture_metadata()
        controls = {c: metadata[c] for c in ["ExposureTime", "AnalogueGain"]}
        print(controls)
#
#        print(picam2.controls.ExposureTime, picam2.controls.AnalogueGain)


    start = time.time()
    X = None
    XX = None
    for i in range(nframes):
        raw = picam2.capture_array("raw")

        if False:
            metadata = picam2.capture_metadata()
            controls = {c: metadata[c] for c in ["ExposureTime", "AnalogueGain"]}
            print(controls)

        #raw16 = raw.view(np.uint16).astype(np.uint32)[:,:4056]
        raw16 = raw.view(np.uint16)
        if (X is None):
            X = np.zeros(raw16.shape, dtype = np.uint32)
            XX = np.zeros_like(X)
        iac.update_x_xx(X, XX, raw16)

    picam2.stop()

    if verbose:
        print("Acquired {0:d} frames at {1:.1f} FPS".format(nframes, nframes/(time.time() - start)))

    X = X.astype(float)
    XX = XX.astype(float)
    pixelMean = X/nframes
    pixelVar = XX/nframes - pixelMean * pixelMean

    return [pixelMean, pixelVar]


if (__name__ == "__main__"):
    import sys
    import tifffile

    if (len(sys.argv) != 4):
       print("Usage <file> <frames> <exposure time (us)>")
       exit()

    pmean, pvar = capture(int(sys.argv[2]), int(sys.argv[3]))

    print("{0:.1f}".format(np.mean(pmean)))

    with tifffile.TiffWriter(sys.argv[1]) as tfw:
        tfw.write(pmean.astype(np.float32))
        tfw.write(pvar.astype(np.float32))
