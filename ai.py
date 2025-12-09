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
from ultralytics import YOLO 

# --- CONFIGURATION ---
CAM_INDICES = [0, 1, 2, 3] 
PORT = 5000

# ROBUST DETECTION SETTINGS
# 1. AI Thresholds
AI_CONFIDENCE = 0.60       # Only trust detections > 60% confidence
AI_NEAR_AREA_RATIO = 0.35  # Object must cover 35% of screen (Very Close)

# 2. Physical Obstruction Thresholds (Covered Camera)
BLUR_THRESHOLD = 150       # Lower = Blurry. <150 is very blurry.
DARKNESS_THRESHOLD = 5     # Lower = Darker. <5 is pitch black.

# 3. Persistence (The "Anti-Ghost" Filter)
# Must detect danger for this many consecutive frames to stop.
# At 15 AI-FPS, 4 frames = ~0.25 seconds of continuous detection.
DANGER_PERSISTENCE_LIMIT = 4 

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
global_frames = [None, None, None, None]

# AI MODEL - OPTIMIZED FOR SPEED & LOGIC
print("--- LOADING HIGH-SPEED AI MODEL (YOLOv8n) ---")
# Using Nano for max FPS, but relying on robust logic for accuracy.
model = YOLO("yolov8n.pt") 
print("--- AI ENGINE READY ---")

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- NETWORK WORKER (NON-BLOCKING) ---
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
            self.cap.set(cv2.CAP_PROP_FPS, 60) # Request Max FPS
    
    def run(self):
        global global_frames, last_save_time
        
        # Persistence Counters
        danger_counter = 0
        frame_count = 0
        
        while True:
            if not self.cap.isOpened():
                time.sleep(2)
                self.cap.open(self.index, cv2.CAP_MSMF)
                continue
            
            success, frame = self.cap.read()
            if not success:
                time.sleep(0.01)
                continue

            frame_count += 1

            # --- SMART AI DETECTION (Every 3rd Frame for Speed) ---
            # We process AI less often than we stream video.
            # This keeps the video "Butter Smooth" even if AI takes 50ms.
            if self.role == "DETECT" and (frame_count % 3 == 0):
                
                is_danger = False
                
                # A. Physical Check (Is camera covered?)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                mean_b = np.mean(gray)
                
                if mean_b < DARKNESS_THRESHOLD:
                    is_danger = True # Covered with hand (Black)
                else:
                    # B. AI Object Check (Is there a train/person?)
                    # Classes: 0=person, 1=bike, 2=car, 3=motorcycle, 5=bus, 6=train, 7=truck
                    results = model(frame, stream=True, verbose=False, conf=AI_CONFIDENCE, 
                                    classes=[0, 1, 2, 3, 5, 6, 7])
                    
                    for r in results:
                        boxes = r.boxes
                        for box in boxes:
                            x1, y1, x2, y2 = box.xyxy[0]
                            area = (x2 - x1) * (y2 - y1)
                            coverage = area / (320 * 240)
                            
                            # Only stop if object is LARGE (Near)
                            if coverage > AI_NEAR_AREA_RATIO:
                                is_danger = True
                                break
                        if is_danger: break
                
                # Persistence Filter
                if is_danger:
                    danger_counter += 1
                else:
                    # Decay counter slowly instead of instant reset (debouncing)
                    if danger_counter > 0: danger_counter -= 1

                # TRIGGER
                if danger_counter >= DANGER_PERSISTENCE_LIMIT:
                    cam_name = "BACK" if self.index == 1 else "FRONT"
                    print(f"🚨 [CRITICAL] {cam_name} CAM: OBSTACLE CONFIRMED -> STOPPING")
                    cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")
                    # Reset slightly to avoid spamming 100 commands a second
                    danger_counter = DANGER_PERSISTENCE_LIMIT - 1 

            # --- IMAGE CAPTURE ---
            elif self.role == "CAPTURE":
                if vehicle_status == "MOVING":
                    now = time.time()
                    if now - last_save_time > 1.0:
                        if self.index == 0 or (self.index == 3 and now - last_save_time > 1.1):
                            last_save_time = now
                            self.save_img(frame)

            # --- ENCODE & UPDATE BUFFER (PURE VIDEO) ---
            # We encode EVERY frame for the stream, regardless of AI logic
            try:
                # Quality 40 is a good balance for 320p
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
                if ret:
                    global_frames[self.index] = buffer.tobytes()
            except: pass
            
            # Tiny yield
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

# --- STREAM GENERATOR (Zero-Wait) ---
def gen(cam_idx):
    while True:
        frame = global_frames[cam_idx]
        if frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            # 30 FPS Target for Stream (0.033s)
            time.sleep(0.033) 
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

# --- ROUTES ---
@app.route('/')
def index(): return "RAIV AI VISION SYSTEM ONLINE (V11.0)"

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

    print(f"--- SYSTEM ACTIVE (HIGH PERF / AI MODE) ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
