import cv2
from flask import Flask, Response
import threading
import time
import subprocess
import re
import requests
import sys
import os

# --- CONFIGURATION ---
# Camera Indices (Based on your testing: 2 and 1)
CAM_1_INDEX = 2
CAM_2_INDEX = 1
PORT = 5000

# FIREBASE URL (Specific path to store the camera link)
# Note: .json at the end is required for the REST API
FIREBASE_URL = "https://mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app/cam_url.json"

app = Flask(__name__)

# --- CAMERA HANDLING ---
class VideoCamera(object):
    def __init__(self, index):
        # Try to open camera with DirectShow (faster on Windows)
        self.video = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not self.video.isOpened():
            print(f"Warning: Camera {index} could not be opened.")
        
        # Lower resolution for smoother streaming over 4G mobile data
        self.video.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.video.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    
    def __del__(self): 
        if self.video.isOpened(): self.video.release()
    
    def get_frame(self):
        if not self.video.isOpened(): return None
        success, image = self.video.read()
        if not success: return None
        
        # Add "REC" text overlay
        cv2.putText(image, "REC", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        # Encode to JPEG
        ret, jpeg = cv2.imencode('.jpg', image)
        return jpeg.tobytes()

# Global camera objects
cam1 = None
cam2 = None

def gen(camera):
    while True:
        try:
            if camera is None: 
                time.sleep(1)
                continue
            frame = camera.get_frame()
            if frame: 
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            else: 
                time.sleep(0.1)
        except: time.sleep(0.1)

@app.route('/video1')
def video_feed1():
    global cam1
    if cam1 is None: cam1 = VideoCamera(CAM_1_INDEX)
    return Response(gen(cam1), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video2')
def video_feed2():
    global cam2
    if cam2 is None: cam2 = VideoCamera(CAM_2_INDEX)
    return Response(gen(cam2), mimetype='multipart/x-mixed-replace; boundary=frame')

# --- AUTOMATION LOGIC ---
def start_tunnel():
    print("--- STARTING CLOUD TUNNEL ---")
    
    # Run Cloudflare Command
    # It looks for 'cloudflared.exe' in the current folder
    cmd = ['cloudflared', 'tunnel', '--url', f'http://localhost:{PORT}']
    
    # Start process and capture output
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    tunnel_url = ""
    
    print("Searching for public link...")
    
    # Scan the output line by line for the .trycloudflare.com link
    for line in iter(process.stdout.readline, ''):
        # print(line.strip()) # Uncomment this if you need to debug Cloudflare output
        
        # Regex to find the URL
        match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
        if match:
            tunnel_url = match.group(0)
            print(f"\n✅ TUNNEL FOUND: {tunnel_url}")
            upload_to_firebase(tunnel_url)
            # We don't break the loop so the tunnel process keeps running
            # We just stop looking for the link
            break 

def upload_to_firebase(url):
    print(f"--- UPLOADING TO FIREBASE ---")
    try:
        # Send the link to the cloud using a PUT request
        # This replaces the existing link in the database
        response = requests.put(FIREBASE_URL, json=url)
        
        if response.status_code == 200:
            print("✅ SUCCESS! Link sent to Website.")
            print("You can now open the dashboard on your phone.")
        else:
            print(f"❌ UPLOAD FAILED: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"❌ ERROR connecting to Firebase: {e}")

# --- MAIN START ---
if __name__ == '__main__':
    # 1. Start the Tunnel Thread (Runs in background)
    t = threading.Thread(target=start_tunnel)
    t.daemon = True
    t.start()

    # 2. Start the Camera Server (Runs in main thread)
    print(f"--- STARTING CAMERAS ON PORTS {CAM_1_INDEX} & {CAM_2_INDEX} ---")
    
    # threaded=True allows multiple viewers (phone + laptop browser etc)
    app.run(host='0.0.0.0', port=PORT, threaded=True)
