import requests
import json
import time
import config as c
import sys
from motor import Motor

if __name__ == "__main__":
    motor = Motor()

    motor.home_axis("Y")
    motor.move_y_axis(15)
