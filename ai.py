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

# 1. SAFE SCORE THRESHOLDS (Blur/Sharpness)
# Lower = Blurry/Blocked. If below this, STOP.
SAFE_SCORE_LIMIT_CAM_2 = 500  # Back View Limit
SAFE_SCORE_LIMIT_CAM_3 = 1000 # Front View Limit

# 2. RED OBSTACLE THRESHOLDS (Color Area %)
# Higher = More Red. If above this, STOP.
RED_THRESHOLD_CAM_2 = 70.0 # Back View Red % Limit
RED_THRESHOLD_CAM_3 = 70.0 # Front View Red % Limit

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

# Global Stats for printing
current_safe_scores = {1: 0, 2: 0}
current_red_scores = {1: 0.0, 2: 0.0} 

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- NETWORK WORKER ---
def network_worker():
    while True:
        cmd = cmd_queue.get()
        if cmd is None: break
        try:
            # Ultra-fast timeout (0.1s) for instant fire-and-forget
            requests.put(COMMAND_ENDPOINT, json=cmd, timeout=0.1)
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
        
        # Determine specific thresholds for this camera
        my_safe_limit = SAFE_SCORE_LIMIT_CAM_2 if self.index == 1 else SAFE_SCORE_LIMIT_CAM_3
        my_red_limit = RED_THRESHOLD_CAM_2 if self.index == 1 else RED_THRESHOLD_CAM_3
        
        # State tracking to avoid spamming
        is_currently_stopped = False
        
        while True:
            if not self.cap.isOpened():
                time.sleep(2)
                self.cap.open(self.index, cv2.CAP_MSMF)
                continue
            
            success, frame = self.cap.read()
            if not success:
                time.sleep(0.01)
                continue

            # --- OBSTACLE DETECTION ---
            if self.role == "DETECT":
                # 1. SAFE SCORE (Blur)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                blur = cv2.Laplacian(gray, cv2.CV_64F).var()
                current_safe_scores[self.index] = int(blur)

                # 2. RED DETECTION (Color)
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                lower1 = np.array([0, 100, 100])
                upper1 = np.array([10, 255, 255])
                lower2 = np.array([160, 100, 100])
                upper2 = np.array([180, 255, 255])
                mask = cv2.inRange(hsv, lower1, upper1) + cv2.inRange(hsv, lower2, upper2)
                
                total_pixels = 320 * 240
                red_pixels = cv2.countNonZero(mask)
                red_percentage = (red_pixels / total_pixels) * 100
                current_red_scores[self.index] = red_percentage

                # --- STOP LOGIC ---
                is_danger = False
                reason = ""

                if red_percentage > my_red_limit:
                    is_danger = True
                    reason = f"RED OBSTACLE ({red_percentage:.1f}% > {my_red_limit}%)"
                elif blur < my_safe_limit:
                    is_danger = True
                    reason = f"LOW SAFE SCORE ({int(blur)} < {my_safe_limit})"

                if is_danger:
                    stop_trigger_count += 1
                else:
                    stop_trigger_count = 0
                    is_currently_stopped = False # Reset state immediately when clear

                # Fast Trigger: 3 frames (~100ms)
                if stop_trigger_count > 3:
                     # Only print/send if we haven't already locked it down recently 
                     # OR if we want to ensure it stays stopped (send every ~30 frames?)
                     # Here we send continuously but check state to avoid console spam
                     if not is_currently_stopped:
                         print(f"\n[⛔ STOP] {cam_label} CAM: {reason} -> STOPPING VEHICLE")
                         is_currently_stopped = True
                     
                     # INSTANT SEND (Always send while danger exists to ensure vehicle doesn't move)
                     cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")
                     
                     # Cap counter to prevent overflow but keep > 3
                     stop_trigger_count = 4 

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
        time.sleep(1.0) # Print every 1 second
        print(f"📊 STATUS | BACK (CAM 1): Score {current_safe_scores[1]} / Red {current_red_scores[1]:.1f}% | FRONT (CAM 2): Score {current_safe_scores[2]} / Red {current_red_scores[2]:.1f}%")

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
