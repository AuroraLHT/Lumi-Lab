import h5py
import numpy as np
from dataclasses import dataclass
from typing import Tuple, List, Dict
from pathlib import Path
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import uuid
import time
import asyncio

from typing import Union, List, Optional, Dict, Tuple, Awaitable

def resize_if_over(idx, dataset, resize_step=1000, resize_absolute=None, axis=0):
    if idx >= dataset.shape[axis]:
        if resize_absolute is None:
            resize_absolute = dataset.shape[axis]+resize_step
        dataset.resize(resize_absolute, axis=axis)        

@dataclass
class RecorderConfig:
    project_name : str
    root_folder : str

    frame_dim : Tuple[int]
    frame_meta_columns : List[str]

    log_columns : List[str]

    pattern_dim : Tuple[int]
    detection_meta_columns : List[str] 

    classifier_classes : List[str]

    initial_size : int
    
    save_frame : bool
    save_log : bool
    save_ai : bool

    force_rewrite : bool = False

    compression : Optional[str | None] = None # could be gzip LZT, and 
    compression_opts : Optional[int | None] = None  # 0-9, default value is 4

class Recorder:
    NAME_LOG_DS = "log"
   
    NAME_FRAME_DS = "frame"
    NAME_FRAME_META_DS = "frame_meta"

    NAME_PATTERN_DS = "pattern"
    NAME_CLASSIFICATION_DS = "classification"
    NAME_DETECTION_DS = "detection"
    NAME_INSTANCE_SEGMENTATION_DS = "instance_segmentation"
    NAME_NUM_DETECTION_DS = "num_detection"

    NAME_TRACK_DS = "tracking"
    NAME_NUM_TRACK_DS = "num_tracking"

    NAME_DETECTION_META_DS = "detection_meta"

    RESIZE_STEP = 1000
    
    def __init__(self, config:RecorderConfig):
        self.config = config
        self.open_h5(self.config.root_folder, self.config.project_name)

    def check_save(self, save_flag, flag_name):
        if not save_flag: raise Exception(f"save flag {flag_name} is not enabled")

    def open_h5(self, root_folder, project_name):
        logging.info("Try to open h5py database file at {}".format( (Path(root_folder) / f"{project_name}.h5py").absolute() ) )
        open_mode = "w" if self.config.force_rewrite else "a"
        self.h5f = h5py.File( Path(root_folder) / f"{project_name}.h5py", open_mode)

    def close_h5(self):
        self.h5f.flush()
        self.h5f.close()

    def create_dataset(self, ):

        # self.frame_meta_columns = frame_meta_columns
        # self.pattern_meta_columns = pattern_meta_columns
        # self.log_columns = log_columns
        # self.initial_size = initial_size

        if self.config.save_frame:

            img_h, img_w = self.config.frame_dim
            frame_dataset = self.h5f.create_dataset(
                self.NAME_FRAME_DS, 
                (self.config.initial_size, img_h, img_w), 
                maxshape=(None, img_h, img_w), 
                chunks=True, 
                dtype=np.uint16,
                compression=self.config.compression,
                compression_opts=self.config.compression_opts
            )
            frame_dataset.attrs['size'] = 0

            self.frame_meta_columns = ["time_stamp", "time"]        
            frame_meta_dataset = self.h5f.create_dataset(
                self.NAME_FRAME_META_DS, 
                (self.config.initial_size, len(self.frame_meta_columns) ), 
                maxshape=(None, len(self.frame_meta_columns) ), 
                chunks=True, 
                dtype=h5py.string_dtype(encoding='utf-8', length=None)
            )
            frame_meta_dataset.attrs['columns'] = self.frame_meta_columns
            frame_meta_dataset.attrs['size'] = 0

        if self.config.save_log:
            log_dataset = self.h5f.create_dataset(
                self.NAME_LOG_DS, 
                (self.config.initial_size, len(self.config.log_columns) ), 
                maxshape=(None, len(self.config.log_columns) ), 
                chunks=True, 
                dtype=h5py.string_dtype(encoding='utf-8', length=None)
            )

            log_dataset.attrs['columns'] = self.config.log_columns
            log_dataset.attrs['size'] = 0

        if self.config.save_ai:
            pattern_h, pattern_w = self.config.pattern_dim

            pattern_dataset = self.h5f.create_dataset(
                self.NAME_PATTERN_DS, 
                (self.config.initial_size, pattern_h, pattern_w), 
                maxshape=(None, pattern_h, pattern_w), 
                chunks=True,
                compression=self.config.compression,
                compression_opts=self.config.compression_opts
            )
            pattern_dataset.attrs['size'] = 0

            detection_meta_dataset = self.h5f.create_dataset(
                self.NAME_DETECTION_META_DS, 
                (self.config.initial_size, len(self.config.detection_meta_columns)), 
                maxshape=(None, len(self.config.detection_meta_columns)), 
                chunks=True, 
                dtype=h5py.string_dtype(encoding='utf-8', length=None)
            )
            detection_meta_dataset.attrs['size'] = 0
            detection_meta_dataset.attrs['columns'] = self.config.detection_meta_columns

            classification_dataset = self.h5f.create_dataset(
                self.NAME_CLASSIFICATION_DS, 
                (self.config.initial_size, len(self.config.classifier_classes)), 
                maxshape=(None, len(self.config.classifier_classes)), 
                chunks=True
            )
            classification_dataset.attrs['size'] = 0
            classification_dataset.attrs['class_name'] = self.config.classifier_classes

            n_init_detection = 2
            detection_dataset = self.h5f.create_dataset(
                self.NAME_DETECTION_DS, 
                (self.config.initial_size, n_init_detection, 6), 
                maxshape=(None, None, 6), 
                chunks=True
            )
            detection_dataset.attrs['size'] = 0
            detection_dataset.attrs['columns'] = ['sx', 'sy', 'ex', 'ey', 'label', 'score']

            instance_segmentation_dataset = self.h5f.create_dataset(
                self.NAME_INSTANCE_SEGMENTATION_DS, 
                (self.config.initial_size, n_init_detection, pattern_h, pattern_w), 
                maxshape=(None, None, pattern_h, pattern_w), 
                chunks=True, 
                dtype=bool, # bool is 1 bytes in C
                compression=self.config.compression,
                compression_opts=self.config.compression_opts
            )
            instance_segmentation_dataset.attrs['size'] = 0

            num_detection_dataset = self.h5f.create_dataset(
                self.NAME_NUM_DETECTION_DS, 
                (self.config.initial_size,), 
                maxshape=(None,), 
                chunks=True,
                dtype=np.int32
            )
            num_detection_dataset.attrs['size'] = 0


            num_tracking_dataset = self.h5f.create_dataset(
                self.NAME_NUM_TRACK_DS, 
                (self.config.initial_size,), 
                maxshape=(None,), 
                chunks=True,
                dtype=np.int32
            )
            num_tracking_dataset.attrs['size'] = 0


            n_init_tracking = 2
            tracking_dataset = self.h5f.create_dataset(
                self.NAME_TRACK_DS, 
                (self.config.initial_size, n_init_tracking), 
                maxshape=(None, None), 
                chunks=True,
                dtype=np.int32
            )
            tracking_dataset.attrs['size'] = 0


    # this is the log part
    @property
    def ds_log(self):
        return self.h5f[self.NAME_LOG_DS]

    # this is the camera part
    @property
    def ds_frame(self):
        return self.h5f[self.NAME_FRAME_DS]

    @property
    def ds_frame_meta(self):
        return self.h5f[self.NAME_FRAME_META_DS]

    # this is the detection part
    @property
    def ds_pattern(self):
        return self.h5f[self.NAME_PATTERN_DS]

    @property
    def ds_detection_meta(self):
        return self.h5f[self.NAME_DETECTION_META_DS]

    @property
    def ds_classification(self):
        return self.h5f[self.NAME_CLASSIFICATION_DS]

    @property
    def ds_detection(self):
        return self.h5f[self.NAME_DETECTION_DS]

    @property
    def ds_instance_segmentation(self):
        return self.h5f[self.NAME_INSTANCE_SEGMENTATION_DS]

    @property
    def ds_num_detection(self):
        return self.h5f[self.NAME_NUM_DETECTION_DS]

    @property
    def ds_num_tracking(self):
        return self.h5f[self.NAME_NUM_TRACK_DS]

    @property
    def ds_tracking(self):
        return self.h5f[self.NAME_TRACK_DS]


    def save_log(
            self, 
            chamber_log, 
            idx=None, 
            resize_step=None
        ):
        self.check_save(self.config.save_log, flag_name="save_log")

        if resize_step is None: resize_step = self.RESIZE_STEP
        _idx = self.ds_log.attrs['size'] if idx is None else idx

        resize_if_over(_idx, self.ds_log, resize_step=self.RESIZE_STEP)
        self.ds_log[_idx] = [ str(chamber_log[k]) for k in self.ds_log.attrs['columns'] ]

        if _idx is None: self.ds_log.attrs['size'] += 1


    def save_frame(
            self, 
            frame, 
            frame_headers, 
            idx=None, 
            resize_step=None
        ):
        self.check_save(self.save_frame, "save_frame")

        if resize_step is None: resize_step = self.RESIZE_STEP
        _idx = self.ds_frame.attrs['size'] if idx is None else idx

        resize_if_over(_idx, self.ds_frame, resize_step=resize_step)
        self.ds_frame[_idx] = frame if frame.ndim == 2 else frame[..., 0]

        resize_if_over(_idx, self.ds_frame_meta, resize_step=resize_step)
        self.ds_frame_meta[_idx] = [ str(frame_headers[k]) for k in self.ds_frame_meta.attrs['columns'] ]

        if _idx is None: self.ds_frame.attrs['size'] += 1
        if _idx is None: self.ds_frame_meta.attrs['size'] += 1


    def save_prediction(
        self,
        pattern,
        masks,
        bboxes,
        labels,
        scores,
        cls_result,
        tracking,
        detection_meta,        
        idx=None,
        resize_step=None
    ):
        self.check_save(self.config.save_ai, "save_ai")
        
        """
            should decompose the inst_result into multiple argument for better generalization
        """
        
        if resize_step is None: resize_step = self.RESIZE_STEP
        _idx = self.ds_pattern.attrs['size'] if idx is None else idx

        num_detection = len(masks)
        num_tracking = len(tracking)
        # num detection
        resize_if_over(_idx, self.ds_num_detection, resize_step, axis=0)
        self.ds_num_detection[_idx] = num_detection
        # pattern
        resize_if_over(_idx, self.ds_pattern, resize_step, axis=0)
        self.ds_pattern[_idx] = pattern
        # classification
        resize_if_over(_idx, self.ds_classification, resize_step, axis=0)
        if isinstance(cls_result, dict):
            cls_result = [ cls_result[k] for k in self.ds_classification.attrs['class_name'] ]
        self.ds_classification[_idx] = cls_result
        # instance segmentation
        resize_if_over(_idx, self.ds_instance_segmentation, resize_step, axis=0)
        resize_if_over(num_detection, self.ds_instance_segmentation, resize_absolute=num_detection, axis=1 )
        self.ds_instance_segmentation[_idx, :len(masks) ] = masks
        # detection
        resize_if_over(_idx, self.ds_detection, resize_step, axis=0)
        resize_if_over(num_detection, self.ds_detection, resize_absolute=num_detection, axis=1 )

        detections = np.concatenate( (bboxes, np.stack( (labels, scores), axis=1)), axis=1 )
        
        self.ds_detection[_idx, :len(detections) ] = detections

        resize_if_over(_idx, self.ds_detection_meta, resize_step=resize_step)
        self.ds_detection_meta[_idx] = [ str(detection_meta[k]) for k in self.ds_detection_meta.attrs['columns'] ]

        # tracking
        resize_if_over(_idx, self.ds_num_tracking, resize_step, axis=0)
        self.ds_num_tracking[_idx] = num_tracking

        resize_if_over(_idx, self.ds_tracking, resize_step, axis=0)
        resize_if_over(num_tracking, self.ds_tracking, resize_absolute=num_tracking, axis=1 )
        if isinstance(tracking, dict):
            # assume tracking is region -> track relationship
            _tracking = -np.ones(num_tracking)
            for k, v in tracking.items():
                _tracking[int(k)] = int(v)
        else:
            _tracking = tracking

        self.ds_tracking[_idx, :len(tracking) ] = _tracking

        if idx is None: self.ds_pattern.attrs['size'] += 1
        if idx is None: self.ds_detection_meta.attrs['size'] += 1
        if idx is None: self.ds_classification.attrs['size'] += 1
        if idx is None: self.ds_instance_segmentation.attrs['size'] += 1
        if idx is None: self.ds_detection.attrs['size'] += 1

        if idx is None: self.ds_tracking.attrs['size'] += 1
        if idx is None: self.ds_num_tracking.attrs['size'] += 1

@dataclass
class RecorderServerConfig:
    idle_time : 0.1

class RecorderServer(threading.Thread):
    recorder : Recorder
    executor : ThreadPoolExecutor
    futures : Dict[str, asyncio.Future]

    def __init__(
        self,
        config: RecorderServerConfig,
        recorder : Recorder,
        name : Union[int, str]        
    ):
        super().__init__(name=name)
        self.config =  config
        self.futures = {}
        self.executor = None
        self.recorder = recorder
        self._stop_event = threading.Event()

    def create_dataset(self):
        self.recorder.create_dataset()

    def close_storages(self):
        self.recorder.close_h5()
        self.stop()

    def save_prediction(self, *args, **kargs):
        future = self.executor.submit( self.recorder.save_prediction, *args, **kargs )
        self.futures[uuid.uuid4()] = future

    def save_frame(self, *args, **kargs):
        future = self.executor.submit( self.recorder.save_frame, *args, **kargs )
        self.futures[uuid.uuid4()] = future

    def save_log(self, *args, **kargs):
        future = self.executor.submit( self.recorder.save_log, *args, **kargs )
        self.futures[uuid.uuid4()] = future

    def clear_futures(self):
        self.futures = { idx : future for idx, future in self.futures.items() if not future.done() }

    def run(self):
        logging.info(f"Recorder server ({self.ident}) started")
        self.executor = ThreadPoolExecutor(max_workers=5)

        while True:
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : break
            self.clear_futures()

        logging.info(f"Recorder server ({self.ident}) run loop terminated, wait for #{len(self.futures)} jobs to finish")
        while len(self.futures):
            # wait until all job are done
            time.sleep(0.05)
            self.clear_futures()
        logging.info(f"Recorder server ({self.ident}) #{len(self.futures)} jobs left. termination complete.")

    def stop(self):
        logging.info(f"Recorder server ({self.ident}) receives a stop signal")
        self._stop_event.set()



class RecordAnalyzer:
    def __init__(self, recorder):
        self.recorder = recorder


    def get_log_columns(self):
        return self.recorder.ds_log.attrs['columns']

    def get_log_time(self):
        log_time = self.get_log("Time", np.datetime64)
        return log_time

    def get_log(self, column, column_transform=np.float64, idx=None):
        log_dataset = self.recorder.ds_log
        log_columns = self.get_log_columns
        dataset_size = self.recorder.ds_log.attrs['size']
        if idx is None: idx = slice(None, dataset_size)

        data = np.array(
            list(
                map(
                column_transform, 
                log_dataset[idx, log_columns.index(column)]
                )
            )
        )
        return data


    def find_deposition_window(self):
        """
            return start and end point of the deposition based on the log dataset record
        """
        laser_pulses = self.get_log("LaserPuls", int)
        deposition_windows = np.where(laser_pulses>0)[0]
        log_time = self.get_log_time()

        return log_time[deposition_windows]
    
    def get_log_idx_between(self, start_time, end_time):
        log_time = self.get_log_time()
        start_time = np.datetime64(start_time)
        end_time = np.datetime64(end_time)

        mask = np.logcial_and( log_time > start_time, log_time < end_time )
        return np.where(mask)[0]

