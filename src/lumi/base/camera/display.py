# This a server for local display with opencv backend

import cv2
import numpy as np
import threading

from dataclasses import dataclass, fields
import queue
import time

@dataclass
class DisplayConfig:
    title : str
    add_timestamp : bool

def add_timestamp(frame):
    timestamp = time.time()
    timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    frame_height, frame_width = frame.shape[:2]
    text_org = (int(frame_width*0.05), int(frame_height * 0.05))
    frame = cv2.putText(frame, timestamp_str, text_org, cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
    return frame

class DisplayServer(threading.Thread):
    """
    Display server for local display with opencv backend
    Args:
        config: DisplayConfig
        queue: queue.Queue
            - The queue is the queue of frames to be displayed
    """
    def __init__(self, config: DisplayConfig, queue: queue.Queue):
        self.config = config
        self.queue = queue
        super().__init__()

    def run(self):
        self.on_initiate(self)

        while True:
            # if self.queue.empty():
            #     time.sleep(0.5)
            #     continue

            frame = self.queue.get()
            # Display the resulting frame
            # frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if self.config.add_timestamp:
                frame = add_timestamp(frame)

            cv2.imshow(self.config.title, frame)
            if cv2.waitKey(1) == ord('q'):
                break

        ## When everything done, release the capture
        cv2.destroyWindow(self.config.title)

    def on_initiate(self):
        pass
