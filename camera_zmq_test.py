#
# Test camera_zmq.
#
# Hazen 6/25
#

import json
import time
import zmq


def send_cmd(socket, cmd):
    socket.send_string(json.dumps(cmd))
    return json.loads(socket.recv().decode())
        
    
def test1():
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect("tcp://127.0.0.1:9898")

    time.sleep(1)
    print(send_cmd(socket, {"command" : "settings"}))
    time.sleep(1)
    print(send_cmd(socket, {"command" : "reduced"}))    
    time.sleep(1)
    print(send_cmd(socket, {"command" : "stop"}))


if (__name__ == "__main__"):
    test1()
