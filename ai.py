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
SAFE_SCORE_LIMIT_CAM_2 = 500  # Back View Limit
SAFE_SCORE_LIMIT_CAM_3 = 1000 # Front View Limit

# 2. RED OBSTACLE THRESHOLDS (Color Area %)
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
vehicle_direction = "UNKNOWN" # Tracks FWD or BWD
stop_signal_sent_for_current_move = False # Latch for one-time stop

last_save_time = 0
global_frames = [None, None, None, None]
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
        global stop_signal_sent_for_current_move
        
        stop_trigger_count = 0
        cam_label = "BACK" if self.index == 1 else "FRONT"
        
        my_safe_limit = SAFE_SCORE_LIMIT_CAM_2 if self.index == 1 else SAFE_SCORE_LIMIT_CAM_3
        my_red_limit = RED_THRESHOLD_CAM_2 if self.index == 1 else RED_THRESHOLD_CAM_3
        
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
                # 1. SAFE SCORE
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                blur = cv2.Laplacian(gray, cv2.CV_64F).var()
                current_safe_scores[self.index] = int(blur)

                # 2. RED DETECTION
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                lower1 = np.array([0, 100, 100]); upper1 = np.array([10, 255, 255])
                lower2 = np.array([160, 100, 100]); upper2 = np.array([180, 255, 255])
                mask = cv2.inRange(hsv, lower1, upper1) + cv2.inRange(hsv, lower2, upper2)
                red_pct = (cv2.countNonZero(mask) / (320 * 240)) * 100
                current_red_scores[self.index] = red_pct

                # --- STOP LOGIC (SMART) ---
                is_danger = (red_pct > my_red_limit) or (blur < my_safe_limit)

                if is_danger:
                    stop_trigger_count += 1
                else:
                    stop_trigger_count = 0

                # Check if we should stop
                # 1. Must be persistent (3 frames)
                # 2. Must NOT have sent stop already for this move
                # 3. Must match direction:
                #    - Front Cam (Index 2) stops only if FWD
                #    - Back Cam (Index 1) stops only if BWD
                if stop_trigger_count > 3:
                    should_stop = False
                    
                    if not stop_signal_sent_for_current_move:
                        if self.index == 2 and vehicle_direction == "FWD": # Front Cam & Moving Forward
                            should_stop = True
                        elif self.index == 1 and vehicle_direction == "BWD": # Back Cam & Moving Backward
                            should_stop = True
                    
                    if should_stop:
                        reason = f"RED {red_pct:.1f}%" if red_pct > my_red_limit else f"SCORE {int(blur)}"
                        print(f"\n[⛔ STOP] {cam_label} CAM OBSTACLE ({reason}) -> STOPPING")
                        
                        cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")
                        stop_signal_sent_for_current_move = True # Latch: Won't send again until new move
                        stop_trigger_count = 0 # Reset counter

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
                if ret: global_frames[self.index] = buffer.tobytes()
            except: pass
            
            time.sleep(0.001)

    def save_img(self, frame):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fn = os.path.join(SAVE_DIR, f"CAM{self.index}_{ts}.jpg")
            cv2.imwrite(fn, frame)
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
            time.sleep(0.04) 
        else:
            time.sleep(0.1)

# --- MONITOR (Status & Direction) ---
def firebase_monitor():
    global vehicle_status, vehicle_direction, stop_signal_sent_for_current_move
    
    last_known_status = "STANDBY"
    
    while True:
        try:
            # 1. Get Status (MOVING/STANDBY)
            r_tel = requests.get(TELEMETRY_ENDPOINT, timeout=1)
            if r_tel.status_code == 200:
                data = r_tel.json()
                if data and "status" in data:
                    vehicle_status = data["status"]
                    
                    # RESET LATCH ON NEW MOVE
                    if vehicle_status == "MOVING" and last_known_status != "MOVING":
                        stop_signal_sent_for_current_move = False
                        # print("--- NEW MOVE STARTED: MONITORING FOR OBSTACLES ---")
                    
                    last_known_status = vehicle_status

            # 2. Get Command (To determine Direction)
            r_cmd = requests.get(COMMAND_ENDPOINT, timeout=1)
            if r_cmd.status_code == 200:
                cmd = r_cmd.json()
                # Determine direction from command string
                if cmd and isinstance(cmd, str):
                    if "FWD" in cmd or "FORWARD" in cmd:
                        vehicle_direction = "FWD"
                    elif "BWD" in cmd or "BACKWARD" in cmd:
                        vehicle_direction = "BWD"
            
            time.sleep(0.5) 
        except: time.sleep(1.0)

# --- STATUS PRINTER ---
def status_printer():
    print("--- STATUS PRINTER STARTED ---")
    while True:
        time.sleep(1.0) 
        print(f"📊 STATUS [{vehicle_direction}] | BACK (CAM 1): Score {current_safe_scores[1]} / Red {current_red_scores[1]:.1f}% | FRONT (CAM 2): Score {current_safe_scores[2]} / Red {current_red_scores[2]:.1f}%")

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
