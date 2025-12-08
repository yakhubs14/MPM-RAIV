import cv2
from flask import Flask, Response
import threading
import time
import subprocess
import re
import requests
import sys
import os
import numpy as np
from datetime import datetime
import queue

# --- CONFIGURATION ---
CAM_INDICES = [0, 1, 2, 3] # Verify these match your setup
PORT = 5000
SAFE_SCORE_THRESHOLD = 300 

# FIREBASE CONFIG
FIREBASE_BASE_URL = "https://mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app"
URL_ENDPOINT = f"{FIREBASE_BASE_URL}/cam_url.json"
COMMAND_ENDPOINT = f"{FIREBASE_BASE_URL}/command.json"
TELEMETRY_ENDPOINT = f"{FIREBASE_BASE_URL}/telemetry.json"

SAVE_DIR = r"C:\Users\Shukri\Documents\RAIV\Saved Pictures"

app = Flask(__name__)
cmd_queue = queue.Queue() 

# --- GLOBAL STATE ---
vehicle_status = "STANDBY" 
last_save_time = 0
stop_command_sent = False

# GLOBAL FRAME BUFFER (The Secret to Speed)
# Stores the latest JPEG bytes for each camera.
# The streaming threads just READ this, they don't do any processing.
global_frames = [None, None, None, None]

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- BACKGROUND WORKER FOR NETWORK CALLS ---
def network_worker():
    while True:
        cmd = cmd_queue.get()
        if cmd is None: break
        try:
            requests.put(COMMAND_ENDPOINT, json=cmd, timeout=1)
        except: pass
        cmd_queue.task_done()

t_worker = threading.Thread(target=network_worker)
t_worker.daemon = True
t_worker.start()

# --- CAMERA PROCESSOR (Runs in Background) ---
class CameraThread(threading.Thread):
    def __init__(self, index, role):
        threading.Thread.__init__(self)
        self.index = index
        self.role = role
        self.daemon = True
        self.cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
        
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
    
    def run(self):
        global global_frames, last_save_time, stop_command_sent
        obstruction_counter = 0
        
        while True:
            if not self.cap.isOpened():
                time.sleep(2)
                self.cap.open(self.index, cv2.CAP_MSMF)
                continue
            
            success, frame = self.cap.read()
            if not success:
                time.sleep(0.1)
                continue

            # --- PROCESSING ---
            if self.role == "DETECT":
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                blur = cv2.Laplacian(gray, cv2.CV_64F).var()
                mean_b = np.mean(gray)

                if (blur < SAFE_SCORE_THRESHOLD) or (mean_b < 10):
                    obstruction_counter += 1
                else:
                    obstruction_counter = 0
                    if stop_command_sent: stop_command_sent = False # Auto-reset logic

                if obstruction_counter > 3:
                    if not stop_command_sent:
                        print(f"!!! STOP: CAM {self.index} !!!")
                        cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")
                        stop_command_sent = True
                    
                    cv2.rectangle(frame, (0,0), (320,240), (0,0,255), 10)
                    cv2.putText(frame, "STOP", (100, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (0,0,255), 4)

            elif self.role == "CAPTURE":
                if vehicle_status == "MOVING":
                    now = time.time()
                    if now - last_save_time > 1.0:
                        # Simple throttle to avoid saving 4x images at once
                        if self.index == 0 or (self.index == 3 and now - last_save_time > 1.1):
                            last_save_time = now
                            self.save_img(frame)
                            cv2.putText(frame, "REC", (250, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2)

            # --- ENCODE & UPDATE BUFFER ---
            try:
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
                if ret:
                    global_frames[self.index] = buffer.tobytes()
            except: pass
            
            # Allow other threads to run
            time.sleep(0.01)

    def save_img(self, frame):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fn = os.path.join(SAVE_DIR, f"CAM{self.index}_{ts}.jpg")
            cv2.imwrite(fn, frame)
            print(f"Saved: {fn}")
        except: pass

# Start Camera Threads
roles = ["CAPTURE", "DETECT", "DETECT", "CAPTURE"]
for i in range(4):
    CameraThread(i, roles[i]).start()

# --- STREAM GENERATOR ---
def gen(cam_idx):
    while True:
        frame = global_frames[cam_idx]
        if frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            time.sleep(0.04) # Limit to ~25 FPS to save bandwidth
        else:
            time.sleep(0.1)

# --- MONITOR ---
def firebase_monitor():
    global vehicle_status
    print("--- FIREBASE MONITOR STARTED ---")
    while True:
        try:
            r = requests.get(TELEMETRY_ENDPOINT, timeout=2)
            if r.status_code == 200:
                data = r.json()
                if data and "status" in data:
                    vehicle_status = data["status"]
            time.sleep(1.0) 
        except: time.sleep(2.0)

# --- ROUTES ---
@app.route('/')
def index(): return "RAIV VISION SYSTEM ONLINE"

@app.route('/video1')
def video_feed1(): return Response(gen(0), mimetype='multipart/x-mixed-replace; boundary=frame')
@app.route('/video2')
def video_feed2(): return Response(gen(1), mimetype='multipart/x-mixed-replace; boundary=frame')
@app.route('/video3')
def video_feed3(): return Response(gen(2), mimetype='multipart/x-mixed-replace; boundary=frame')
@app.route('/video4')
def video_feed4(): return Response(gen(3), mimetype='multipart/x-mixed-replace; boundary=frame')

# --- STARTUP ---
def start_tunnel():
    print("--- WAITING FOR FLASK ---")
    time.sleep(3)
    
    print("--- STARTING TUNNEL ---")
    # Force cloudflared to point to 127.0.0.1 explicitly
    cmd = ['cloudflared', 'tunnel', '--url', f'http://127.0.0.1:{PORT}']
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    for line in iter(process.stdout.readline, ''):
        match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
        if match:
            url = match.group(0)
            print(f"\n✅ TUNNEL: {url}")
            try: requests.put(URL_ENDPOINT, json=url)
            except: pass
            break 

if __name__ == '__main__':
    try: subprocess.run("taskkill /F /IM cloudflared.exe", shell=True, stdout=subprocess.DEVNULL)
    except: pass
    
    t_tunnel = threading.Thread(target=start_tunnel)
    t_tunnel.daemon = True
    t_tunnel.start()

    t_fb = threading.Thread(target=firebase_monitor)
    t_fb.daemon = True
    t_fb.start()

    print(f"--- SYSTEM ACTIVE ---")
    # Bind to 0.0.0.0 to ensure accessibility
    app.run(host='0.0.0.0', port=PORT, threaded=True)
