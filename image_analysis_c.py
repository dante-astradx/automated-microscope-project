#
# Python interface to image_analysis.c
#
# Hazen 06/25
#

import ctypes
import numpy as np
from numpy.ctypeslib import ndpointer
import os
import pathlib


# Load and describe the C library.
def load_c_library():
    libPath = pathlib.Path(__file__).parent.resolve()
    return ctypes.cdll.LoadLibrary(os.path.join(libPath, 'lib_image_analysis.so'))

imAna = load_c_library()

imAna.update_x_xx_32.argtypes = [ndpointer(dtype = np.uint32),
                                 ndpointer(dtype = np.uint32),
                                 ndpointer(dtype = np.uint16),
                                 ctypes.c_int,
                                 ctypes.c_int]

imAna.update_x_xx_64.argtypes = [ndpointer(dtype = np.uint64),
                                 ndpointer(dtype = np.uint64),
                                 ndpointer(dtype = np.uint16),
                                 ctypes.c_int,
                                 ctypes.c_int]


# Python wrappers.

def update_x_xx(X, XX, im):
    for i in range(2):
        assert (X.shape[i] == XX.shape[i] == im.shape[i])

    if (X.dtype == np.uint32):
        imAna.update_x_xx_32(X, XX, im, X.shape[0], X.shape[1])
    else:
        imAna.update_x_xx_64(X, XX, im, X.shape[0], X.shape[1])


if (__name__ == "__main__"):
    for at in [np.uint32, np.uint64]:
        X = np.zeros((5,10), dtype = at)
        XX = np.zeros((5,10), dtype = at)
        im = 2*np.ones((5,10), dtype = np.uint16)
        update_x_xx(X, XX, im)
        print(im)
        print(X)
        print(XX)
        print()

    
