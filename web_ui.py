#!/usr/bin/env python
#
# Pi Cam HQ web interface.
#
# This should be started after camera_zmq is running.
#
# Hazen 10/23
# 
import datetime
import json
import threading
import zmq

from flask import Flask, jsonify, render_template, request, Response

app = Flask(__name__,
            static_url_path='',
	    static_folder='static',
            template_folder='templates')

zmqContext = zmq.Context()
zmqSocket = None

sem = threading.Semaphore()


def connect():
    global zmqSocket
    zmqSocket = zmqContext.socket(zmq.REQ)
    zmqSocket.RCVTIMEO = 500
    zmqSocket.connect("tcp://127.0.0.1:9898")    


def send_message(msg):
    global zmqSocket
    
    sem.acquire()
    response = None
    try:
        if zmqSocket is not None:
            zmqSocket.send_string(json.dumps(msg))
            response = json.loads(zmqSocket.recv().decode())
    except zmq.error.Again:
        zmqSocket = None
    except zmq.error.ZMQError:
        zmqSocket = None

    if zmqSocket is None:
        response = {"timeout" : "controller is busy"}
        connect()

    sem.release()
    
    return response


# Load webpage.
@app.route('/picamhq')
def index():
    
    # Get time.
    now = datetime.datetime.now()
    timeStr = "{0:02d}:{1:02d}:{2:02d}".format(now.hour, now.minute, now.second)
    
    return render_template("picamhq.html", time = timeStr)


# External control endpoint.
@app.route('/picamhq/command')
def command():
    """
    A command that nominally came from an external entity (not the web interface).
    """
    return jsonify(send_message({"source" : "ext"} | dict(request.args)))


# Webpage UI javascript endpoint.
@app.route('/picamhq/uiCommand')
def uiCommand():
    """
    A command that nominally came from the web interface.
    """
    return jsonify(send_message({"source" : "ui"} | dict(request.args)))


if __name__ == '__main__':
    connect()
    app.run(debug=True, host='0.0.0.0')

