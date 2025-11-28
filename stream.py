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
CAM_1_INDEX = 2
CAM_2_INDEX = 1
PORT = 5000

# FIREBASE CONFIG (Paste your specific URL here)
# Format: https://your-project-default-rtdb.asia-southeast1.firebasedatabase.app/cam_url.json
FIREBASE_URL = "https://mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app/cam_url.json"

app = Flask(__name__)

# --- CAMERA HANDLING ---
class VideoCamera(object):
    def __init__(self, index):
        self.video = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        self.video.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.video.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    def __del__(self): self.video.release()
    def get_frame(self):
        success, image = self.video.read()
        if not success: return None
        cv2.putText(image, "REC", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        ret, jpeg = cv2.imencode('.jpg', image)
        return jpeg.tobytes()

cam1 = None
cam2 = None

def gen(camera):
    while True:
        try:
            frame = camera.get_frame()
            if frame: yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            else: time.sleep(0.1)
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

# --- CLOUDFLARE AUTOMATION ---
def start_tunnel():
    print("--- STARTING CLOUD TUNNEL ---")
    # Start Cloudflared in background and read its output
    # Ensure 'cloudflared.exe' is in the same folder!
    process = subprocess.Popen(['cloudflared', 'tunnel', '--url', f'http://localhost:{PORT}'], 
                               stdout=subprocess.PIPE, 
                               stderr=subprocess.STDOUT,
                               text=True,
                               bufsize=1)
    
    tunnel_url = ""
    
    # Read output line by line to find the .trycloudflare.com link
    for line in iter(process.stdout.readline, ''):
        print(f"[Cloudflare]: {line.strip()}")
        match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
        if match:
            tunnel_url = match.group(0)
            print(f"\n✅ TUNNEL FOUND: {tunnel_url}")
            upload_to_firebase(tunnel_url)
            break

def upload_to_firebase(url):
    print(f"--- UPLOADING TO FIREBASE ---")
    try:
        # We send a PUT request to update the 'cam_url' key
        response = requests.put(FIREBASE_URL, json=url)
        if response.status_code == 200:
            print("✅ LINK SYNCED SUCCESSFULLY!")
            print("Web Dashboard should update automatically.")
        else:
            print(f"❌ UPLOAD FAILED: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ ERROR: {e}")

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    # 1. Start Tunnel in a separate thread
    t = threading.Thread(target=start_tunnel)
    t.daemon = True
    t.start()

    # 2. Start Video Server
    print("--- STARTING CAMERAS ---")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
