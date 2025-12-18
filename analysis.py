import numpy as np
import cv2
import tifffile as tif
from camera import Camera
import config as c
import time
import os
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier
import pickle, cloudpickle

def get_focus_score(image):
# --- Laplacian ---
    #im = image
    #print(np.shape(im))
    #score = cv2.Laplacian(im, cv2.CV_64F).var()
    #score = cv2.Laplacian(im, cv2.CV_32F).var()
    #result = f"{score:.6f}"

# --- Tenengrad ---
    red   = image[0::2, 0::2]   # top-left = R
    green1 = image[0::2, 1::2]  # top-right = G
    green2 = image[1::2, 0::2]  # bottom-left = G
    blue  = image[1::2, 1::2]   # bottom-right = B

    channel = green2

    gx = cv2.Sobel(channel, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(channel, cv2.CV_32F, 0, 1, ksize=3)
    fm = gx**2 + gy**2
    mean_fm = np.mean(fm)
    result = f"{mean_fm:.6f}"

# --- Brenner Gradient ---
    #diff = image[:,2:] - image[:,:-2]
    #score = np.sum(diff**2)
    #result = f"{score:.1f}"

# --- Frequency Domain Method ---
    #if len(image.shape) == 3:
        #gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    #else:
        #gray = image

    # Compute 2D FFT
    #f = np.fft.fft2(gray)
    #fshift = np.fft.fftshift(f)  # center low freq

    # Magnitude spectrum
    #magnitude = np.abs(fshift)

    # Normalize
    #magnitude = magnitude / np.max(magnitude)

    # Define a radius around the center (low freq cutoff)
    #rows, cols = gray.shape
    #crow, ccol = rows//2, cols//2
    #radius = min(rows, cols) // 5  # tweakable

    # Mask out low frequencies (keep only high freq energy)
    #mask = np.ones_like(magnitude, dtype=bool)
    #y, x = np.ogrid[:rows, :cols]
    #mask_area = (x-ccol)**2 + (y-crow)**2 <= radius*radius
    #mask[mask_area] = False

    # Focus score = sum of high frequency energy
    #score = np.sum(magnitude[mask])
    #result =f"{score:.6f}"

    return result

def check_focus_score(array):
    focus_score_column = array[:, 1]
    best_focus_score = np.max(focus_score_column)
    row_index_of_best_focus_score = np.argmax(focus_score_column)
    best_focus_score_position = int(array[row_index_of_best_focus_score, 0])
    best_focus_score = int(array[row_index_of_best_focus_score, 1])

    return best_focus_score_position, best_focus_score

def check_image_laplacian(impath, dark_path, background_path):
    if not os.path.exists(impath):
        print(f"Error: Sample image file not found at '{impath}'. Cannot perform Laplacian check.", flush=True)
        return False
    if not os.path.exists(dark_path):
        print(f"Error: Dark-field image file not found at '{dark_path}'. Cannot perform cell count.", flush=True)
        return False
    if not os.path.exists(background_path):
        print(f"Error: Background image file not found at '{background_path}'. Cannot perform cell count.", flush=True)
        return False

    print(f"\n---Analyzing image: {os.path.basename(impath)} ---")
    corrected_img_f32 = image_corrector(impath, dark_path, background_path, scaling_factor=255.0)

    if corrected_img_f32 is None:
        print("Error: Image correction failed. Cannot proceed with Laplacian check.", flush=True)
        return False

    image_laplacian = get_focus_score(corrected_img_f32)
    print(f"\nThe image's Laplacian is {image_laplacian}")
    return image_laplacian

def get_laplacian(image):
    pass

def get_centered_im(im):
    cim = im[::2, 1::2]
    c0 = cim.shape[0] // 2
    c1 = cim.shape[1] // 2
    w0 = 380 // 2  # Matches reduced size.
    w1 = 507 // 2
    cim = cim[c0 - w0:c0 + w0, c1 - w1:c1 + w1]
    cim = np.right_shift(cim, 4).astype(np.uint8)
    return cim

def image_corrector_alt(img_path, dark_path, background_path, scaling_factor=4095.0):
    print(f"\n--- Performing Image Correction for '{os.path.basename(img_path)}' ---", flush=True)

    try:
        img = tif.imread(img_path).astype(np.float32)
        dark = tif.imread(dark_path).astype(np.float32)
        background = tif.imread(background_path).astype(np.float32)
        print(f"Loaded: Sample={img.shape}, Dark={dark.shape}, Background={background.shape}", flush=True)
    except FileNotFoundError as e:
        print(f"Error: One or more input files for image_corrector not found: {e}", flush=True)
        return None
    except Exception as e:
        print(f"Error loading images for correction: {e}", flush=True)
        return None

    if not (img.shape == dark.shape == background.shape):
        print(f"Error: Image shapes do not match for correction. Cannot proceed.", flush=True)
        print(f"  Sample: {img.shape}, Dark: {dark.shape}, Background: {background.shape}", flush=True)
        return None

    # Calculate the denominator (background - dark)
    denominator = background - dark

    # Add a small epsilon to prevent division by zero or very large values if the denominator is zero/near-zero.
    epsilon = 1e-8

    denominator = np.maximum(denominator, epsilon) 

    # Apply the flat-field correction formula: (img - dark) / (background - dark) * scaling_factor
    corrected_img_float = ((img - dark) / denominator) * scaling_factor

    # Clip the output values to ensure they stay within the valid range [0, scaling_factor].
    corrected_img_float = np.clip(corrected_img_float, 0, scaling_factor)

    # TEMPORARY - FIGURE OUT HOW TO FIX!!
    corrected_img_float = corrected_img_float[:,:-9]

    return corrected_img_float

def bggr_values(im):
    b = im[::2, ::2]
    g1 = im[::2, 1::2]
    g2 = im[1::2, ::2]
    r = im[1::2, 1::2]
    return [b, g1, g2, r]

def extract_features_from_image(corrected_img):
    _, g1, _, _ = bggr_values(corrected_img)
    img = g1.copy()
    # Extract date from image name, use to get no-light and no-slide paths
    thresh, mask = cv2.threshold(img.astype(np.uint16), 0, 1, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    no_foreground_pixels = len(mask[mask==1])
    no_background_pixels = len(mask[mask==0])
    ratio_foreground_background_pixels = no_foreground_pixels / no_background_pixels
    intensity_range = np.median(img[mask==0]) - np.median(img[mask==1])
    no_foreground_objects, labels, stats, centroids = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=4)
    object_areas = stats[:, -1]
    num_cell_sized_objects = len(np.where((object_areas > 8) & (object_areas < 1000))[0])
    ratio_cell_sized_objects = num_cell_sized_objects / len(object_areas)
    largest_object_area = np.max(object_areas)
    object_radius_med = np.median(np.sqrt(object_areas))
    background_var = np.var(img[mask==0])
    img_features = ratio_foreground_background_pixels, intensity_range, object_radius_med, ratio_cell_sized_objects, background_var, largest_object_area
    return list(img_features)

def is_good_for_ID(impath, dark_field_impath, background_impath):
    if not os.path.exists(impath):
        print(f"Error: Sample image file not found at '{impath}'. Cannot perform cell count.", flush=True)
        return False
    if not os.path.exists(dark_field_impath):
        print(f"Error: Dark-field image file not found at '{dark_field_impath}'. Cannot perform cell count.", flush=True)
        return False
    if not os.path.exists(background_impath):
        print(f"Error: Background image file not found at '{background_impath}'. Cannot perform cell count.", flush=True)
        return False

    print(f"\n--- Analyzing image: {os.path.basename(impath)} ---", flush=True)

    corrected_img = image_corrector_alt(impath, dark_field_impath, background_impath, scaling_factor=4095.0)

    if corrected_img is None:
        print("Error: Image correction failed. Cannot proceed", flush=True)
        return False

    X_features = extract_features_from_image(corrected_img)

    X_features = np.array(X_features)
    X_features = X_features.reshape((1, X_features.shape[0]))

    with open("scaler.pkl", "rb") as f:
        scaler = pickle.load(f)

    X_new_normalized = scaler.transform(X_features)
    # Load XGBoost model
    xgb_model = XGBClassifier()
    xgb_model.load_model('xgb_image_detection.json')

    y_val = xgb_model.predict(X_new_normalized)

    is_there_bacteria = bool(y_val)
   # returns True if img good for ID, False if not
    return is_there_bacteria


if __name__ == "__main__":
    pass
    imager = Camera()

    back_path = "/home/microscope_auto/Images/no-slide_20251121_M1/no-slide_20251121_M1_10x/no-slide_20251121_M1_10x.tif"
    dark_path = "/home/microscope_auto/Images/no-light_20251121_M1/no-light_20251121_M1_10x/no-light_20251121_M1_10x.tif"

    #filename = "scanning_M5I2UQ_20251107_M1_140x_15y"
    filename = "scanning_M5I2UQ_20251121_M1_SM2_131x_17y_406z"
    #imager.take_rpi_image(100, filename)
    #time.sleep(15)

    impath = f"{c.PI_IMAGE_DIR}/{filename}.tif"
    is_there_bacteria = is_good_for_ID(impath, dark_path, back_path)
    print(is_there_bacteria)
    #for im in im_list:
        #impath = f"/home/microscope_auto/Images/{im}"
        #cell_counter_alt(impath, dark_path, back_path, 600)


