from pydantic import BaseModel
from typing import List


class UNetInputConfig(BaseModel):
    batch: int
    shape : List[int]
    dtype : str


class UpdatePipelineConfig(BaseModel):
    threshold : float
    do_mean_clip : bool
    do_min_max : bool
    min_area : int
    do_tracking : bool
    tracker_t_min : int
    tracker_sigma_iou : float


class GetPipelineConfig(BaseModel):
    threshold : float
    do_mean_clip : bool
    do_min_max : bool
    classes : List[str]
    min_area : int
    do_tracking : bool
    tracker_t_min : int
    tracker_sigma_iou : float
