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
from typing import Union, List, Optional, Dict, Deque, Any
from collections.abc import Callable, Awaitable

import json
from .integrator import MultiBoxIntegrator

@dataclass
class STFTCalculatorConfig:
    idle_time: float = field(default=0.005, metadata={"unit": "s", "description": "Time to wait when no data is available"})
    output_queue_size: int = field(default=50, metadata={"description": "Maximum size of the output queue"})
    window_size: float = field(default=50, metadata={"unit": "s", "description": "Size of the sliding window for FFT calculation"})
    hop_size: float = field(default=1, metadata={"unit": "s", "description": "Step size between consecutive FFT calculations"})
    time_resolution: float = field(default=0.1, metadata={"unit": "s", "description": "Time resolution for resampling the signal before FFT"})
    frequency_min: float = field(default=0, metadata={"unit": "Hz", "description": "Minimum frequency for FFT calculation"})
    frequency_max: float = field(default=1, metadata={"unit": "Hz", "description": "Maximum frequency for FFT calculation"})
    
    def __post_init__(self):

        if self.idle_time is None:
            self.idle_time = self.spf / 10

    def get_window_length(self):
        return self.window_size // self.time_resolution


class STFTCalculator(threading.Thread):
    def __init__(self, integrator: "MultiBoxIntegrator", config:STFTCalculatorConfig, name:Union[int|str]=""):
        super().__init__(name=name)
        self.config = config
        self.output_queue = queue.Queue(maxsize=self.config.output_queue_size)
        self.integrator = integrator
        self._stop_event = threading.Event()

        self.registered_integrations : Dict[Union[int, str], Dict[str, Any]] = {}
        self.live_stft_cache : Dict[Union[int, str], Deque] = {}

        self.current_time = time.time()
        self._registered_integrations_lock = threading.Lock()

    def register_integration(self, bbox_id):
        with self._registered_integrations_lock:
            self.registered_integrations[bbox_id] = self.integrator.bboxes[bbox_id]
            self.live_stft_cache[bbox_id] = deque([], maxlen=1000)

    def remove_integration(self, bbox_id):
        with self._registered_integrations_lock:
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
        start_time = _time[0]
        _time = _time - start_time
        _signal = signal[mask]
        # _signal -= np.mean(_signal)

        # padding the signal using the first value to the window size
        # _signal = np.pad(_signal, (self.config.get_window_length() - len(_signal), 0), mode='edge')
        # maybe the interpolation would handle the padding

        # resample the signal to match the time resolution
        # resampled_time = np.arange(0, self.config.window_size, self.config.time_resolution)
        # end_window = min(self.config.window_size, _time[-1])
        # start_window = end_window - self.config.window_size

        start_window, end_window = _time[0], _time[-1]
        # print(_time)
        # print(start_window, end_window, "len", len(_time))
        resampled_time = np.arange(start_window, end_window, self.config.time_resolution)

        # this clip the signal timestamp such that the padding timestamp would be the same as the actual starting timestamp
        # resampled_time = resampled_time.clip(0, None)

        # print(resampled_time)
        # print(len(resampled_time))
        # this is to avoid the out of range error
        resampled_signal = np.interp(resampled_time, _time, _signal, left=_signal[0], right=_signal[-1])
        resampled_time += start_time # add back the start time
        # compute the fft
        signal_size = int(self.config.window_size / self.config.time_resolution)
        if len(resampled_signal) < signal_size:
            # resampled_signal = np.pad(resampled_signal, (0, signal_size - len(resampled_signal)), mode='constant', constant_values=0)
            _resampled_signal = np.pad(resampled_signal, (0, signal_size - len(resampled_signal)), mode='wrap')

        # print("resampled_signal", len(resampled_signal), "signal_size", signal_size)


        fft = np.fft.rfft(_resampled_signal)
        fft_freq = np.fft.rfftfreq(len(_resampled_signal), self.config.time_resolution)

        # print("fft_freq", len(fft_freq), "fft", len(fft))

        mask = fft_freq > 0
        fft_freq = fft_freq[mask]
        fft = fft[mask]
        # fft_freq = fft_freq[fft_freq>=0]
        
        # print(resampled_time[0])
        # print(resampled_time[-1])
        return fft_freq, fft, resampled_time, resampled_signal

    def truncate_fft(self,fft_freq, fft):
        mask = (fft_freq >= self.config.frequency_min) & (fft_freq <= self.config.frequency_max)
        fft = fft[mask]
        fft_freq = fft_freq[mask]
        return fft_freq, fft

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
                with self._registered_integrations_lock:
                    # print("registered stft box", self.registered_integrations)
                    for bbox_id, bbox in self.registered_integrations.items():
                        if bbox_id not in self.integrator.bboxes:
                            bbox_to_remove.put(bbox_id)
                            continue

                        # t_before = time.time()
                        integration_time, intergration, latest_header = self.integrator.get_integration_history(bbox_id)
                        if integration_time is None or intergration is None or latest_header is None:
                            logging.warning(f"No integration history found for bbox {bbox_id}")
                            continue
                        # t_after = time.time()
                        # print("get_history_time: ", t_after - t_before)

                        # t_before_computed = time.time()
                        try:
                            fft_freq, fft, resampled_time, resampled_signal = self.compute_fft(signal=intergration, signal_time=integration_time)
                            fft_freq, fft = self.truncate_fft(fft_freq, fft)

                            # t_after_computed = time.time()
                            # print(f"fft computed within the time of {t_after_computed - t_before_computed}")
                            
                            self.live_stft_cache[bbox_id].append( (fft_freq, fft)  )

                            # t_b = time.time()
                            content = self.package_fft_result( fft_freq, fft, resampled_time, resampled_signal )
                            # t_a = time.time()
                            # print("package result time", t_b - t_a)

                            stfts[bbox_id] = content


                        except Exception as e:
                            logging.error(f"Error computing FFT for bbox {bbox_id}: {e}", exc_info=True)
                            continue
                    if stfts:
                        content = self.prepare_content(stfts, {"stft_uuid": str(uuid.uuid4())})
                        # print("content", content)
                        yield content
                            
                while not bbox_to_remove.empty():
                    bbox_id = bbox_to_remove.get()
                    # the remove function use lock too, donnot put it in a lock env
                    self.remove_integration(bbox_id)

            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : 
                logging.info(f"Thread[{self.name}] exit thread while loop")
                break   

        # Finalize the container
        logging.info(f"Thread[{self.name}] close live fft yield")

    def run(self):
        # prev_yield_time = time.time()
        for content in self.yield_content():
            # yield_time = time.time()
            # print(f"yield content with interval: {yield_time - prev_yield_time}")

            # before_put_time = time.time()
            self.output_queue.put(content)
            # after_put_time = time.time()
            # print(f"sample put to the queue with waittime of {after_put_time - before_put_time}")
            # prev_yield_time = time.time()

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