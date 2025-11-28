import cv2
from flask import Flask, Response, render_template_string
import threading
import time

app = Flask(__name__)

# --- CONFIGURATION ---
# Camera Indices (0 is usually built-in webcam, 1 and 2 are USB)
# Try changing these numbers if cameras are swapped or not found.
CAM_1_INDEX = 2 
CAM_2_INDEX = 1 

# --- CAMERA HANDLER ---
class VideoCamera(object):
    def __init__(self, index):
        self.video = cv2.VideoCapture(index, cv2.CAP_DSHOW) # CAP_DSHOW helps on Windows
        # Set Resolution to 640x480 for faster streaming over 4G
        self.video.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.video.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    
    def __del__(self):
        self.video.release()
    
    def get_frame(self):
        success, image = self.video.read()
        if not success:
            # Return a black frame if camera disconnects
            return None
        
        # Add a Timestamp or Text overlay
        cv2.putText(image, f"REC", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        ret, jpeg = cv2.imencode('.jpg', image)
        return jpeg.tobytes()

# Global Camera Objects
cam1 = None
cam2 = None

def gen(camera):
    while True:
        try:
            frame = camera.get_frame()
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            else:
                time.sleep(0.1)
        except:
            time.sleep(0.1)

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

@app.route('/')
def index():
    return "<h1>Rail Vehicle Camera Server Online</h1><p>Stream 1: <a href='/video1'>/video1</a></p><p>Stream 2: <a href='/video2'>/video2</a></p>"

if __name__ == '__main__':
    print("--- STARTING CAMERA SERVER ---")
    print("Access streams at http://127.0.0.1:5000/video1 and /video2")
    # Host on 0.0.0.0 to allow local network access if needed
    app.run(host='0.0.0.0', port=5000, threaded=True)
