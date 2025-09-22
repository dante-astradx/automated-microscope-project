import subprocess
import config as c

def turn_on():
    """Turn on the microscope light."""
    try:
        subprocess.run(
            ["kasa", "--host", c.SMART_PLUG_IP, "on"],
            check=True,
            capture_output=True,
            text=True,
        )
        print("Light turned ON.")
    except subprocess.CalledProcessError as e:
        print(f"Error turning on light: {e.stderr}")


def turn_off():
    """Turn off the microscope light."""
    try:
        subprocess.run(
            ["kasa", "--host", c.SMART_PLUG_IP, "off"],
            check=True,
            capture_output=True,
            text=True,
        )
        print("Light turned OFF.")
    except subprocess.CalledProcessError as e:
        print(f"Error turning off light: {e.stderr}")

def check_light_state():
    result = subprocess.run(["kasa", "--host", c.SMART_PLUG_IP, "state"], capture_output=True, text=True)

    for line in result.stdout.splitlines():
        if line.strip().startswith("State (state):"):
            # The line looks like: "State (state): False"
            state_value = line.split(":")[-1].strip()
            return state_value == "True"   # returns True if on, False if off

    return None  # couldn’t find the line

def toggle_light():
    light_state = check_light_state()
    print(f"Light state is: {light_state}")
    if check_light_state():
        print("Light is on. Turning it OFF")
        turn_off()
        return "OFF"
    else:
        print("Light is off. Turning it ON")
        turn_on()
        return "ON"

if __name__ == '__main__':
    pass
