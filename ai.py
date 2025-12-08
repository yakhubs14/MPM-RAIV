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

# --- CONFIGURATION ---
# Verify these indices match your USB ports!
# Try swapping 0/1/2/3 if cams are swapped.
CAM_1_INDEX = 0 # Right Track
CAM_2_INDEX = 1 # Back View
CAM_3_INDEX = 2 # Front View 
CAM_4_INDEX = 3 # Left Track

PORT = 5000

# FIREBASE CONFIG
FIREBASE_BASE_URL = "https://mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app"
URL_ENDPOINT = f"{FIREBASE_BASE_URL}/cam_url.json"
COMMAND_ENDPOINT = f"{FIREBASE_BASE_URL}/command.json"
TELEMETRY_ENDPOINT = f"{FIREBASE_BASE_URL}/telemetry.json"

# SAVE PATH
SAVE_DIR = r"C:\Users\Shukri\Documents\RAIV\Saved Pictures"

app = Flask(__name__)

# --- GLOBAL STATE ---
vehicle_status = "STANDBY" 
last_save_time = 0
stop_command_sent = False
# We track obstruction persistence to avoid false triggers
obstruction_counters = {1: 0, 2: 0} # Cam 2 and 3

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- CAMERA HANDLING ---
class VideoCamera(object):
    def __init__(self, index, role="VIEW"):
        # Use CAP_DSHOW on Windows for faster init
        self.video = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        self.role = role 
        self.index = index
        self.is_obstacle = False
        
        if self.video.isOpened():
            # Lower resolution is critical for 4 cameras on one USB bus
            self.video.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            self.video.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
            self.video.set(cv2.CAP_PROP_FPS, 15)
        else:
            print(f"Warning: Camera {index} failed to open.")
    
    def __del__(self): 
        if self.video.isOpened(): self.video.release()
    
    def get_frame_and_process(self, cam_id):
        if not self.video.isOpened(): 
            # Try to reconnect if failed previously
            self.video.open(self.index, cv2.CAP_DSHOW)
            if not self.video.isOpened(): return None

        success, image = self.video.read()
        if not success: return None
        
        # --- 1. OBSTRUCTION DETECTION (Front/Rear Cams) ---
        if self.role == "DETECT":
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            mean_brightness = np.mean(gray)
            std_dev = np.std(gray)
            
            # TUNED THRESHOLDS: 
            # Brightness < 5 (Pitch Black) OR StdDev < 3 (Completely uniform/covered)
            # We use a counter so it must happen for 5 consecutive frames
            if mean_brightness < 10 or std_dev < 5: 
                obstruction_counters[self.index] += 1
            else:
                obstruction_counters[self.index] = 0
                self.is_obstacle = False

            # Trigger only if persistent (approx 0.3 seconds)
            if obstruction_counters[self.index] > 5:
                self.is_obstacle = True
                trigger_emergency_stop(cam_id)
                
                # VISUAL ALERT ON CAMERA
                # Red Box Border
                cv2.rectangle(image, (0,0), (320,240), (0,0,255), 15)
                # Text Overlay
                cv2.putText(image, "OBSTACLE DETECTED", (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                cv2.putText(image, "VEHICLE STOPPED", (40, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            else:
                reset_stop_flag() 
        
        # --- 2. IMAGE CAPTURE (Side Track Cams) ---
        elif self.role == "CAPTURE":
            global last_save_time
            if vehicle_status == "MOVING":
                now = time.time()
                if now - last_save_time > 1.0:
                    last_save_time = now # Update global timer
                    save_snapshot(image, cam_id)
                    cv2.putText(image, "REC", (280, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

        # Cam ID Overlay
        if not self.is_obstacle:
            cv2.putText(image, f"CAM {cam_id}", (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
        
        try:
            ret, jpeg = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
            return jpeg.tobytes()
        except: return None

# --- HELPER FUNCTIONS ---
def trigger_emergency_stop(cam_id):
    global stop_command_sent
    if not stop_command_sent:
        print(f"!!! STOP CMD: OBSTACLE ON CAM {cam_id} !!!")
        try:
            cmd = f"STOP_EMERGENCY_{int(time.time())}"
            requests.put(COMMAND_ENDPOINT, json=cmd)
            stop_command_sent = True
        except: pass

def reset_stop_flag():
    global stop_command_sent
    stop_command_sent = False

def save_snapshot(image, cam_id):
    # Run in thread to not block video
    def _save():
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fn = os.path.join(SAVE_DIR, f"CAM{cam_id}_{ts}.jpg")
            cv2.imwrite(fn, image)
            print(f"Saved: {fn}")
        except: pass
    threading.Thread(target=_save).start()

# --- GLOBAL CAMERAS ---
cameras = [None, None, None, None] 
indices = [CAM_1_INDEX, CAM_2_INDEX, CAM_3_INDEX, CAM_4_INDEX]
roles   = ["CAPTURE", "DETECT", "DETECT", "CAPTURE"]

def gen(cam_idx):
    global cameras
    while True:
        try:
            # Init Camera if missing
            if cameras[cam_idx] is None:
                try: 
                    cameras[cam_idx] = VideoCamera(indices[cam_idx], roles[cam_idx])
                except: pass
                time.sleep(1.0) # Wait before retry
                continue
            
            # Get Frame
            frame = cameras[cam_idx].get_frame_and_process(cam_idx + 1)
            
            if frame: 
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
                time.sleep(0.05) # 20 FPS Cap
            else:
                # If frame fails (camera disconnected), kill object to force re-init
                cameras[cam_idx] = None
                time.sleep(0.5)

        except GeneratorExit:
            # Browser closed tab - Allow clean exit
            return
        except Exception as e: 
            time.sleep(0.5)

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
def index():
    return "RAIV VISION SERVER ONLINE"

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
    print("--- STARTING CLOUD TUNNEL ---")
    cmd = ['cloudflared', 'tunnel', '--url', f'http://localhost:{PORT}']
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
    t_tunnel = threading.Thread(target=start_tunnel)
    t_tunnel.daemon = True
    t_tunnel.start()

    t_fb = threading.Thread(target=firebase_monitor)
    t_fb.daemon = True
    t_fb.start()

    print(f"--- SYSTEM ACTIVE ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
