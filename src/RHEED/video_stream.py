import cv2
import av
# import asyncio
# import websockets
import io
from fastapi import WebSocket
from starlette.websockets import WebSocketState
import time
import asyncio
import threading
import queue
from collections import deque
import fractions
import datetime
import logging

from dataclasses import dataclass

from typing import Union, List, Optional
from collections.abc import Callable, Awaitable


def extract_buffer(output_buffer):
    # Get the encoded data from the buffer
    output_buffer.seek(0)
    content = output_buffer.read()
    output_buffer.seek(0)
    # delete all the encoded data from the buffer to prevent memory overflow
    output_buffer.truncate(0)
    return content

@dataclass
class VideoCompressorConfig:
    fps : int = 30
    frames_per_keyframe : int = 10
    idle_time : float = None
    time_base : fractions.Fraction = fractions.Fraction(1, av.time_base)
    
    height : int = 480
    width : int = 640

    fragment_queue_size : int = 10
    cached_startup_fragments: int = None
    bit_rate : int = 3_000_000# Mbps

    def __post_init__(self):
        if self.cached_startup_fragments is None:
            self.cached_startup_fragments = int( 2*(self.fps / self.frames_per_keyframe) )

        if self.idle_time is None:
            self.idle_time = self.spf / 10

        print(self.cached_startup_fragments)

    @property
    def spf(self):
        return 1 / self.fps 


class VideoCompressor(threading.Thread):
    def __init__(self, camera, camera_queue:queue.Queue, config:VideoCompressorConfig, frame_processing:Callable=None, name:Union[int|str]=""):
        super().__init__(name=name)
        self.io_lock = threading.Lock()
        self.config = config
        self.startup_fragments = [] # for client that join in the middle of the streaming
        self.fragments = queue.Queue(maxsize=self.config.fragment_queue_size)
        # self.fragments = deque(maxlen=self.config.fragment_queue_size)
        self.camera = camera
        self.camera_queue = camera_queue
        self.frame_processing = frame_processing
        self._stop_event = threading.Event()


    def get_video_frame(self, start_time=None, prev_pts=None):
        # extract camera frame
        # print(f"is queue full {self.camera_queue.full()} ")
        cv_frame, current_time = self.camera_queue.get()
        # cv_frame, current_time = self.camera.get_frame()
        # the camera we have is monocolor version
        # convert to rgb from gray signal
        if self.frame_processing is not None:
            cv_frame = self.frame_processing(cv_frame, current_time)
            # cv_frame = cv2.cvtColor(cv_frame, cv2.COLOR_GRAY2RGB)


        frame = av.VideoFrame.from_ndarray(cv_frame, format='rgb24')
        
        # add time stamp info to the frame
        if start_time is None:
            start_time = current_time
            time_diff = 0
        else:
            time_diff = current_time - start_time

        pts = round(time_diff / self.config.time_base)
        if pts == prev_pts: pts+=1
        frame.pts = pts
        frame.dts = pts
        frame.time_base = self.config.time_base
        # print(frame.pts, frame.dts)

        return frame, start_time, current_time, pts


    def yield_video(self):
        logging.info("start send video")
        # test_writer = open("test.mp4", "wb")
        # print(self.config.cached_startup_fragments)

        # Initialize the container for writing to a buffer
        output_buffer = io.BytesIO()

        container = av.open(
            output_buffer, 
            mode='w', 
            format='mp4',
            # options={'movflags': 'frag_keyframe+empty_moov+default_base_moof'} # without the moov the video itself would be clueless
            options={'movflags': 'frag_keyframe+default_base_moof'} 
            # options={'movflags': 'frag_keyframe+empty_moov'} # this is not non chrome browser
            # see https://developer.mozilla.org/en-US/docs/Web/API/Media_Source_Extensions_API/Transcoding_assets_for_MSE
        )

        # Define the codec and create a video stream
        stream = container.add_stream('h264', rate=self.config.fps)
        # stream = container.add_stream('hevc', rate=self.config.fps)

        stream.width = self.config.width
        stream.height = self.config.height
        stream.bit_rate = self.config.bit_rate
        stream.pix_fmt = 'yuv420p'
        stream.codec_context.gop_size = self.config.frames_per_keyframe
        stream.time_base = self.config.time_base

        frame_idx = 0
        frame_bytes_idx = 0

        start_time = None
        prev_pts= None
        stop_flag = False

        _current_frag_frame_start_time = None
        _current_frag_frame_end_time = None

        while True:
            # Encode the frame and write it to the buffer
            av_frame, start_time, current_time, prev_pts = self.get_video_frame(start_time=start_time, prev_pts=prev_pts)
            if _current_frag_frame_start_time is None: _current_frag_frame_start_time = datetime.datetime.fromtimestamp(current_time)
            _current_frag_frame_end_time = datetime.datetime.fromtimestamp(current_time)

            for packet in stream.encode(av_frame):
                container.mux(packet)

                if packet.is_keyframe and frame_idx > 0:

                    frame_bytes = extract_buffer(output_buffer=output_buffer)

                    # logging.info(f"Video compressor: at frame {frame_idx}, at fragment {frame_bytes_idx}, create package: {len(frame_bytes)}")

                    # condition = frame_bytes_idx % 20 != 0 # simulate loss one packet
                    # condition = frame_bytes_idx < 4 or frame_bytes_idx > 20
                    condition = True
                    if frame_bytes_idx > 0 and condition:
                        # test_writer.write(frame_bytes)
                        # test_writer.flush()

                        content = frame_bytes, {"frame_start":str(_current_frag_frame_start_time), "frame_end":str(_current_frag_frame_end_time), "frag_idx":frame_bytes_idx}
                        if frame_bytes_idx < self.config.cached_startup_fragments:
                            self.startup_fragments.append(content)
                            logging.info("add startup fragment")

                        # logging.info(f"Fragment yield for frame from {_current_frag_frame_start_time} to {_current_frag_frame_end_time} current time {datetime.datetime.now()}")

                        # yield (frame_bytes, frame_bytes_idx)
                        yield content
                        _current_frag_frame_start_time = None
                        

                    frame_bytes_idx += 1

            frame_idx+=1

            stop_flag = self._stop_event.wait(self.config.idle_time/3)
            if stop_flag : 
                logging.info("exit thread while loop")
                break   

        # Finalize the container
        logging.info("finalization")

        for packet in stream.encode():
            container.mux(packet)

        frame_bytes = extract_buffer(output_buffer=output_buffer)

        logging.info('close container and video capture')
        container.close()
        logging.info("close resource")

        # if not stop_flag :
        if not stop_flag:
            logging.info("last yield")
            yield frame_bytes


    def run(self):
        for content in self.yield_video():
            self.fragments.put(content)
            # with self.io_lock:
            #     self.fragments.append(fragment)

    def clear(self):
        while not self.fragments.empty():
            self.fragments.get()
            # self.fragments.clear()
        self.startup_fragments = []
        
    def get_fragment(self):
        return self.fragments.get()
        # with self.io_lock:
        #     return 

    def get_history_fragment(self, i):
        # TODO: work caching and reloading
        # raise NotImplementedError
        if i < len(self.startup_fragments):
            return self.startup_fragments[i]
        else:
            raise NotImplementedError(f"i is {i}, but length is {len(self.startup_fragments)}")
        
    def number_of_startup_fragments(self):
        # return self.config.cached_startup_fragments
        return len(self.startup_fragments)

    def get_startup_fragments(self):
        return self.startup_fragments

    def has_fragment(self):
        return not self.fragments.empty()
        
    def stop(self):
        logging.info(f"Live thread receives a stop signal")

        # clear the queue
        self.clear()
        self._stop_event.set()

# start_server = websockets.serve(send_video, 'localhost', 8765)
# asyncio.get_event_loop().run_until_complete(start_server)
# asyncio.get_event_loop().run_forever()
