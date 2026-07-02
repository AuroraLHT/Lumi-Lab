# This a server for local display with opencv backend

from typing import Union

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

def add_timestamp(frame, frame_header):
    timestamp = frame_header["time_stamp"] if "time_stamp" in frame_header else None
    frame_height, frame_width = frame.shape[:2]
    text_org = (int(frame_width*0.05), int(frame_height * 0.1))
    if timestamp is not None:
        font = cv2.FONT_HERSHEY_PLAIN
        frame = cv2.putText(
            frame,
            timestamp,
            text_org,
            font,
            1.5,
            (255, 0, 0),
            2,
            cv2.LINE_AA,
        )
    return frame


class DisplayServer(threading.Thread):
    """
    Display server for local display with opencv backend
    Args:
        config: DisplayConfig
        queue: queue.Queue
            - The queue is the queue of frames to be displayed
    """
    def __init__(self, config: DisplayConfig, queue: queue.Queue, name:Union[int|str]="", daemon : bool = True):
        super().__init__(name=name, daemon=daemon)
        self.config = config
        self.queue = queue

    def run(self):
        self.on_initiate()

        while True:
            # if self.queue.empty():
            #     time.sleep(0.5)
            #     continue

            frame, frame_header = self.queue.get()
            # Display the resulting frame
            if self.config.add_timestamp:
                frame = add_timestamp(frame, frame_header)
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            cv2.imshow(self.config.title, frame)
            if cv2.waitKey(1) == ord('q'):
                break

        ## When everything done, release the capture
        cv2.destroyWindow(self.config.title)

    def on_initiate(self):
        pass
