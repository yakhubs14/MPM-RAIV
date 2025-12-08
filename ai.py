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

# SAFE SCORE THRESHOLDS (LOWER = BLURRIER/CLOSER)
SAFE_SCORE_CAM_2 = 300 # Back View
SAFE_SCORE_CAM_3 = 500 # Front View

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

# GLOBAL FRAME BUFFER (Pre-encoded)
global_frames = [None, None, None, None]

# Global Safe Scores for periodic printing
current_safe_scores = {1: 0, 2: 0}
last_print_time = 0

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- HIGH PRIORITY NETWORK WORKER ---
def network_worker():
    while True:
        cmd = cmd_queue.get()
        if cmd is None: break
        try:
            # Ultra-fast timeout to prevent blocking
            requests.put(COMMAND_ENDPOINT, json=cmd, timeout=0.2)
            print(f"\n[⚠️ ALERT] STOP COMMAND SENT: {cmd}")
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
            # LOWEST RESOLUTION FOR MAX SPEED
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
    
    def run(self):
        global global_frames, last_save_time, current_safe_scores
        obstruction_counter = 0
        
        # Set specific threshold based on camera index
        my_threshold = SAFE_SCORE_CAM_2 if self.index == 1 else SAFE_SCORE_CAM_3
        
        # Frame Throttling: Only process every Nth frame to save CPU
        frame_skip = 0
        
        while True:
            if not self.cap.isOpened():
                time.sleep(2)
                self.cap.open(self.index, cv2.CAP_MSMF)
                continue
            
            success, frame = self.cap.read()
            if not success:
                time.sleep(0.1)
                continue

            # Skip processing for smoothness (process 1 out of 2 frames)
            frame_skip += 1
            if frame_skip % 2 != 0:
                # Still update buffer for smooth view, but skip heavy math
                try:
                    ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
                    if ret: global_frames[self.index] = buffer.tobytes()
                except: pass
                continue

            # --- OBSTACLE DETECTION (NO DRAWING) ---
            if self.role == "DETECT":
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                
                # 1. Blur Check
                blur = cv2.Laplacian(gray, cv2.CV_64F).var()
                
                # Update global score for printing
                current_safe_scores[self.index] = int(blur)

                # 2. Large Object Check (Contour Area)
                _, thresh = cv2.threshold(gray, 60, 255, cv2.THRESH_BINARY_INV)
                contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                has_large_obstacle = False
                for cnt in contours:
                    if cv2.contourArea(cnt) > 30000:
                        has_large_obstacle = True
                        break

                # CHECK AGAINST THRESHOLD
                if (blur < my_threshold) or has_large_obstacle:
                    obstruction_counter += 1
                else:
                    obstruction_counter = 0

                # Fast Trigger: 2 frames
                if obstruction_counter > 2:
                    cam_name = "BACK" if self.index == 1 else "FRONT"
                    print(f"\n[⛔ STOP] {cam_name} CAM OBSTRUCTION DETECTED (Score: {int(blur)} < {my_threshold}) -> STOPPING VEHICLE")
                    # Send STOP every time loop runs (continuous safety)
                    cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")

            # --- IMAGE CAPTURE ---
            elif self.role == "CAPTURE":
                if vehicle_status == "MOVING":
                    now = time.time()
                    if now - last_save_time > 1.0:
                        if self.index == 0 or (self.index == 3 and now - last_save_time > 1.1):
                            last_save_time = now
                            self.save_img(frame)
                            # print(f"📸 SNAPSHOT [CAM {self.index}]") 

            # --- ENCODE & UPDATE BUFFER (PURE VIDEO) ---
            try:
                # Quality 30 is fast and good enough for view
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
                if ret:
                    global_frames[self.index] = buffer.tobytes()
            except: pass
            
            # Tiny sleep to yield CPU
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

# --- STREAM GENERATOR (Non-Blocking) ---
def gen(cam_idx):
    while True:
        frame = global_frames[cam_idx]
        if frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            # 30 FPS Cap
            time.sleep(0.03) 
        else:
            time.sleep(0.1)

# --- MONITOR ---
def firebase_monitor():
    global vehicle_status
    print("--- FIREBASE MONITOR STARTED ---")
    while True:
        try:
            r = requests.get(TELEMETRY_ENDPOINT, timeout=1)
            if r.status_code == 200:
                data = r.json()
                if data and "status" in data:
                    vehicle_status = data["status"]
            time.sleep(1.0) 
        except: time.sleep(2.0)

# --- STATUS PRINTER (NEW) ---
def status_printer():
    global last_print_time
    print("--- STATUS PRINTER STARTED ---")
    while True:
        time.sleep(2.0) # Print every 2 seconds
        # Print Safe Scores on one line
        # Using \r to overwrite line for cleaner terminal (optional, standard print is safer for logs)
        print(f"📊 STATUS: [CAM 1 (BACK) Safe Score: {current_safe_scores[1]}] | [CAM 2 (FRONT) Safe Score: {current_safe_scores[2]}]")

# --- ROUTES ---
@app.route('/')
def index(): return "RAIV VISION SYSTEM ONLINE (HEADLESS MODE)"

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
    
    # Start the Status Printer
    t_print = threading.Thread(target=status_printer)
    t_print.daemon = True
    t_print.start()

    print(f"--- SYSTEM ACTIVE (TERMINAL OUTPUT ONLY) ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
