"""
Need update 
"""

from pydantic import BaseModel
from typing import List


class Command(BaseModel):
    cmd : str

class CommandStatus(BaseModel):
    is_command_finished : bool
    current_command : str
    command_executing : str

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
