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
# Standard Indices for 4 Cameras
CAM_1_INDEX = 0
CAM_2_INDEX = 1
CAM_3_INDEX = 2 
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
        
        # REDUCED RESOLUTION FOR BANDWIDTH (Fixes 4-Cam Crash)
        self.video.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        self.video.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        # Try to set FPS on hardware level
        self.video.set(cv2.CAP_PROP_FPS, 15)
    
    def __del__(self): 
        if self.video.isOpened(): self.video.release()
    
    def get_frame(self, cam_id):
        if not self.video.isOpened(): return None
        success, image = self.video.read()
        if not success: return None
        
        # Add "REC" text
        cv2.putText(image, f"CAM {cam_id}", (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        
        # HIGH COMPRESSION (Fixes Lag)
        # Quality = 30 (Lower quality, much faster stream)
        ret, jpeg = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
        return jpeg.tobytes()

# Global Objects
cameras = [None, None, None, None] 
indices = [CAM_1_INDEX, CAM_2_INDEX, CAM_3_INDEX, CAM_4_INDEX]

def gen(cam_idx):
    global cameras
    while True:
        try:
            # Lazy Initialization (Only start camera if someone is watching)
            if cameras[cam_idx] is None:
                try: cameras[cam_idx] = VideoCamera(indices[cam_idx])
                except: pass
                time.sleep(0.5)
                continue
            
            frame = cameras[cam_idx].get_frame(cam_idx + 1)
            if frame: 
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
                
                # Software FPS Cap (Fixes Lag)
                # 0.066 = ~15 FPS. Gives the 4G network time to breathe.
                time.sleep(0.066)
            else: 
                time.sleep(0.1)
        except: time.sleep(0.1)

# --- ROUTES ---
@app.route('/')
def index():
    return """
    <html>
    <head><title>RAIV 4-CAM SERVER</title></head>
    <body style="background:black; color:cyan; font-family:monospace; text-align:center; padding-top:50px;">
        <h1>RAIV 4-CAMERA SYSTEM (LOW LATENCY)</h1>
        <p>Status: ACTIVE</p>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px; max-width:800px; margin:30px auto;">
            <div style="border:1px solid cyan; padding:20px;">
                <h3>CAM 1</h3> <a href="/video1" style="color:yellow;">[ VIEW ]</a>
            </div>
            <div style="border:1px solid cyan; padding:20px;">
                <h3>CAM 2</h3> <a href="/video2" style="color:yellow;">[ VIEW ]</a>
            </div>
            <div style="border:1px solid cyan; padding:20px;">
                <h3>CAM 3</h3> <a href="/video3" style="color:yellow;">[ VIEW ]</a>
            </div>
            <div style="border:1px solid cyan; padding:20px;">
                <h3>CAM 4</h3> <a href="/video4" style="color:yellow;">[ VIEW ]</a>
            </div>
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

    print(f"--- STARTING 4 CAMERAS (OPTIMIZED) ---")
    # threaded=True is vital for multiple streams
    app.run(host='0.0.0.0', port=PORT, threaded=True)
