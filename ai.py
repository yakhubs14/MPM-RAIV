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
from ultralytics import YOLO # pip install ultralytics

# --- CONFIGURATION ---
CAM_INDICES = [0, 1, 2, 3] 
PORT = 5000

# UNIFIED THRESHOLDS
# Both cameras now share the same sensitivity
SAFE_SCORE_THRESHOLD = 400 
CONFIDENCE_THRESHOLD = 0.45 # Lowered slightly to catch more objects
NEAR_THRESHOLD_AREA = 0.25  # Object covers 25% of screen = STOP

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

# AI MODEL - UPGRADED TO YOLOv8 SMALL
print("--- LOADING ADVANCED AI MODEL (YOLOv8s) ---")
# 'yolov8s.pt' is more accurate than 'n' but requires more CPU.
# It is a Deep Learning CNN trained on COCO dataset.
model = YOLO("yolov8s.pt") 
print("--- DEEP LEARNING MODEL READY ---")

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- NETWORK WORKER (NON-BLOCKING) ---
def network_worker():
    while True:
        cmd = cmd_queue.get()
        if cmd is None: break
        try:
            # Fast timeout, fire and forget
            requests.put(COMMAND_ENDPOINT, json=cmd, timeout=0.5)
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
        global global_frames, last_save_time
        
        stop_trigger_count = 0
        
        while True:
            if not self.cap.isOpened():
                time.sleep(2)
                self.cap.open(self.index, cv2.CAP_MSMF)
                continue
            
            success, frame = self.cap.read()
            if not success:
                time.sleep(0.1)
                continue

            # --- AI OBSTACLE DETECTION (ADVANCED) ---
            if self.role == "DETECT":
                # Run Inference
                # Stream=True for performance
                # Specific classes for RAILWAY context:
                # 0:person, 1:bicycle, 2:car, 3:motorcycle, 5:bus, 6:train, 7:truck, 
                # 16:dog, 17:horse, 18:sheep, 19:cow (Animals on track)
                results = model(frame, stream=True, verbose=False, conf=CONFIDENCE_THRESHOLD, 
                                classes=[0, 1, 2, 3, 5, 6, 7, 16, 17, 18, 19])
                
                danger_detected = False
                
                # Check for physical obstruction (Blur/Covered)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                blur_score = cv2.Laplacian(gray, cv2.CV_64F).var()
                mean_brightness = np.mean(gray)
                
                # Condition A: Camera is covered or blind
                if blur_score < SAFE_SCORE_THRESHOLD or mean_brightness < 10:
                    danger_detected = True
                
                # Condition B: AI Detected Object
                if not danger_detected:
                    for r in results:
                        boxes = r.boxes
                        for box in boxes:
                            # Calculate Area Coverage
                            x1, y1, x2, y2 = box.xyxy[0]
                            box_area = (x2 - x1) * (y2 - y1)
                            total_area = 320 * 240
                            coverage = box_area / total_area
                            
                            # Logic: If valid object covers > 25% of screen
                            if coverage > NEAR_THRESHOLD_AREA:
                                danger_detected = True
                                break 
                        if danger_detected: break
                
                if danger_detected:
                    stop_trigger_count += 1
                else:
                    stop_trigger_count = 0
                
                # TRIGGER STOP (Immediate & Robust)
                # 2 consecutive frames = ~66ms reaction time
                if stop_trigger_count >= 2:
                    cam_name = "BACK" if self.index == 1 else "FRONT"
                    print(f"🚨 DANGER! {cam_name} CAM DETECTED THREAT -> SENDING STOP")
                    cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")

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
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
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

# --- ROUTES ---
@app.route('/')
def index(): return "RAIV AI VISION SYSTEM ONLINE"

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

    print(f"--- SYSTEM ACTIVE (YOLOv8 SMALL MODE) ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
