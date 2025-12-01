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
# Camera Indices (Adjust these if cameras are swapped)
CAM_1_INDEX = 2
CAM_2_INDEX = 1
CAM_3_INDEX = 0 
CAM_4_INDEX = 3 

PORT = 5000

# FIREBASE URL
FIREBASE_URL = "https://mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app/cam_url.json"

app = Flask(__name__)

# --- CAMERA HANDLING ---
class VideoCamera(object):
    def __init__(self, index):
        self.video = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not self.video.isOpened():
            print(f"Warning: Camera {index} could not be opened.")
        
        # Resolution 640x480
        self.video.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.video.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    
    def __del__(self): 
        if self.video.isOpened(): self.video.release()
    
    def get_frame(self, cam_id):
        if not self.video.isOpened(): return None
        success, image = self.video.read()
        if not success: return None
        
        # Add "REC" text
        cv2.putText(image, f"CAM {cam_id} REC", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        ret, jpeg = cv2.imencode('.jpg', image)
        return jpeg.tobytes()

# Global Objects
cameras = [None, None, None, None] # Index 0->Cam1, 1->Cam2...
indices = [CAM_1_INDEX, CAM_2_INDEX, CAM_3_INDEX, CAM_4_INDEX]

def gen(cam_idx):
    global cameras
    while True:
        try:
            if cameras[cam_idx] is None:
                try: cameras[cam_idx] = VideoCamera(indices[cam_idx])
                except: pass
                time.sleep(1)
                continue
            
            frame = cameras[cam_idx].get_frame(cam_idx + 1)
            if frame: 
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            else: 
                time.sleep(0.1)
        except: time.sleep(0.1)

# --- ROUTES ---
@app.route('/')
def index():
    return "<h1>RAIV 4-CAM SERVER ONLINE</h1>"

@app.route('/video1')
def video_feed1(): return Response(gen(0), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video2')
def video_feed2(): return Response(gen(1), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video3')
def video_feed3(): return Response(gen(2), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video4')
def video_feed4(): return Response(gen(3), mimetype='multipart/x-mixed-replace; boundary=frame')

# --- AUTOMATION LOGIC ---
def start_tunnel():
    print("--- STARTING CLOUD TUNNEL ---")
    cmd = ['cloudflared', 'tunnel', '--url', f'http://localhost:{PORT}']
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    for line in iter(process.stdout.readline, ''):
        match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
        if match:
            tunnel_url = match.group(0)
            print(f"\n✅ TUNNEL FOUND: {tunnel_url}")
            upload_to_firebase(tunnel_url)
            break 

def upload_to_firebase(url):
    print(f"--- UPLOADING TO FIREBASE ---")
    try:
        requests.put(FIREBASE_URL, json=url)
        print("✅ SUCCESS! Link sent to Website.")
    except Exception as e:
        print(f"❌ ERROR: {e}")

# --- MAIN START ---
if __name__ == '__main__':
    t = threading.Thread(target=start_tunnel)
    t.daemon = True
    t.start()

    print(f"--- STARTING 4 CAMERAS ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
