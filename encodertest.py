#
# Encoder testing.
#
# Hazen 06/25
#

from picamera2 import Picamera2
from picamera2.encoders import Encoder
from picamera2.outputs import Output
import numpy as np
import tifffile
import time


class OutputTest(Output):

    def __init__(self, nframes = None, size = None, **kwargs):
        super().__init__(**kwargs)
        self.nframes = nframes
        self.n = 0
        self.size = (size[1], size[0])
        self.verbose = False
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
            im = im[:,:self.size[1]].astype(np.uint32)
#            im = im[:,:self.size[1]].astype(int)
#            im = im[:,:self.size[1]]
            if self.verbose:
                print(timestamp)
            if (self.X is None):
                self.X = np.zeros(im.shape, dtype = np.uint32)
                self.XX = np.zeros_like(self.X)
            self.X += im
            self.XX += im*im
            self.n += 1


def encoder_test(nframes):
    picam2 = Picamera2()

    size = (4056, 3040)
    config = picam2.create_video_configuration(raw = {'format' : 'SRGGB12', 'size' : (4056, 3040)})
    picam2.configure(config)
    picam2.encode_stream_name = "raw"
    encoder = Encoder()

    nframes = 100
    output = OutputTest(nframes = nframes, size = size)

    picam2.start()
    picam2.set_controls({"AeEnable" : False, "ExposureTime" : 1000, "AnalogueGain" : 1.0})
    time.sleep(1)
    
    picam2.start_encoder(encoder, output)
    start = time.time()
    while(not output.done()):
        print(output.n)
        time.sleep(0.5)
    stop = time.time()
    picam2.stop_encoder()
    picam2.stop()
    
    print("Acquired {0:d} frames at {1:.1f} FPS".format(nframes, nframes/(stop - start)))

    with tifffile.TiffWriter("test.tif") as tfw:
        [pmean, pvar] = output.get_mean_var()
        tfw.write(pmean.astype(np.float32))
        tfw.write(pvar.astype(np.float32))


if (__name__ == "__main__"):
    encoder_test(100)
