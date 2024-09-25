import datetime
import time
import asyncio
import threading
import queue
from collections import deque
import logging
import uuid

import numpy as np

from dataclasses import dataclass, field

from typing import Union, List, Optional, Dict, Deque
from collections.abc import Callable, Awaitable

import json

@dataclass
class STFTCalculatorConfig:
    idle_time: float = field(default=None, metadata={"unit": "s", "description": "Time to wait when no data is available"})
    output_queue_size: int = field(default=50, metadata={"description": "Maximum size of the output queue"})
    window_size: float = field(default=50, metadata={"unit": "s", "description": "Size of the sliding window for FFT calculation"})
    hop_size: float = field(default=1, metadata={"unit": "s", "description": "Step size between consecutive FFT calculations"})
    time_resolution: float = field(default=0.1, metadata={"unit": "s", "description": "Time resolution for resampling the signal before FFT"})
    
    def __post_init__(self):

        if self.idle_time is None:
            self.idle_time = self.spf / 10

    def get_window_length(self):
        return self.window_size // self.time_resolution


class STFTCalculator(threading.Thread):
    def __init__(self, integrator, config:STFTCalculatorConfig, name:Union[int|str]=""):
        super().__init__(name=name)
        self.io_lock = threading.Lock()
        self.config = config
        self.output_queue = queue.Queue(maxsize=self.config.output_queue_size)
        self.integrator = integrator
        self._stop_event = threading.Event()

        self.registered_integrations = {}
        self.live_stft_cache : Dict[Union[int, str], Deque] = {}

        self.current_time = time.time()

    def register_integration(self, bbox_id):
        self.registered_integrations[bbox_id] = self.integrator.bboxes[bbox_id]
        self.live_stft_cache[bbox_id] = deque([], maxlen=1000)

    def remove_integration(self, bbox_id):
        if bbox_id in self.registered_integrations:
            self.registered_integrations.pop(bbox_id)
        if bbox_id in self.live_stft_cache:
            self.live_stft_cache.pop(bbox_id)

    def is_next_integration_ready(self):
        prev_time = self.current_time
        current_time = time.time()

        if current_time - prev_time > self.config.hop_size:
            # print("current time: ", current_time, "prev time: ", prev_time)
            # print(f"hop size {current_time - prev_time} > {self.config.hop_size}")
            self.current_time = current_time
            return True
        else:
            return False

    def compute_fft(self, signal, signal_time):
        """
        """

        assert len(signal) > 0
        mask = signal_time > signal_time[-1] - self.config.window_size

        _time = signal_time[mask]
        _time = _time - _time[0]
        _signal = signal[mask]

        # padding the signal using the first value to the window size
        # _signal = np.pad(_signal, (self.config.get_window_length() - len(_signal), 0), mode='edge')
        # maybe the interpolation would handle the padding

        # resample the signal to match the time resolution
        # resampled_time = np.arange(0, self.config.window_size, self.config.time_resolution)
        end_window = min(self.config.window_size, _time[-1])
        start_window = end_window - self.config.window_size
        # print(_time)
        # print(start_window, end_window)
        resampled_time = np.arange(start_window, end_window, self.config.time_resolution)

        # print(resampled_time)
        # print(len(resampled_time))
        # this is to avoid the out of range error
        resampled_signal = np.interp(resampled_time, _time, _signal, left=_signal[0], right=_signal[-1])
        resampled_time += _time[0] # add back the start time
        # compute the fft
        fft = np.fft.rfft(resampled_signal)
        fft_freq = np.fft.rfftfreq(len(resampled_signal), self.config.time_resolution)

        mask = fft_freq > 0
        fft_freq = fft_freq[mask]
        fft = fft[mask]
        # fft_freq = fft_freq[fft_freq>=0]
        
        return fft_freq, fft, resampled_time, resampled_signal


    def package_fft_result(self, fft_freq, fft, resampled_time, resampled_signal):
        result = {
            "fft_freq": fft_freq.tolist(),
            "fft_mag": np.abs(fft).tolist(),
            "fft_phase": np.angle(fft).tolist(),
            "time_end" : resampled_time[-1],
            "timestamp_end" : datetime.datetime.fromtimestamp(resampled_time[-1]).isoformat(),
            "time_start" : resampled_time[0],
            "timestamp_start" : datetime.datetime.fromtimestamp(resampled_time[0]).isoformat(),
            "time_resolution" : self.config.time_resolution,
        }
        return result
    
    def prepare_content(self, result, header):
        return (result, header)

    def yield_content(self):
        logging.info(f"Thread[{self.name}] start live fft")

        prev_frame_uuid = None
        bbox_to_remove = queue.Queue()

        while True:
            # Encode the frame and write it to the buffer
            if self.integrator.is_updated(prev_frame_uuid) and self.is_next_integration_ready():
                # print("fft triggered")
                prev_frame_uuid = self.integrator.get_processed_frame_uuid()

                stfts = {} # Not sure if this is the best way to do it
                # Need to know the different between sending each individual bbox data or sending them all at once
                for bbox_id, bbox in self.registered_integrations.items():
                    if bbox_id not in self.integrator.bboxes:
                        bbox_to_remove.put(bbox_id)
                        continue
                    integration_time, intergration, latest_header = self.integrator.get_integration_history(bbox_id)
                    fft_freq, fft, resampled_time, resampled_signal = self.compute_fft(signal=intergration, signal_time=integration_time)
                    
                    self.live_stft_cache[bbox_id].append( (fft_freq, fft)  )
                    content = self.package_fft_result( fft_freq, fft, resampled_time, resampled_signal )
                    stfts[bbox_id] = content

                content = self.prepare_content(stfts, {"stft_uuid": str(uuid.uuid4())})
                yield content
                        
                while not bbox_to_remove.empty():
                    bbox_id = bbox_to_remove.get()
                    self.remove_integration(bbox_id)

            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : 
                logging.info(f"Thread[{self.name}] exit thread while loop")
                break   

        # Finalize the container
        logging.info(f"Thread[{self.name}] close live fft yield")

    def run(self):
        for content in self.yield_content():
            self.output_queue.put(content)

    def clear(self):
        while not self.output_queue.empty():
            self.output_queue.get()
        self.registered_integrations = {}
        self.live_stft_cache = {}
        
        
    def stop(self):
        logging.info(f"Thread[{self.name}] Live FFT thread receives a stop signal")

        # clear the queue
        self.clear()
        self._stop_event.set()

    def get_cache(self, bbox_id):
        if bbox_id in self.live_stft_cache:
            return self.live_stft_cache[bbox_id]
        else:
            return None