#
# Capture an image stack using a custom output.
#
# This is ~2x faster when using the C library for the math.
# Note that uint32 will overflow around 1000 frames depending
#
# Hazen 06/25
#

from picamera2 import Picamera2
from picamera2.encoders import Encoder
from picamera2.outputs import Output
import numpy as np
import time

import image_analysis_c as iac


class OutputCapture(Output):

    def __init__(self, nframes = None, size = None, verbose = None, **kwargs):
        super().__init__(**kwargs)
        self.nframes = nframes
        self.n = 0
        self.size = (size[1], size[0])
        self.verbose = verbose
        self.X = None
        self.XX = None

    def done(self):
        return (self.n == self.nframes)

    def get_mean_var(self):
        X = self.X.astype(float)
        XX = self.XX.astype(float)
        pixelMean = X/self.n
        pixelVar = XX/self.n - pixelMean * pixelMean
        return [pixelMean, pixelVar]

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        if (self.n < self.nframes):
            im = np.frombuffer(frame, dtype = np.uint16)
            im = np.reshape(im, (self.size[0], -1))
            #im = im[:,:self.size[1]].astype(np.uint32)
            if self.verbose:
                print(timestamp)
            if (self.X is None):
                self.X = np.zeros(im.shape, dtype = np.uint32)
                self.XX = np.zeros_like(self.X)
            iac.update_x_xx(self.X, self.XX, im)
            self.n += 1


def capture(nframes, exposureTime, verbose = True):
    picam2 = Picamera2()

    size = (4056, 3040)
    config = picam2.create_video_configuration(raw = {'format' : 'SRGGB12', 'size' : (4056, 3040)})
    #config = picam2.create_still_configuration(raw = {'format' : 'SRGGB12', 'size' : (4056, 3040)})
    picam2.configure(config)
    picam2.set_controls({"AeEnable" : False, "ExposureTime" : exposureTime, "AnalogueGain" : 1.0})
    picam2.encode_stream_name = "raw"
    
    encoder = Encoder()
    output = OutputCapture(nframes = nframes, size = size, verbose = False)

    picam2.start()
    time.sleep(1)
    
    if verbose:
        metadata = picam2.capture_metadata()
        controls = {c: metadata[c] for c in ["ExposureTime", "AnalogueGain"]}
        print(controls)
    
    picam2.start_encoder(encoder, output)
    start = time.time()
    while(not output.done()):
        if verbose:
            print(output.n)
        time.sleep(1)
    stop = time.time()
    picam2.stop_encoder()

    picam2.stop()

    if verbose:
        print("Acquired {0:d} frames at {1:.1f} FPS".format(nframes, nframes/(stop - start)))

    return output.get_mean_var()


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
