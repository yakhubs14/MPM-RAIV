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
# Camera Indices (0,1,2,3 - Verify these match your physical ports)
CAM_1_INDEX = 0 # Right Track
CAM_2_INDEX = 1 # Back View (Obstacle Detection)
CAM_3_INDEX = 2 # Front View (Obstacle Detection)
CAM_4_INDEX = 3 # Left Track

PORT = 5000

# FIREBASE CONFIG
# We need the base URL for REST API calls
FIREBASE_BASE_URL = "https://mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app"
URL_ENDPOINT = f"{FIREBASE_BASE_URL}/cam_url.json"
COMMAND_ENDPOINT = f"{FIREBASE_BASE_URL}/command.json"
TELEMETRY_ENDPOINT = f"{FIREBASE_BASE_URL}/telemetry.json"

# SAVE PATH
SAVE_DIR = r"C:\Users\Shukri\Documents\RAIV\Saved Pictures"

app = Flask(__name__)

# --- GLOBAL STATE ---
vehicle_status = "STANDBY" # Updated via Firebase polling
last_save_time = 0
stop_command_sent = False # Debounce flag

# Ensure Save Directory Exists
if not os.path.exists(SAVE_DIR):
    try:
        os.makedirs(SAVE_DIR)
        print(f"✅ Created Directory: {SAVE_DIR}")
    except Exception as e:
        print(f"❌ Error creating directory: {e}")

# --- CAMERA HANDLING ---
class VideoCamera(object):
    def __init__(self, index, role="VIEW"):
        self.video = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        self.role = role # "VIEW", "DETECT", "CAPTURE"
        self.index = index
        
        if not self.video.isOpened():
            print(f"Warning: Camera {index} could not be opened.")
        
        # Optimization
        self.video.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        self.video.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        self.video.set(cv2.CAP_PROP_FPS, 15)
    
    def __del__(self): 
        if self.video.isOpened(): self.video.release()
    
    def get_frame_and_process(self, cam_id):
        if not self.video.isOpened(): return None
        success, image = self.video.read()
        if not success: return None
        
        # --- 1. OBSTRUCTION DETECTION (Front/Rear Cams) ---
        if self.role == "DETECT":
            # Convert to grayscale for analysis
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            # Calculate mean brightness and standard deviation
            mean_brightness = np.mean(gray)
            std_dev = np.std(gray)
            
            # Thresholds: Low brightness OR extremely low contrast (flat color/covered)
            # You may need to tune these values based on ambient light
            if mean_brightness < 10 or std_dev < 5: 
                trigger_emergency_stop(cam_id)
                cv2.putText(image, "OBSTRUCTION DETECTED!", (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                # Draw red border
                cv2.rectangle(image, (0,0), (320,240), (0,0,255), 10)
            else:
                reset_stop_flag() # Reset if clear
        
        # --- 2. IMAGE CAPTURE (Side Track Cams) ---
        elif self.role == "CAPTURE":
            global last_save_time, vehicle_status
            now = time.time()
            # Save every 1 second IF vehicle is moving
            if vehicle_status == "MOVING" and (now - last_save_time > 1.0):
                # We handle the saving in the main loop to avoid blocking *this* generator thread too much,
                # or simpler: verify it's this specific camera's turn. 
                # Since we have 2 capture cams, we can just save.
                save_snapshot(image, cam_id)
                # Note: last_save_time updates globally, so cam 1 and 4 might alternate or sync. 
                # To force both, we'd need per-camera timers. For now, shared timer is okay.

        # Add Overlay
        cv2.putText(image, f"CAM {cam_id} [{self.role}]", (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        
        # Compression
        ret, jpeg = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
        return jpeg.tobytes()

# --- HELPER FUNCTIONS ---
def trigger_emergency_stop(cam_id):
    global stop_command_sent
    if not stop_command_sent:
        print(f"!!! CRITICAL: OBSTRUCTION ON CAM {cam_id} - SENDING STOP !!!")
        try:
            # Send unique command to force update
            cmd = f"STOP_EMERGENCY_{int(time.time())}"
            requests.put(COMMAND_ENDPOINT, json=cmd)
            stop_command_sent = True
        except Exception as e:
            print(f"Failed to send STOP: {e}")

def reset_stop_flag():
    global stop_command_sent
    # Only reset if we were stopped previously? 
    # Actually, simpler: just allow sending stop again if needed.
    # We use a simple debounce so we don't spam Firebase 15 times a second.
    stop_command_sent = False

def save_snapshot(image, cam_id):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{SAVE_DIR}\\CAM{cam_id}_{timestamp}.jpg"
        cv2.imwrite(filename, image)
        print(f"📸 Saved: {filename}")
        # Update global timer inside the calling logic is safer if we want exact 1s
    except Exception as e:
        print(f"Save Error: {e}")

# --- GLOBAL CAMERAS ---
# CAM 1 (Right Track) -> CAPTURE
# CAM 2 (Back) -> DETECT
# CAM 3 (Front) -> DETECT
# CAM 4 (Left Track) -> CAPTURE
cameras = [None, None, None, None] 
indices = [CAM_1_INDEX, CAM_2_INDEX, CAM_3_INDEX, CAM_4_INDEX]
roles   = ["CAPTURE", "DETECT", "DETECT", "CAPTURE"]

def gen(cam_idx):
    global cameras, last_save_time
    while True:
        try:
            if cameras[cam_idx] is None:
                try: 
                    cameras[cam_idx] = VideoCamera(indices[cam_idx], roles[cam_idx])
                except: pass
                time.sleep(0.5)
                continue
            
            # Get frame
            frame = cameras[cam_idx].get_frame_and_process(cam_idx + 1)
            
            # Global Timer Update for Capture Cams (Shared Logic)
            if roles[cam_idx] == "CAPTURE" and vehicle_status == "MOVING":
                # We check inside get_frame_and_process, but we update timer here 
                # to prevent double-saving if threads run perfectly parallel
                if time.time() - last_save_time > 1.0:
                    last_save_time = time.time()

            if frame: 
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
                time.sleep(0.066)
            else: 
                time.sleep(0.1)
        except Exception as e: 
            print(f"Cam {cam_idx} Error: {e}")
            time.sleep(0.1)

# --- FIREBASE BACKGROUND TASK ---
def firebase_monitor():
    global vehicle_status
    print("--- FIREBASE MONITOR STARTED ---")
    while True:
        try:
            # Poll Telemetry for Status
            r = requests.get(TELEMETRY_ENDPOINT)
            if r.status_code == 200:
                data = r.json()
                if data and "status" in data:
                    vehicle_status = data["status"]
                    # print(f"Status: {vehicle_status}") # Debug
            
            time.sleep(1.0) # Check every second
        except Exception as e:
            print(f"Firebase Monitor Error: {e}")
            time.sleep(2.0)

# --- ROUTES ---
@app.route('/')
def index():
    return """
    <html>
    <head><title>RAIV VISION SYSTEM</title></head>
    <body style="background:#111; color:cyan; font-family:sans-serif; text-align:center;">
        <h2>RAIV ADVANCED VISION & SAFETY</h2>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px;">
            <div><h3>CAM 1 (R-TRACK) [REC]</h3><img src="/video1" width="320"></div>
            <div><h3>CAM 2 (BACK) [AI-GUARD]</h3><img src="/video2" width="320"></div>
            <div><h3>CAM 3 (FRONT) [AI-GUARD]</h3><img src="/video3" width="320"></div>
            <div><h3>CAM 4 (L-TRACK) [REC]</h3><img src="/video4" width="320"></div>
        </div>
    </body>
    </html>
    """

@app.route('/video1')
def video_feed1(): return Response(gen(0), mimetype='multipart/x-mixed-replace; boundary=frame')
@app.route('/video2')
def video_feed2(): return Response(gen(1), mimetype='multipart/x-mixed-replace; boundary=frame')
@app.route('/video3')
def video_feed3(): return Response(gen(2), mimetype='multipart/x-mixed-replace; boundary=frame')
@app.route('/video4')
def video_feed4(): return Response(gen(3), mimetype='multipart/x-mixed-replace; boundary=frame')

# --- TUNNEL & STARTUP ---
def start_tunnel():
    print("--- STARTING CLOUD TUNNEL ---")
    cmd = ['cloudflared', 'tunnel', '--url', f'http://localhost:{PORT}']
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    for line in iter(process.stdout.readline, ''):
        match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
        if match:
            tunnel_url = match.group(0)
            print(f"\n✅ TUNNEL ESTABLISHED: {tunnel_url}")
            try: requests.put(URL_ENDPOINT, json=tunnel_url)
            except: pass
            break 

if __name__ == '__main__':
    # 1. Start Tunnel
    t_tunnel = threading.Thread(target=start_tunnel)
    t_tunnel.daemon = True
    t_tunnel.start()

    # 2. Start Firebase Monitor (For Status Checking)
    t_fb = threading.Thread(target=firebase_monitor)
    t_fb.daemon = True
    t_fb.start()

    print(f"--- SYSTEM ACTIVE ---")
    print(f"CAM 1/4: Recording to {SAVE_DIR}")
    print(f"CAM 2/3: Obstacle Detection Active")
    
    app.run(host='0.0.0.0', port=PORT, threaded=True)
