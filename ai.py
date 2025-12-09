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
CAM_INDICES = [0, 1, 2, 3] 
PORT = 5000

# RED DETECTION THRESHOLD
RED_PERCENTAGE_THRESHOLD = 70.0 # Stop if > 70% of screen is red

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

# GLOBAL FRAME BUFFER
global_frames = [None, None, None, None]

# Global Safe Scores for printing
current_safe_scores = {1: 0, 2: 0}
current_red_scores = {1: 0, 2: 0} # Track red percentage

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- NETWORK WORKER ---
def network_worker():
    while True:
        cmd = cmd_queue.get()
        if cmd is None: break
        try:
            requests.put(COMMAND_ENDPOINT, json=cmd, timeout=0.2)
        except: pass
        cmd_queue.task_done()

t_worker = threading.Thread(target=network_worker)
t_worker.daemon = True
t_worker.start()

# --- CAMERA PROCESSOR ---
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
        global global_frames, last_save_time, current_safe_scores, current_red_scores
        
        stop_trigger_count = 0
        cam_label = "BACK" if self.index == 1 else "FRONT"
        
        while True:
            if not self.cap.isOpened():
                time.sleep(2)
                self.cap.open(self.index, cv2.CAP_MSMF)
                continue
            
            success, frame = self.cap.read()
            if not success:
                time.sleep(0.01)
                continue

            # --- OBSTACLE DETECTION (RED COLOR LOGIC) ---
            if self.role == "DETECT":
                # Convert to HSV
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                
                # Define Red Range (Red wraps around 0/180)
                # Lower Red
                lower1 = np.array([0, 100, 100])
                upper1 = np.array([10, 255, 255])
                # Upper Red
                lower2 = np.array([160, 100, 100])
                upper2 = np.array([180, 255, 255])
                
                # Create Masks
                mask1 = cv2.inRange(hsv, lower1, upper1)
                mask2 = cv2.inRange(hsv, lower2, upper2)
                mask = mask1 + mask2
                
                # Calculate Percentage
                total_pixels = 320 * 240
                red_pixels = cv2.countNonZero(mask)
                red_percentage = (red_pixels / total_pixels) * 100
                
                # Update global score for printing
                current_red_scores[self.index] = red_percentage

                # Logic: STOP IF RED > 70%
                if red_percentage > RED_PERCENTAGE_THRESHOLD:
                    stop_trigger_count += 1
                else:
                    stop_trigger_count = 0

                # Require 3 consecutive frames (Anti-Flicker)
                if stop_trigger_count > 3:
                     print(f"\n[⛔ STOP] {cam_label} RED OBSTACLE DETECTED ({red_percentage:.1f}%) -> STOPPING VEHICLE")
                     cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")
                     stop_trigger_count = 2 

            # --- IMAGE CAPTURE ---
            elif self.role == "CAPTURE":
                if vehicle_status == "MOVING":
                    now = time.time()
                    if now - last_save_time > 1.0:
                        if self.index == 0 or (self.index == 3 and now - last_save_time > 1.1):
                            last_save_time = now
                            self.save_img(frame)

            # --- ENCODE ---
            try:
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 35])
                if ret:
                    global_frames[self.index] = buffer.tobytes()
            except: pass
            
            time.sleep(0.001)

    def save_img(self, frame):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fn = os.path.join(SAVE_DIR, f"CAM{self.index}_{ts}.jpg")
            cv2.imwrite(fn, frame)
        except: pass

# Start Camera Threads
# 0=Right(REC), 1=Back(DETECT), 2=Front(DETECT), 3=Left(REC)
roles = ["CAPTURE", "DETECT", "DETECT", "CAPTURE"]
for i in range(4):
    CameraThread(i, roles[i]).start()

# --- STREAM GENERATOR ---
def gen(cam_idx):
    while True:
        frame = global_frames[cam_idx]
        if frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            time.sleep(0.04) 
        else:
            time.sleep(0.1)

# --- MONITOR ---
def firebase_monitor():
    global vehicle_status
    while True:
        try:
            r = requests.get(TELEMETRY_ENDPOINT, timeout=1)
            if r.status_code == 200:
                data = r.json()
                if data and "status" in data:
                    vehicle_status = data["status"]
            time.sleep(1.0) 
        except: time.sleep(2.0)

# --- STATUS PRINTER ---
def status_printer():
    print("--- STATUS PRINTER STARTED ---")
    while True:
        time.sleep(2.0) 
        # Print Red Percentages
        print(f"🔴 RED OBSTACLE: [BACK CAM: {current_red_scores[1]:.1f}%] | [FRONT CAM: {current_red_scores[2]:.1f}%]")

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
    
    t_print = threading.Thread(target=status_printer)
    t_print.daemon = True
    t_print.start()

    print(f"--- SYSTEM ACTIVE ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
