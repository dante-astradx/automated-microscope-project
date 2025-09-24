# ---Microscope ID ---
MICROSCOPE_ID = "M1"
MICROSCOPE_USERNAME = "microscope_auto"

# ---Smart Plug IP ---
SMART_PLUG_IP = "10.116.9.29"

# ---Moonraker Connection ---
MOONRAKER_IP = "10.116.9.239"
MOONRAKER_PORT = 7125

# --- Moonraker API Endpoint ---
GCODE_API_URL = f"http://{MOONRAKER_IP}:{MOONRAKER_PORT}/printer/gcode/script"

# --- Camera URL ---
RPI_URL = f"http://{MOONRAKER_IP}:5000//picamhq"

# --- SSH Configuration ---
HOSTNAME_IP = "192.168.50.10"
USERNAME = "astradx"
#USERNAME = "luisartra"
PORT = 22

# --- Path for uploading images and metadata from pi to laptop or dropbox--- 
LAPTOP_UPLOAD_DIR = "/Users/astradx/Documents/images_raw_backup"
#LAPTOP_UPLOAD_DIR = "/Users/luisartra/Documents/Lab Microscope/lab_GUI"

RSYNC_REMOTE = "/Users/astradx/Documents/images_raw_backup"

RCLONE_REMOTE_ZSTACK = "astradx_dropbox:/Dante Muzila/AstraDx_data/images_raw/CARB-X/Milestone_5"
RCLONE_REMOTE_NO_SLIDE = "astradx_dropbox:/Dante Muzila/AstraDx_data/images_raw/CARB-X/Milestone_5/no-slide"
RCLONE_REMOTE_NO_LIGHT = "astradx_dropbox:/Dante Muzila/AstraDx_data/images_raw/CARB-X/Milestone_5/no-light"

# --- Path for Images directory on Pi ---
PI_IMAGE_DIR = f"/home/{MICROSCOPE_USERNAME}/Images"

# --- Bactera Analysis Thresholds ---
IM_THRESHOLD_MIN = 400.0
IM_THRESHOLD_MAX = 1200.0
CELL_RADIUS_THRESHOLD = 15.0

# --- Target Axis Identifiers ---
TARGET_AXIS_X = "X"
TARGET_AXIS_Y = "Y"
TARGET_AXIS_Z = "Z"

# --- Exposure Time ---
EXPOSURE_TIME_10X = 11000 #7500
EXPOSURE_TIME_20X = 30000 #26000
EXPOSURE_TIME_40X = 100000

# --- Motor Rotation Distance in MM ---
ROTATION_DISTANCE_MM_X = 20
ROTATION_DISTANCE_MM_Y = 20
ROTATION_DISTANCE_MM_Z = 20

# --- Coordinate Mapping ---
MICROSCOPE_X_OFFSET_AT_PRINTER_HOME = 100
MICROSCOPE_Y_OFFSET_AT_PRINTER_HOME = 0
MICROSCOPE_Z_OFFSET_AT_PRINTER_HOME = 0

# --- Before/After Points to image/scan ---
POINTS_BEFORE = 15
POINTS_AFTER = 5

# --- Movement Speed ---
MOVEMENT_SPEED_MM_S = 50

# --- Microscope Scan Limits ---
SM1_X_MIN = 138
SM1_X_MAX = 153
SM2_X_MIN = 120
SM2_X_MAX = 135
SM3_X_MIN = 106
SM3_X_MAX = 117

Y_MIN = 8
Y_MAX = 22

# --- Z Axis focus presets ---
Z_FOCUS_NFRAMES = 10
NFRAMES = 100

# movement increment in microns (um)
INC = 2

# number of locations above/below preset that images will be taken to determine focus point
NUM = 20

# --- Mechanical Ratios for Microscope Unit Conversion ---
# Motor pulley teeth count
NM = 20
NM_Z = 60

# X, Y, Z dial pulley teeth count
NX = 60
NY = 60
NZ = 60

# Dial rotation in degrees to complete one MM of X & Y axis translation
DIAL_ROT_X = 360/18
DIAL_ROT_Y = 360/30
# Dial rotation in degrees to complete one uM (micron) of Z axis translation
DIAL_ROT_Z = 360/200

# Dial rotation in degrees to complete one MM of X & Y translation
MOTOR_ROT_X = (NX / NM) * DIAL_ROT_X
MOTOR_ROT_Y = (NY / NM) * DIAL_ROT_Y
# Dial rotation in degrees to complete one uM of Z translation
MOTOR_ROT_Z = (NZ / NM_Z) * DIAL_ROT_Z

# Converting one degree of motor rotation to MM
ONE_DEG_MOVEMENT_MM_X = ROTATION_DISTANCE_MM_X / 360
ONE_DEG_MOVEMENT_MM_Y = ROTATION_DISTANCE_MM_Y / 360
ONE_DEG_MOVEMENT_MM_Z = ROTATION_DISTANCE_MM_Z / 360

# Calculating the distance movement in MM to complete MOTOR_ROT_X,Y,Z
ROT_DISTANCE_X = MOTOR_ROT_X * ONE_DEG_MOVEMENT_MM_X
ROT_DISTANCE_Y = MOTOR_ROT_Y * ONE_DEG_MOVEMENT_MM_Y
ROT_DISTANCE_Z = MOTOR_ROT_Z * ONE_DEG_MOVEMENT_MM_Z


