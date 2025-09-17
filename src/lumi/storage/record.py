import h5py
from lumi.pascal.chamber_log import TYPE_CONVERSIONS
from lumi.utils.error import get_error_info

import numpy as np
import pandas as pd

from dataclasses import dataclass, field
from typing import Any, Generator, Tuple, List, Dict
from pathlib import Path
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import uuid
import time
import asyncio
import logging

from lumi.config import settings
from lumi.rheed.types import IntegrationCollection, IntegrationResult
from typing import Union, List, Optional, Dict, Tuple, Awaitable


def resize_if_over(idx, dataset, resize_step=1000, resize_absolute=None, axis=0):
    if idx >= dataset.shape[axis]:
        if resize_absolute is None:
            # resize_absolute = dataset.shape[axis]+resize_step
            resize_absolute = idx+resize_step
        dataset.resize(resize_absolute, axis=axis)


def register_dataset(registry: List[str], dataset_type: str):
    """
    Decorator to register dataset creation methods into a class registry.
    
    Args:
        dataset_type: The type of dataset (e.g., "frame", "log", "ai")
    """
    def decorator(func):
        # Register the method into the class's dataset_creators list
        
        registry.append({
            'type': dataset_type,
            'method': func,
            'config_flag': f'save_{dataset_type}'
        })
        return func
    return decorator


@dataclass
class RecorderConfig:
    project_name : str
    root_folder : str

    frame_dim : Tuple[int]
    frame_meta_columns : List[str]

    log_columns : List[str]

    pattern_dim : Tuple[int]
    detection_meta_columns : List[str] 

    detector_classes : List[str]
    classifier_classes : List[str]

    initial_size : int
    
    save_frame : bool
    save_log : bool
    save_ai : bool
    save_integration : bool

    frame_speed_limit : Optional[float] = field(
        default=None,
        metadata={"help": "Maximum frame rate to save in fps"}
    )
    
    detection_speed_limit : Optional[float] = field(
        default=None,
        metadata={"help": "Maximum detection rate to save in fps"}
    )

    force_rewrite : bool = False

    compression: Optional[str] = field(
        default=None,
        metadata={"help": "Compression filter to use. Options: gzip, lzf, etc."}
    )
    
    compression_opts: Optional[int] = field(
        default=None,
        metadata={"help": "Compression settings (0-9, default is 4)"}
    )
    
    scaleoffset: Optional[int] = field(
        default=None,
        metadata={"help": "Integer value for lossless-lossy integer or float compression. Use 0 for integer lossless compression"}
    )


class SpeedLimiter:
    def __init__(self, max_speed: float):
        self.max_speed = max_speed
        self.last_time = time.time()

    def throttle(self):
        """
        return True if the current time is greater than the last time plus the time interval
        """
        current_time = time.time()
        time_diff = current_time - self.last_time
        if time_diff < 1 / self.max_speed:
            return False
        else:
            self.last_time = time.time()
            return True


class RecordDataset:
    """
    Based RecordDataset structure.
    """
    h5f : h5py.File

    # this is the log part
    @property
    def ds_log(self):
        # return self.h5f[self.NAME_LOG_DS]
        return self.h5f[settings.storage.databases.log]
    
    # this is the camera part
    @property
    def ds_frame(self):
        # return self.h5f[self.NAME_FRAME_DS]
        return self.h5f[settings.storage.databases.frame]
        
    @property
    def ds_frame_meta(self):
        # return self.h5f[self.NAME_FRAME_META_DS]
        return self.h5f[settings.storage.databases.frame_meta]

    # this is the detection part
    @property
    def ds_pattern(self):
        # return self.h5f[self.NAME_PATTERN_DS]
        return self.h5f[settings.storage.databases.pattern]
    
    @property
    def ds_detection_meta(self):
        # return self.h5f[self.NAME_DETECTION_META_DS]
        return self.h5f[settings.storage.databases.detection_meta]

    @property
    def ds_classification(self):
        # return self.h5f[self.NAME_CLASSIFICATION_DS]
        return self.h5f[settings.storage.databases.classification]

    @property
    def ds_detection(self):
        # return self.h5f[self.NAME_DETECTION_DS]
        return self.h5f[settings.storage.databases.detection]

    @property
    def ds_instance_segmentation(self):
        # return self.h5f[self.NAME_INSTANCE_SEGMENTATION_DS]
        return self.h5f[settings.storage.databases.instance_segmentation]

    @property
    def ds_num_detection(self):
        # return self.h5f[self.NAME_NUM_DETECTION_DS]
        return self.h5f[settings.storage.databases.num_detection]

    @property
    def ds_num_tracking(self):
        # return self.h5f[self.NAME_NUM_TRACK_DS]
        return self.h5f[settings.storage.databases.num_tracking]

    @property
    def ds_tracking(self):
        # return self.h5f[self.NAME_TRACK_DS]
        return self.h5f[settings.storage.databases.tracking]

    # this is the integration part
    @property
    def ds_integration(self):
        return self.h5f[settings.storage.databases.integration]
    
    @property
    def ds_integration_root(self):
        return self.h5f[settings.storage.databases.integration_root]
    
    @property
    def ds_integration_meta(self):
        return self.h5f[settings.storage.databases.integration_meta]


class Recorder(RecordDataset):

    RESIZE_STEP = 1000
    frame_speed_limiter : Optional[SpeedLimiter] = None
    detection_speed_limiter : Optional[SpeedLimiter] = None
    dataset_registry : List[Dict[str, Any]] = []
    
    def __init__(self, config:RecorderConfig):
        self.config = config
        self.open_h5(self.config.root_folder, self.config.project_name)
        self._init_idx()
        self.frame_speed_limiter = SpeedLimiter(self.config.frame_speed_limit) if self.config.frame_speed_limit else None
        self.detection_speed_limiter = SpeedLimiter(self.config.detection_speed_limit) if self.config.detection_speed_limit else None

        self._buffer_integrations = {"content": [], "meta": [], "bbox_idx_start": [], "bbox_idx_end": [], "integration_idx": []}
        self._lock_buffer_integrations = threading.Lock()

    def check_save(self, save_flag, flag_name):
        if not save_flag: 
            logging.warning(f"save flag {flag_name} is not enabled")
            raise Exception(f"save flag {flag_name} is not enabled")

    def open_h5(self, root_folder, project_name):
        h5_path = Path(root_folder) / f"{project_name}.hdf5"
        logging.info("Try to open h5py database file at {}".format( h5_path.absolute() ) )

        if h5_path.exists() and not self.config.force_rewrite:
            logging.info(f"h5py database file {h5_path} already exists, set force_rewrite to True to rewrite")
            raise FileExistsError(f"h5py database file {h5_path} already exists, set force_rewrite to True to rewrite")

        open_mode = "w"
        self.h5f = h5py.File( h5_path, open_mode)

    def close_h5(self):
        self.h5f.flush()
        self.h5f.close()

    @register_dataset(dataset_registry, 'frame')
    def create_frame_dataset(self):
        img_h, img_w = self.config.frame_dim
        frame_dataset = self.h5f.create_dataset(
            # self.NAME_FRAME_DS, 
            settings.storage.databases.frame,
            (self.config.initial_size, img_h, img_w), 
            maxshape=(None, img_h, img_w), 
            # chunks=True, 
            chunks=(1, img_h, img_w),
            dtype=np.uint16,
            # compression=self.config.compression,
            # compression_opts=self.config.compression_opts
            scaleoffset=self.config.scaleoffset
        )
        frame_dataset.attrs['size'] = 0

        self.frame_meta_columns = ["time_stamp", "time"]        
        frame_meta_dataset = self.h5f.create_dataset(
            # self.NAME_FRAME_META_DS, 
            settings.storage.databases.frame_meta,
            (self.config.initial_size, len(self.frame_meta_columns) ), 
            maxshape=(None, len(self.frame_meta_columns) ), 
            chunks=True, 
            # chunks=(1, len(self.frame_meta_columns) ),
            dtype=h5py.string_dtype(encoding='utf-8', length=None)
        )
        frame_meta_dataset.attrs['columns'] = self.frame_meta_columns
        frame_meta_dataset.attrs['size'] = 0

    @register_dataset(dataset_registry, 'log')
    def create_log_dataset(self):
        log_dataset = self.h5f.create_dataset(
            # self.NAME_LOG_DS, 
            settings.storage.databases.log,
            (self.config.initial_size, len(self.config.log_columns)), 
            maxshape=(None, len(self.config.log_columns)), 
            chunks=True, 
            dtype=h5py.string_dtype(encoding='utf-8', length=None)
        )

        log_dataset.attrs['columns'] = self.config.log_columns
        log_dataset.attrs['size'] = 0

    @register_dataset(dataset_registry, 'ai')
    def create_ai_dataset(self):
        pattern_h, pattern_w = self.config.pattern_dim

        pattern_dataset = self.h5f.create_dataset(
            # self.NAME_PATTERN_DS, 
            settings.storage.databases.pattern,
            (self.config.initial_size, pattern_h, pattern_w), 
            maxshape=(None, pattern_h, pattern_w), 
            # chunks=True,
            chunks=(1, pattern_h, pattern_w),
            compression=self.config.compression,
            compression_opts=self.config.compression_opts
        )
        pattern_dataset.attrs['size'] = 0

        detection_meta_dataset = self.h5f.create_dataset(
            # self.NAME_DETECTION_META_DS, 
            settings.storage.databases.detection_meta,
            (self.config.initial_size, len(self.config.detection_meta_columns)), 
            maxshape=(None, len(self.config.detection_meta_columns)), 
            chunks=True, 
            dtype=h5py.string_dtype(encoding='utf-8', length=None)
        )
        detection_meta_dataset.attrs['size'] = 0
        detection_meta_dataset.attrs['columns'] = self.config.detection_meta_columns

        classification_dataset = self.h5f.create_dataset(
            # self.NAME_CLASSIFICATION_DS, 
            settings.storage.databases.classification,
            (self.config.initial_size, len(self.config.classifier_classes)), 
            maxshape=(None, len(self.config.classifier_classes)), 
            # chunks=True
            chunks=(1, len(self.config.classifier_classes)),
        )
        classification_dataset.attrs['size'] = 0
        classification_dataset.attrs['class_name'] = self.config.classifier_classes

        n_init_detection = 2
        detection_dataset = self.h5f.create_dataset(
            # self.NAME_DETECTION_DS, 
            settings.storage.databases.detection,
            (self.config.initial_size, n_init_detection, 6), 
            maxshape=(None, None, 6), 
            # chunks=True
            chunks=(1, 1, 6),
        )
        detection_dataset.attrs['size'] = 0
        detection_dataset.attrs['columns'] = ['sx', 'sy', 'ex', 'ey', 'label', 'score']
        detection_dataset.attrs['class_name'] = self.config.detector_classes

        instance_segmentation_dataset = self.h5f.create_dataset(
            # self.NAME_INSTANCE_SEGMENTATION_DS, 
            settings.storage.databases.instance_segmentation,
            (self.config.initial_size, n_init_detection, pattern_h, pattern_w), 
            maxshape=(None, None, pattern_h, pattern_w), 
            # chunks=True, 
            chunks=(1, 1, pattern_h, pattern_w),
            dtype=bool, # bool is 1 bytes in C
            # dtype=np.uint8,
            compression=self.config.compression,
            compression_opts=self.config.compression_opts,
            # scaleoffset=self.config.scaleoffset
        )
        instance_segmentation_dataset.attrs['size'] = 0

        num_detection_dataset = self.h5f.create_dataset(
            # self.NAME_NUM_DETECTION_DS, 
            settings.storage.databases.num_detection,
            (self.config.initial_size,), 
            maxshape=(None,), 
            chunks=True,
            dtype=np.int32
        )
        num_detection_dataset.attrs['size'] = 0

        num_tracking_dataset = self.h5f.create_dataset(
            # self.NAME_NUM_TRACK_DS, 
            settings.storage.databases.num_tracking,
            (self.config.initial_size,), 
            maxshape=(None,), 
            chunks=True,
            dtype=np.int32
        )
        num_tracking_dataset.attrs['size'] = 0

        n_init_tracking = 2
        tracking_dataset = self.h5f.create_dataset(
            # self.NAME_TRACK_DS, 
            settings.storage.databases.tracking,
            (self.config.initial_size, n_init_tracking), 
            maxshape=(None, None), 
            chunks=True,
            dtype=np.int32
        )
        tracking_dataset.attrs['size'] = 0

    @register_dataset(dataset_registry, 'integration')
    def create_integration_dataset(self):
        # img_h, img_w = self.config.frame_dim
        self._integration_columns = ["bbox_id"] + list(IntegrationResult.__annotations__.keys())
        self._integration_root_columns = ["bbox_idx_start", "bbox_idx_end"]
        self._integration_meta_columns = ["time_stamp", "time"]

        integration_dataset = self.h5f.create_dataset(
            # self.NAME_FRAME_DS, 
            settings.storage.databases.integration,
            (self.config.initial_size, len(self._integration_columns)), 
            maxshape=(None, len(self._integration_columns)), 
            chunks=True, 
            dtype=np.float32
        )        
        integration_dataset.attrs['columns'] = self._integration_columns
        integration_dataset.attrs['size'] = 0

        integration_root_dataset = self.h5f.create_dataset(
            # self.NAME_FRAME_META_DS, 
            settings.storage.databases.integration_root,
            (self.config.initial_size, len(self._integration_root_columns)),
            maxshape=(None, 2), 
            chunks=True, 
            dtype=np.int32
        )
        integration_root_dataset.attrs['columns'] = self._integration_root_columns
        integration_root_dataset.attrs['size'] = 0

        integration_meta_dataset = self.h5f.create_dataset(
            # self.NAME_FRAME_META_DS, 
            settings.storage.databases.integration_meta,
            (self.config.initial_size, len(self._integration_meta_columns) ), 
            maxshape=(None, len(self._integration_meta_columns) ), 
            chunks=True, 
            # chunks=(1, len(self.frame_meta_columns) ),
            dtype=h5py.string_dtype(encoding='utf-8', length=None)
        )
        integration_meta_dataset.attrs['columns'] = self._integration_meta_columns
        integration_meta_dataset.attrs['size'] = 0

    def create_datasets(self):
        status = {
            "succ": True,
            "failed_datasets": [],
            "error_message": ""
        }

        # Loop through all registered dataset creators
        for dataset_creator in self.dataset_registry:
            config_flag = getattr(self.config, dataset_creator['config_flag'], False)
            if config_flag:
                try:
                    dataset_creator['method'](self)
                except Exception as e:
                    logging.error(get_error_info(e))
                    status["failed_datasets"].append(dataset_creator['type'])
                    status["error_message"] += str(e) + "\n"

        status['succ'] = len(status["failed_datasets"]) == 0
        return status

    def _init_idx(self):
        self._idx_log = 0
        self._idx_frame = 0
        self._idx_detection = 0
        self._idx_integration = 0
        self._idx_integration_bbox = 0

    @property
    def next_log_idx(self):
        idx = self._idx_log
        self._idx_log += 1
        return idx
    
    @property
    def next_frame_idx(self):
        idx = self._idx_frame
        self._idx_frame += 1
        return idx

    @property
    def next_detection_idx(self):
        idx = self._idx_detection
        self._idx_detection += 1
        return idx

    @property
    def next_integration_idx(self):
        idx = self._idx_integration
        self._idx_integration += 1
        return idx

    @property
    def next_integration_bbox_idx(self):
        idx = self._idx_integration_bbox
        self._idx_integration_bbox += 1
        return idx

    def save_log(
            self, 
            chamber_log, 
            idx=None, 
            resize_step=None
        ):
        self.check_save(self.config.save_log, flag_name="save_log")

        if resize_step is None: resize_step = self.RESIZE_STEP
        _idx = self.next_log_idx if idx is None else idx

        resize_if_over(_idx, self.ds_log, resize_step=self.RESIZE_STEP)
        self.ds_log[_idx] = [ str(chamber_log[k]) for k in self.ds_log.attrs['columns'] ]

        if idx is None: self.ds_log.attrs['size'] += 1

    def save_frame(
            self, 
            frame, 
            frame_headers, 
            idx=None, 
            resize_step=None
        ):
        # logging.info(f"start save_frame idx={idx}")
        self.check_save(self.config.save_frame, "save_frame")

        if self.frame_speed_limiter is not None:
            if not self.frame_speed_limiter.throttle():
                return

        if resize_step is None: resize_step = self.RESIZE_STEP
        # this would break the order of storage
        _idx = self.next_frame_idx if idx is None else idx
        # logging.info(f"save_frame data {_idx}")
        resize_if_over(_idx, self.ds_frame, resize_step=resize_step)
        self.ds_frame[_idx] = frame if frame.ndim == 2 else frame[..., 0]
        # logging.info(f"save_frame meta {_idx} done")
        resize_if_over(_idx, self.ds_frame_meta, resize_step=resize_step)
        self.ds_frame_meta[_idx] = [ str(frame_headers[k]) for k in self.ds_frame_meta.attrs['columns'] ]

        # logging.info(f"add size")
        if idx is None: self.ds_frame.attrs['size'] += 1
        if idx is None: self.ds_frame_meta.attrs['size'] += 1
        # print(f"done {_idx}")

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
        if self.detection_speed_limiter is not None:
            if not self.detection_speed_limiter.throttle():
                return
        
        if resize_step is None: resize_step = self.RESIZE_STEP
        _idx = self.next_detection_idx if idx is None else idx

        num_detection = len(masks)
        num_tracking = len(tracking)

        if num_detection > 0:
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

    def _save_integrations_buffered_data(
            self, integrations_data, integrations_meta, 
            integrations_idx, integrations_bbox_idx_start, integrations_bbox_idx_end,
            # is_append = True,
        ):
        
        resize_if_over(integrations_bbox_idx_end[-1], self.ds_integration, resize_step=self.RESIZE_STEP)        
        self.ds_integration[integrations_bbox_idx_start[0]:integrations_bbox_idx_end[-1]+1] = integrations_data
        # print("save integration", integrations_bbox_idx_start, integrations_bbox_idx_end)

        resize_if_over(integrations_bbox_idx_end[-1], self.ds_integration_meta, resize_step=self.RESIZE_STEP)
        self.ds_integration_meta[integrations_bbox_idx_start[0]:integrations_bbox_idx_end[-1]+1] = integrations_meta
        # print("save integration meta", integrations_bbox_idx_start, integrations_bbox_idx_end)

        resize_if_over(integrations_idx[-1], self.ds_integration_root, resize_step=self.RESIZE_STEP)
        _integrations_root_content = np.stack([ integrations_bbox_idx_start, integrations_bbox_idx_end ], axis=1)

        self.ds_integration_root[integrations_idx[0]:integrations_idx[-1]+1] = _integrations_root_content
        # print("save integration index", _integration_idx)

        # if is_append:

        self.ds_integration.attrs['size'] += integrations_bbox_idx_end[-1] - integrations_bbox_idx_start[0] + 1
        self.ds_integration_root.attrs['size'] += len(integrations_idx)
        self.ds_integration_meta.attrs['size'] += integrations_bbox_idx_end[-1] - integrations_bbox_idx_start[0] + 1

    def save_integrations_buffered(
        self,
        integrations : IntegrationCollection,
        integrations_meta : Dict[str, Any],
        # integration_idx=None,
        resize_step=None
    ):
        """
        this function is used to save integrations in buffered mode. 
        Only allow for appending integrations to the dataset.
        """
        self.check_save(self.config.save_integration, flag_name="save_integration")

        _integrations_data = np.zeros((len(integrations), len(self._integration_columns)), dtype=np.float32)
        _integrations_meta = np.zeros((len(integrations), len(self._integration_meta_columns)), dtype=np.object_)

        # if integration_idx is None:
        _integrations_idx = self.next_integration_idx
        _integrations_bbox_idx_start, _integrations_bbox_idx_end = None, None
        # else:
        #     _integrations_idx = integration_idx
        #     _integrations_bbox_idx_start, _integrations_bbox_idx_end = self.ds_integration_root[integration_idx]
        #     assert 1 + (_integrations_bbox_idx_end - _integrations_bbox_idx_start) == len(integrations)

        bbox_idxes = []
        for i, (bbox_id, integration) in enumerate(integrations.items()):
            _integration_bbox_idx = _integrations_bbox_idx_start + i if _integrations_bbox_idx_start is not None else self.next_integration_bbox_idx
            _integrations_data[i] = [int(bbox_id)] + [ integration[k] for k in self._integration_columns[1:] ]
            # create meta for each box for the ease of per bbox retrieval
            _integrations_meta[i] = [ integrations_meta[k] for k in self._integration_meta_columns ]
            bbox_idxes.append(_integration_bbox_idx)

        _integrations_bbox_idx_start, _integrations_bbox_idx_end = bbox_idxes[0], bbox_idxes[-1]

        with self._lock_buffer_integrations:            
            # print("append integrations start", _integrations_bbox_idx_start, _integrations_bbox_idx_end, time.time())
            self._buffer_integrations["content"].append(_integrations_data)
            self._buffer_integrations["meta"].append(_integrations_meta)
            self._buffer_integrations["bbox_idx_start"].append(_integrations_bbox_idx_start)
            self._buffer_integrations["bbox_idx_end"].append(_integrations_bbox_idx_end)
            self._buffer_integrations["integration_idx"].append(_integrations_idx)
            # print("append integrations end", _integrations_bbox_idx_start, _integrations_bbox_idx_end, time.time())

        if len(self._buffer_integrations["content"]) >= settings.storage.hdf5_recorder.buffer.intergration_buffer_size:
            with self._lock_buffer_integrations:
                if resize_step is None: resize_step = self.RESIZE_STEP

                _integrations_data = np.concatenate(self._buffer_integrations["content"], axis=0)
                _integrations_meta = np.concatenate(self._buffer_integrations["meta"], axis=0)
                _integrations_idx = self._buffer_integrations["integration_idx"]
                _integrations_bbox_idx_start = self._buffer_integrations["bbox_idx_start"]
                _integrations_bbox_idx_end = self._buffer_integrations["bbox_idx_end"]

                # print(_integrations_bbox_idx_start)

                # print(_integrations_bbox_idx_end)

                # print("save integrations from buffered data start", _integrations_bbox_idx_start[0], _integrations_bbox_idx_end[-1])
                # print(_integrations_data.shape, _integrations_meta.shape, len(_integrations_idx), len(_integrations_bbox_idx_start), len(_integrations_bbox_idx_end))
                try:
                    self._save_integrations_buffered_data(
                        _integrations_data,
                        _integrations_meta,
                        _integrations_idx,
                        _integrations_bbox_idx_start,
                        _integrations_bbox_idx_end,
                    )
                except Exception as e:
                    logging.error(f"save integrations from buffered data error: {e}")
                # print("save integrations from buffered data end", _integrations_bbox_idx_start[0], _integrations_bbox_idx_end[-1])

                self._buffer_integrations["content"] = []
                self._buffer_integrations["meta"] = []
                self._buffer_integrations["bbox_idx_start"] = []
                self._buffer_integrations["bbox_idx_end"] = []
                self._buffer_integrations["integration_idx"] = []
                # print("clear buffered integrations")


    def save_integrations(
        self,
        integrations : IntegrationCollection,
        integrations_meta : Dict[str, Any],
        integration_idx=None,
        resize_step=None
    ):
        self.check_save(self.config.save_integration, flag_name="save_integration")
        
        _integrations_data = np.zeros((len(integrations), len(self._integration_columns)), dtype=np.float32)
        _integrations_meta = np.zeros((len(integrations), len(self._integration_meta_columns)), dtype=np.object_)

        if integration_idx is None:
            _integration_idx = self.next_integration_idx
            _integration_bbox_idx_start, _integration_bbox_idx_end = self.next_integration_bbox_idx, None
        else:
            _integration_idx = integration_idx
            _integration_bbox_idx_start, _integration_bbox_idx_end = self.ds_integration_root[integration_idx]
            assert 1 + (_integration_bbox_idx_end - _integration_bbox_idx_start) == len(integrations)

        if resize_step is None: resize_step = self.RESIZE_STEP

        for i, (bbox_id, integration) in enumerate(integrations.items()):
            _integration_bbox_idx = _integration_bbox_idx_start + i
            _integrations_data[i] = [int(bbox_id)] + [ integration[k] for k in self._integration_columns[1:] ]
            # create meta for each box for the ease of per bbox retrieval
            _integrations_meta[i] = [ integrations_meta[k] for k in self._integration_meta_columns ]
            resize_if_over(_integration_bbox_idx, self.ds_integration, resize_step=self.RESIZE_STEP)
            resize_if_over(_integration_bbox_idx, self.ds_integration_meta, resize_step=self.RESIZE_STEP)
        _integration_bbox_idx_end = _integration_bbox_idx

        self.ds_integration[_integration_bbox_idx_start:_integration_bbox_idx_end+1] = _integrations_data
        # print("save integration", _integration_bbox_idx_start, _integration_bbox_idx_end)

        self.ds_integration_meta[_integration_bbox_idx_start:_integration_bbox_idx_end+1] = _integrations_meta
        # print("save integration meta", _integration_bbox_idx_start, _integration_bbox_idx_end)
        
        resize_if_over(_integration_idx, self.ds_integration_root, resize_step=self.RESIZE_STEP)
        _integrations_index_content = np.array([ _integration_bbox_idx_start, _integration_bbox_idx_end ])

        self.ds_integration_root[_integration_idx] = _integrations_index_content
        # print("save integration index", _integration_idx)

        if integration_idx is None: self.ds_integration.attrs['size'] += _integration_bbox_idx_end - _integration_bbox_idx_start + 1
        if integration_idx is None: self.ds_integration_root.attrs['size'] += 1
        if integration_idx is None: self.ds_integration_meta.attrs['size'] += _integration_bbox_idx_end - _integration_bbox_idx_start + 1

@dataclass
class RecorderServerConfig:
    idle_time : float = settings.storage.hdf5_recorder.idle_time
    max_workers : int = settings.storage.hdf5_recorder.max_workers


class RecorderServer(threading.Thread):
    recorder : Recorder
    executor : ThreadPoolExecutor
    # futures : Dict[str, asyncio.Future]

    def __init__(
        self,
        config: RecorderServerConfig,
        recorder : Recorder,
        name : Union[int, str]        
    ):
        super().__init__(name=name)
        self.config =  config
        self._futures_lock = threading.Lock()
        self.futures = {}
        self.executor = None
        self.recorder = recorder
        self._stop_event = threading.Event()
        # self._total_jobs = 0

    def create_datasets(self):
        return self.recorder.create_datasets()

    def close_storages(self):
        self.recorder.close_h5()
        self.stop()


    def save_prediction(self, *args, **kargs):        
        future = self.executor.submit( self.recorder.save_prediction, *args, **kargs )
        with self._futures_lock:
            self.futures[uuid.uuid4()] = future
        # self._total_jobs += 1

    def save_frame(self, *args, **kargs):
        future = self.executor.submit( self.recorder.save_frame, *args, **kargs )
        with self._futures_lock:
            self.futures[uuid.uuid4()] = future
        # self._total_jobs += 1

    def save_log(self, *args, **kargs):
        future = self.executor.submit( self.recorder.save_log, *args, **kargs )
        with self._futures_lock:
            self.futures[uuid.uuid4()] = future
        # self._total_jobs += 1

    def save_integrations(self, *args, **kargs):
        future = self.executor.submit( self.recorder.save_integrations_buffered, *args, **kargs )
        with self._futures_lock:
            self.futures[uuid.uuid4()] = future
        # self._total_jobs += 1
        # print("save integrations", args, kargs)


    def clear_futures(self):
        if self.executor._work_queue.qsize() > 50:
            logging.info(f"Recorder server ({self.ident}) #{self.executor._work_queue.qsize()} jobs stalled in workqueue. Performance may be degraded. Consider reducing the speed limit, improving compression strategy, or adjusting chunk shape")

        with self._futures_lock:
            self.futures = {}
            # next_futures = {}
            # for idx, future in self.futures.items():
            #     if not future.done():
            #         next_futures[idx] = future
            #     else:
            #         future.result()
            # self.futures = next_futures


    def run(self):
        logging.info(f"Recorder server ({self.ident}) started")
        # self.executor = ProcessPoolExecutor(max_workers=self.config.max_workers)
        self.executor = ThreadPoolExecutor(max_workers=self.config.max_workers)

        while True:
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : break
            self.clear_futures()
        
        logging.info(f"Recorder server ({self.ident}) run loop terminated, wait for #{self.executor._work_queue.qsize()} jobs to finish")
        self.executor.shutdown(wait=True, cancel_futures=False)
        while len(self.futures):
            # wait until all job are done
            time.sleep(0.1)
            self.clear_futures()
        logging.info(f"Recorder server ({self.ident}) #{len(self.futures)} jobs left. termination complete.")

    def stop(self):
        logging.info(f"Recorder server ({self.ident}) receives a stop signal")
        self._stop_event.set()


class RecordReader(RecordDataset):

    def __init__(self, record_path:Path):
        self.record_path = record_path
        self.h5f = h5py.File(record_path, 'r')

    def get_integration_item(self, idx) -> Tuple[IntegrationCollection, Dict[str, Any]]:
        if idx < 0: idx = self.ds_integration_root.attrs['size'] + idx

        integration_bbox_idx_start, integration_bbox_idx_end = self.ds_integration_root[idx]
        integration_collector = self._parse_integration_item(self.ds_integration[integration_bbox_idx_start:integration_bbox_idx_end+1])
        integration_meta = self._parse_integration_meta(self.ds_integration_meta[integration_bbox_idx_start])
        return integration_collector, integration_meta

    def _parse_integration_item(self, integration) -> IntegrationCollection:
        integration_collector : IntegrationCollection = {}
        for row_idx in range(integration.shape[0]):
            content : IntegrationResult = { k : v for k, v in zip( self.ds_integration.attrs['columns'][1:], integration[row_idx, 1:] )}            
            integration_collector[integration[row_idx, 0]] = content
        return integration_collector

    def _parse_integration_meta(self, integration_meta) -> Dict[str, Any]:
        integration_meta = { k : v.decode("utf-8") for k, v in zip( self.ds_integration_meta.attrs['columns'], integration_meta )}
        integration_meta['time_stamp'] = np.datetime64(integration_meta['time_stamp']) if integration_meta['time_stamp'] else integration_meta['time_stamp']
        integration_meta['time'] = float(integration_meta['time']) if integration_meta['time'] else integration_meta['time']
        return integration_meta

    def iter_integration_dataset(self, start=0, end=None, step=1) -> Generator[Tuple[IntegrationCollection, Dict[str, Any]], None, None]:
        end = self.ds_integration_root.attrs['size'] if end is None else end
        for idx in range(start, end, step):
            integration_collector, integration_meta = self.get_integration_item(idx)
            yield integration_collector, integration_meta

    def get_integration_by_bbox_id(self, bbox_id) -> pd.DataFrame:
        columns = list(self.ds_integration.attrs['columns']) + list(self.ds_integration_meta.attrs['columns'])
        mask = np.array(self.ds_integration[:self.ds_integration.attrs['size'], 0], copy=True).astype(np.int32) == bbox_id
        if not np.any(mask):
            return pd.DataFrame(data=[], columns=columns)

        mask = np.where(mask)[0]

        data = np.concatenate([self.ds_integration[mask], self.ds_integration_meta[mask]], axis=1)
        integration_df = pd.DataFrame(data, columns=columns)
        integration_df['time'] = integration_df['time'].str.decode('utf-8').astype(np.float64)
        integration_df['time_stamp'] = integration_df['time_stamp'].str.decode('utf-8').astype("datetime64[ns]")
        return integration_df
    
    def get_all_integrations(self) -> Dict[int, pd.DataFrame]:
        unique_bbox_ids = np.unique(np.array(self.ds_integration[:self.ds_integration.attrs['size'], 0], copy=True).astype(np.int32))
        # print(unique_bbox_ids)

        integrations = {}
        for bbox_id in unique_bbox_ids:
            integration_df = self.get_integration_by_bbox_id(bbox_id)
            integrations[bbox_id] = integration_df
        return integrations

    def get_log_columns(self):
        return self.ds_log.attrs['columns']

    def get_log_item(self, idx):
        if idx < 0: idx = self.ds_log.attrs['size'] + idx

        log_columns = self.get_log_columns()
        log_item = self.ds_log[idx]

        if len(log_item):
            try:
                return { k : TYPE_CONVERSIONS[k](v.decode('utf-8')) if k in TYPE_CONVERSIONS else v for k,v in zip(log_columns, log_item) }
            except Exception as e:
                logging.error(f"Failed to convert log item {idx} to {log_columns}: {str(e)}")
                return { }
        else:
            logging.warning(f"Log item {idx} is empty")
            return { }

    def get_logs_by_column(self, column, column_transform=None, idx=None):
        log_dataset = self.ds_log
        log_columns = self.get_log_columns()
        dataset_size = self.ds_log.attrs['size']
        if idx is None: idx = slice(None, dataset_size)

        if column_transform is None: 
            if column in TYPE_CONVERSIONS:
                column_transform = TYPE_CONVERSIONS[column]
            else:
                raise ValueError(f"Column {column} is not in TYPE_CONVERSIONS, please provide a column_transform function")

        data = np.array(
            list(
                map(
                column_transform, 
                log_dataset[idx, log_columns.index(column)]
                )
            )
        )
        return data
    
    def get_full_logs(self):
        log_dataset = self.ds_log
        log_columns = self.get_log_columns()
        dataset_size = self.ds_log.attrs['size']
        data = np.array( log_dataset[ :dataset_size ] )

        df = pd.DataFrame(data, columns=log_columns)

        # Apply type conversions
        for column, dtype in TYPE_CONVERSIONS.items():
            if column in df.columns:
                try:
                    df[column] = df[column].str.decode('utf-8').apply(dtype)
                except Exception as e:
                    logging.warning(f"Failed to convert column {column} to {dtype}: {str(e)}")

        return df
    
    def iter_log_dataset(self, start=0, end=None, step=1):
        end = self.ds_log.attrs['size'] if end is None else end
        for idx in range(start, end, step):
            yield self.get_log_item(idx)

    def get_log_idx_between(self, start_time, end_time):
        log_time = self.get_logs_by_column("time_stamp")
        start_time = np.datetime64(start_time)
        end_time = np.datetime64(end_time)

        mask = np.logical_and( log_time > start_time, log_time < end_time )
        return np.where(mask)[0]
    
    def _parse_detection_meta(self, detection_meta):
        detection_meta = { k : v.decode("utf-8") for k, v in zip( self.ds_detection_meta.attrs['columns'], detection_meta )}

        detection_meta['time_stamp'] = np.datetime64(detection_meta['time_stamp']) if detection_meta['time_stamp'] else detection_meta['time_stamp']
        detection_meta['time'] = float(detection_meta['time']) if detection_meta['time'] else detection_meta['time']
        detection_meta['crop_setup_sx'] = int(detection_meta['crop_setup_sx']) if detection_meta['crop_setup_sx'] else detection_meta['crop_setup_sx']
        detection_meta['crop_setup_ex'] = int(detection_meta['crop_setup_ex']) if detection_meta['crop_setup_ex'] else detection_meta['crop_setup_ex']
        detection_meta['crop_setup_sy'] = int(detection_meta['crop_setup_sy']) if detection_meta['crop_setup_sy'] else detection_meta['crop_setup_sy']
        detection_meta['crop_setup_ey'] = int(detection_meta['crop_setup_ey']) if detection_meta['crop_setup_ey'] else detection_meta['crop_setup_ey']
        return detection_meta
    
    def get_detection_item(self, idx):
        if idx < 0: idx = self.ds_pattern.attrs['size'] + idx
        pattern = self.ds_pattern[idx]
        num_detection = self.ds_num_detection[idx]
        detection_meta = self.ds_detection_meta[idx]
        masks = self.ds_instance_segmentation[idx, :num_detection]
        detections = self.ds_detection[idx, :num_detection]
        classification = self.ds_classification[idx]
        # tracking = self.ds_tracking[idx]

        detection_meta =  self._parse_detection_meta(detection_meta)

        return pattern, masks, detections, classification, detection_meta
    
    def _parse_frame_meta(self, frame_meta):
        frame_meta = { k : v.decode("utf-8") for k, v in zip( self.ds_frame_meta.attrs['columns'], frame_meta )}
        frame_meta['time_stamp'] = np.datetime64(frame_meta['time_stamp']) if frame_meta['time_stamp'] else frame_meta['time_stamp']
        frame_meta['time'] = float(frame_meta['time']) if frame_meta['time'] else frame_meta['time']
        return frame_meta
    
    def get_frame(self, idx):
        if idx < 0: idx = self.ds_frame.attrs['size'] + idx
        frame = self.ds_frame[idx]
        frame_meta = self.ds_frame_meta[idx]
        frame_meta = self._parse_frame_meta(frame_meta)
        
        return frame, frame_meta

    def iter_detection_dataset(self, start=0, end=None, step=1):
        end = self.ds_pattern.attrs['size'] if end is None else end
        for idx in range(start, end, step):
            pattern, masks, detections, classification, detection_meta = self.get_detection_item(idx)
            yield pattern, masks, detections, classification, detection_meta

    def iter_frame_dataset(self, start=0, end=None, step=1):
        end = self.ds_frame.attrs['size'] if end is None else end
        for idx in range(start, end, step):
            frame, frame_meta = self.get_frame(idx)
            yield frame, frame_meta

    def close(self):
        self.h5f.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close() 