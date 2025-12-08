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

# GLOBAL FRAME BUFFER (Pre-encoded)
global_frames = [None, None, None, None]

if not os.path.exists(SAVE_DIR):
    try: os.makedirs(SAVE_DIR)
    except: pass

# --- HIGH PRIORITY NETWORK WORKER ---
def network_worker():
    while True:
        cmd = cmd_queue.get()
        if cmd is None: break
        try:
            # Short timeout to fail fast and retry if needed
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
        # MSMF is faster on Windows
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

            # --- OBSTACLE DETECTION (SMART) ---
            if self.role == "DETECT":
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                
                # 1. Blur Check (Object very close)
                blur = cv2.Laplacian(gray, cv2.CV_64F).var()
                
                # 2. Large Object Check (Contour Area)
                # Threshold to black/white
                _, thresh = cv2.threshold(gray, 60, 255, cv2.THRESH_BINARY_INV)
                # Find contours (blobs)
                contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                has_large_obstacle = False
                for cnt in contours:
                    area = cv2.contourArea(cnt)
                    # If a blob covers > 40% of the screen (320x240 = 76800 pixels)
                    # 40% is approx 30,000 pixels
                    if area > 30000:
                        has_large_obstacle = True
                        # Draw it for feedback
                        x,y,w,h = cv2.boundingRect(cnt)
                        cv2.rectangle(frame, (x,y), (x+w,y+h), (0,255,255), 2)
                        break

                # COMBINED LOGIC:
                # If blurry (too close) OR large dark object blocking view
                if (blur < SAFE_SCORE_THRESHOLD) or has_large_obstacle:
                    obstruction_counter += 1
                else:
                    obstruction_counter = 0
                    if stop_command_sent: stop_command_sent = False 

                # Fast Trigger: 2 frames (approx 66ms response)
                if obstruction_counter > 2:
                    if not stop_command_sent:
                        print(f"!!! STOP: CAM {self.index} !!!")
                        # Priority Send
                        cmd_queue.put(f"STOP_EMERGENCY_{int(time.time())}")
                        stop_command_sent = True
                    
                    cv2.rectangle(frame, (0,0), (320,240), (0,0,255), 10)
                    cv2.putText(frame, "STOP", (100, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (0,0,255), 4)

            # --- IMAGE CAPTURE ---
            elif self.role == "CAPTURE":
                if vehicle_status == "MOVING":
                    now = time.time()
                    if now - last_save_time > 1.0:
                        if self.index == 0 or (self.index == 3 and now - last_save_time > 1.1):
                            last_save_time = now
                            self.save_img(frame)
                            cv2.putText(frame, "REC", (250, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2)

            # --- ENCODE & UPDATE BUFFER ---
            # Low quality for speed over network
            try:
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
                if ret:
                    global_frames[self.index] = buffer.tobytes()
            except: pass
            
            # Tiny sleep to prevent CPU 100% usage
            time.sleep(0.005)

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
            # Cap at 20 FPS to save network bandwidth for commands
            time.sleep(0.05) 
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

    print(f"--- SYSTEM ACTIVE ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
