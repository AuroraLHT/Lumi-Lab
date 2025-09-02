"""
Type definitions for RHEED module.
"""

from typing import TypedDict, Union, Dict, Any, List
import numpy as np

class IntegrationResult(TypedDict):
    """Result of integrating pixel values over a bounding box region."""
    mean: float
    max: float
    min: float
    height: int
    width: int
    center_x: float
    center_y: float

IntegrationCollection = Dict[int, IntegrationResult]

class FFTResult(TypedDict):
    """Result of FFT analysis on time series data."""
    fft_freq: List[float]
    fft_mag: List[float]
    fft_phase: List[float]
    time_end: float
    timestamp_end: str
    time_start: float
    timestamp_start: str
    time_resolution: float

class FrameHeader(TypedDict):
    """Header information for camera frames."""
    time: float
    uuid: str
    time_stamp: str
    # Add other header fields as needed

class IntegrationHeader(FrameHeader):
    """Header for integration results."""
    bbox_id: Union[str, int]
    integration_uuid: str

class MultiIntegrationResult(TypedDict):
    """Multiple integration results keyed by bbox_id."""
    __annotations__: Dict[Union[str, int], IntegrationResult] 