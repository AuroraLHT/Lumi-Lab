from web_camera import WebCamera, WebCameraConfig, list_devices as webcam_list_devices
import time
import datetime

import cv2

import logging
import matplotlib.pyplot as plt

logging.basicConfig(level=logging.INFO)

def frame_processing(frame, timestamp):
    # frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    # Put current DateTime on each frame 
    font = cv2.FONT_HERSHEY_PLAIN 
    frame = cv2.putText(frame, str(datetime.datetime.fromtimestamp(timestamp)), (20, 40), 
                font, 2, (255, 0, 0), 2, cv2.LINE_AA)     
    return frame

def _main():
    # Perform connection
    # devices = list_devices()
    # pylon_camera_config = PylonCameraConfig(device = devices[0] )
    web_camera_config = WebCameraConfig(
        fps=30
    )

    # pylon_camera = PylonCamera(device=devices[0], config=pylon_camera_config, ident=0)
    # video_compressor = VideoCompressor(camera_queue=pylon_camera.queue, config=video_compressor_config, frame_processing=lambda cv_frame: cv2.cvtColor(cv_frame, cv2.COLOR_GRAY2RGB))

    web_camera = WebCamera(config=web_camera_config, name="web_cam")

    web_camera.daemon = True
    web_camera.start()
    print("start web cam")

    time.sleep(1)
    print("start opencv ui")

    cv2.namedWindow("preview", cv2.WINDOW_NORMAL)
    while(True): 
        
        # Capture the video frame 
        # by frame 
        frame, frame_header = web_camera.get_frame()
        # print(f"get frame {frame.shape} {timestamp}")
        frame = frame_processing(frame, frame_header['timestamp'])
        # Display the resulting frame 
        # print(f"process frame {frame.shape}")
        cv2.imshow('preview', frame) 
        # print(f"Show frame")
        
        # the 'q' button is set as the 
        # quitting button you may use any 
        # desired button of your choice 
        if cv2.waitKey(1) & 0xFF == ord('q'): 
            break

    # Destroy all the windows 
    cv2.destroyAllWindows() 

    web_camera.stop()    
    web_camera.join()


if __name__ == "__main__":
    _main() 