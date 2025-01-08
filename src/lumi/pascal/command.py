from abc import ABC
from enum import Enum
from typing import Optional, Union, Dict, List

def _drop_tailling_zero(log_text):
    decimal, exponent = log_text.split('E')
    return f"{decimal}E{int(exponent):d}"

class PascalCommand(ABC):
    def to_text(self):
        pass

    def __repr__(self) -> str:
        return self.to_text()    


class PascalNowaitMixin:
    def apply_nowait(self, cmd):
        if self.nowait:
            return cmd + " (Nowait)"
        else:
            return cmd


class PascalSyncMixin:
    def apply_sync(self, cmd):
        if self.sync:
            return cmd + " (Sync)"
        else:
            return cmd


class TargetTwistModeType(Enum):
    OFF="OFF"
    ON="ON"
    AUTO="AUTO"


class TargetRotationModeType(Enum):
    OFF="OFF"
    ON="ON"
    AUTO="AUTO"


class SampleRotationModeType(Enum):
    OFF="OFF"
    ON="ON"
    AUTO="AUTO"


class TemperatureControlModeType(Enum):
    MANUAL = "Manual"
    PID = "PID"


class Targets(Enum):
    A="A"
    B="B"
    C="C"
    D="D"
    E="E"
    F="F"
    Clear="Clear"


class MaskID(Enum):
    M1=1
    M2=2


class MFCCVs(Enum):
    CV301="CV301"
    CV302="CV302"
    MV10="MV10"
    MV11="MV11"


class MFCs(Enum):
    MFC1="MFC1"
    MFC2="MFC2"


class PressureGauges(Enum):
    CDG10="CDG10"
    CDG11="CDG11"


class CoilPositions(Enum):
    A="A"
    B="B"
    C="C"
    D="D"
    E="E"
    F="F"


class RHEEDGunXAxes(Enum):
    P1=1
    P2=2
    P3=3
    P4=4
    P5=5
    P6=6
    P7=7
    P8=8
    P9=9
    P10=10


class PascalState:
    def __init__(self, state:Union[bool, str]) -> None:
        if isinstance(state, str):
            if state.upper() == "ON":
                self.state = True
            elif state.upper() == "OFF":
                self.state = False
            else:
                raise ValueError(state)
        elif isinstance(state, bool):
            self.state = state
        else:
            raise ValueError(state)
    
    def __bool__(self):
        return self.state

    def __str__(self):
        return "ON" if self.state else "OFF"
    

class PascalEnable:
    def __init__(self, enable:Union[bool, str]) -> None:
        if isinstance(enable, str):
            if enable.upper() == "ENABLE":
                self.enable = True
            elif enable.upper() == "DISABLE":
                self.enable = False
            else:
                raise ValueError(enable)
        elif isinstance(enable, bool):
            self.enable = enable
        else:
            raise ValueError(enable)
    
    def __bool__(self):
        return self.enable

    def __str__(self):
        return "Enable" if self.enable else "Disable"


class PascalLock:
    def __init__(self, locked:Union[bool, str]) -> None:
        if isinstance(locked, str):
            if locked.upper() == "LOCKED":
                self.locked = True
            elif locked.upper() == "ACTIVE":
                self.locked = False
            else:
                raise ValueError(locked)
        elif isinstance(locked, bool):
            self.locked = locked
        else:
            raise ValueError("Unknown value for PascalLock: " + str(locked))
    
    def __bool__(self):
        return self.locked

    def __str__(self):
        return "LOCKED" if self.locked else "ACTIVE"


class PascalScope:
    prefix = "| "
    def __init__(self) -> None:
        super().__init__()
        self.children = []

    def add_child(self, child):
        self.children.append(child)

    def to_text(self, level=0):
        cmd = ""
        for c in self.children:
            if isinstance(c, PascalCommand):
                cmd += (self.prefix * level) +c.to_text()
            elif isinstance(c, PascalScope):
                cmd += c.to_text(level+1)
        return cmd
    
    def __repr__(self):
        return self.to_text()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        return False


class ForLoopStart(PascalCommand):
    def __init__(self, iteration:int) -> None:
        super().__init__()
        self.iteration = iteration

    def to_text(self):
        return f"Loop {self.iteration:d} (0)\n"


class ForLoopEnd(PascalCommand):
    def __init__(self) -> None:
        super().__init__()

    def to_text(self):
        return f"Loop End\n"


class ForLoop(PascalScope):
    def __init__(self, iteration) -> None:
        super().__init__()
        self.iteration = iteration

    def to_text(self, level=0):
        level = max(level-1, 0)
        cmd = ""
        cmd += (self.prefix * level) + ForLoopStart(self.iteration).to_text()
        cmd += super().to_text(level+1)
        cmd += (self.prefix * level) + ForLoopEnd().to_text()
        return cmd

    def __repr__(self):
        return self.to_text()
        
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        return False


class Wait(PascalCommand):
    def __init__(self, time:int) -> None:
        super().__init__()
        self.time = time

    def to_text(self):
        return f"Wait {self.time:d} sec (0)\n"


class WaitForContinue(PascalCommand):
    def __init__(self) -> None:
        super().__init__()

    def to_text(self):
        return "Wait for Continue\n"


class MoveMask(PascalCommand, PascalNowaitMixin):
    def __init__(self, mask_id:Union[int, MaskID], distance:float, nowait:bool) -> None:
        super().__init__()
        mask_id = mask_id.value if isinstance(mask_id, MaskID) else mask_id
        self.mask_id = mask_id
        self.distance = distance
        self.nowait = nowait

    def to_text(self):
        cmd = f"Move Mask M{self.mask_id:d}={self.distance:.2f}"
        cmd = self.apply_nowait(cmd)
        return cmd + "\n"


class Beep(PascalCommand):
    def __init__(self) -> None:
        super().__init__()

    def to_text(self):
        return "Beep\n"


class SelectTarget(PascalCommand, PascalNowaitMixin):
    def __init__(self, target:Union[str, Targets] , nowait:bool) -> None:
        super().__init__()
        target = target.value if isinstance(target, Targets) else target
        self.target = target 
        self.nowait = nowait


    def to_text(self):
        return self.apply_nowait(f"Select Target {self.target}") + "\n"


class TargetRotationMode(PascalCommand):
    def __init__(self, mode:Union[str, TargetRotationModeType]) -> None:
        super().__init__()
        mode = mode.value if isinstance(mode, TargetRotationModeType) else mode
        self.mode = mode

    def to_text(self):
        return f"Target Rotation Mode {self.mode}\n"


class TargetTwistMode(PascalCommand):
    def __init__(self, mode:Union[str, TargetTwistModeType]) -> None:
        super().__init__()
        mode = mode.value if isinstance(mode, TargetTwistModeType) else mode
        self.mode = mode

    def to_text(self):
        return f"Target Twist Mode {self.mode}\n"


class SeekTargetHome(PascalCommand, PascalNowaitMixin):
    def __init__(self, nowait:bool) -> None:
        super().__init__()
        self.nowait = nowait

    def to_text(self):
        return self.apply_nowait(f"Seek Targets Home") + "\n"


class MaskSpeed(PascalCommand):
    def __init__(self, mask_id:Union[int, MaskID], low:int, high:int, acceleration:int) -> None:
        super().__init__()
        mask_id = mask_id.value if isinstance(mask_id, MaskID) else mask_id        
        self.mask_id = mask_id
        self.low = low
        self.high = high
        self.acceleration = acceleration

    def to_text(self):
        cmd = f"Mask Speed M{self.mask_id:d} Low={self.low:d} High={self.high:d} Accel={self.acceleration:d}"
        return cmd + "\n"

class SeekMasktHome(PascalCommand, PascalNowaitMixin):
    def __init__(self, mask_id:Union[int, MaskID], nowait:bool) -> None:
        super().__init__()
        mask_id = mask_id.value if isinstance(mask_id, MaskID) else mask_id
        self.mask_id = mask_id
        self.nowait = nowait

    def to_text(self):
        return self.apply_nowait(f"Seek Mask Home M{self.mask_id}") + "\n"


class SetMaskPosition(PascalCommand, PascalNowaitMixin, PascalSyncMixin):
    def __init__(self, mask_id:Union[int, MaskID], distance:float, sync:bool, nowait:bool) -> None:
        super().__init__()
        mask_id = mask_id.value if isinstance(mask_id, MaskID) else mask_id
        self.mask_id = mask_id
        self.distance = distance
        self.sync = sync
        self.nowait = nowait

    def to_text(self):
        cmd = f"Set Mask Position M{self.mask_id:d}={self.distance:.2f}"
        cmd = self.apply_sync(cmd)
        cmd = self.apply_nowait(cmd)
        return cmd + "\n"


class MoveMask(PascalCommand, PascalSyncMixin, PascalNowaitMixin):
    def __init__(self, mask_id:Union[int, MaskID], distance:float, sync:bool, nowait:bool) -> None:
        super().__init__()
        mask_id = mask_id.value if isinstance(mask_id, MaskID) else mask_id
        self.mask_id = mask_id
        self.distance = distance
        self.sync = sync
        self.nowait = nowait

    def to_text(self):
        cmd = f"Move Mask M{self.mask_id:d}={self.distance:.2f}"
        cmd = self.apply_sync(cmd)
        cmd = self.apply_nowait(cmd)
        return cmd + "\n"


class SetSamplePosition(PascalCommand, PascalSyncMixin, PascalNowaitMixin):
    def __init__(self, position:float, sync:bool, nowait:bool) -> None:
        super().__init__()
        self.position = position
        self.sync = sync
        self.nowait = nowait

    def to_text(self):
        cmd = f"Set Sample Position {self.position:.2f}"
        cmd = self.apply_sync(cmd)
        cmd = self.apply_nowait(cmd)
        return cmd + "\n"


class RotateSample(PascalCommand, PascalSyncMixin, PascalNowaitMixin):
    def __init__(self, angle:float, sync:bool, nowait:bool) -> None:
        super().__init__()
        self.angle = angle
        self.sync = sync
        self.nowait = nowait

    def to_text(self):
        cmd = f"Rotate Sample {self.angle:.2f}"
        cmd = self.apply_sync(cmd)
        cmd = self.apply_nowait(cmd)
        return cmd + "\n"

class SampleRotationMode(PascalCommand):
    def __init__(self, mode:Union[str, SampleRotationModeType]) -> None:
        super().__init__()
        mode = mode.value if isinstance(mode, SampleRotationModeType) else mode
        self.mode = mode

    def to_text(self):
        return f"Sample Rotation Mode {self.mode}\n"


class HeatingLaserPointer(PascalCommand):
    def __init__(self, state:Union[bool, str, PascalState]) -> None:
        super().__init__()
        self.state = PascalState(state) if not isinstance(state, PascalState) else state

    def to_text(self):
        return f"Heating Laser Pointer {self.state}\n"


class HeatingLaserLock(PascalCommand, PascalNowaitMixin):
    def __init__(self, locked:Union[bool, str, PascalLock], nowait:bool) -> None:
        super().__init__()
        self.locked = PascalLock(locked=locked) if not isinstance(locked, PascalLock) else locked
        self.nowait = nowait

    def to_text(self):
        return self.apply_nowait(f"Heating Laser Lock {self.locked}") + "\n"


class HeatingLaser(PascalCommand, PascalNowaitMixin):
    def __init__(self, state:Union[bool, str, PascalState], nowait:bool) -> None:
        super().__init__()
        self.state = PascalState(state) if not isinstance(state, PascalState) else state
        self.nowait = nowait

    def to_text(self):
        cmd = f"Heating Laser {self.state}"
        return self.apply_nowait(cmd) + "\n"


class HeatingLaserThreshold(PascalCommand):
    def __init__(self, state:Union[bool, str, PascalState]) -> None:
        super().__init__()
        self.state = PascalState(state) if not isinstance(state, PascalState) else state

    def to_text(self):
        return f"Heating Laser Threshold {self.state}\n"


class SetHeatingCurrent(PascalCommand):
    def __init__(self, current:float) -> None:
        super().__init__()
        self.current = current

    def to_text(self):
        return f"Set Heating Current {self.current:.2f}\n"


class SetMinimumCurrent(PascalCommand):
    def __init__(self, min_current:float) -> None:
        super().__init__()
        self.min_current = min_current

    def to_text(self):
        return f"Set Minimum Current {self.min_current:.2f}\n"


class SetMaximumCurrent(PascalCommand):
    def __init__(self, max_current:float) -> None:
        super().__init__()
        self.max_current = max_current

    def to_text(self):
        return f"Set Maximum Current {self.max_current:.2f}\n"


class TemperatureTolerance(PascalCommand):
    def __init__(self, tolerance:float) -> None:
        super().__init__()
        self.tolerance = tolerance

    def to_text(self):
        return f"Temperature Tolerance {self.tolerance:.1f}\n"

class TemperatureRamp(PascalCommand):
    def __init__(self, ramp_rate:float, state:Union[bool, str, PascalState]) -> None:
        super().__init__()
        self.ramp_rate = ramp_rate
        self.state = PascalState(state) if not isinstance(state, PascalState) else state

    def to_text(self):
        if self.state:
            return f"Temperature Ramp {self.ramp_rate:.1f}\n"
        else:
            return f"Temperature Ramp {self.state}\n"
        

class TemperatureSet(PascalCommand, PascalNowaitMixin):
    def __init__(self, temperature:float, nowait:bool) -> None:
        super().__init__()
        self.temperature = temperature
        self.nowait = nowait

    def to_text(self):
        cmd = f"Temperature Set {self.temperature:.1f}"
        return self.apply_nowait(cmd) + "\n"

class TemperatureControl(PascalCommand):
    def __init__(self, mode:Union[str, TemperatureControlModeType]) -> None:
        super().__init__()
        mode = mode.value if isinstance(mode, TemperatureControlModeType) else mode
        self.mode = mode

    def to_text(self):
        return f"Temperature Control {self.mode}\n"

class SampleShutter(PascalCommand):
    def __init__(self, state:Union[bool, str, PascalState]) -> None:
        super().__init__()
        self.state = PascalState(state) if not isinstance(state, PascalState) else state

    def to_text(self):
        return f"Sample Shutter {self.state}\n"

class DepoLaserGate(PascalCommand, PascalNowaitMixin):
    def __init__(self, state:Union[bool, str, PascalState], nowait:bool) -> None:
        super().__init__()
        self.state = PascalState(state) if not isinstance(state, PascalState) else state
        self.nowait = nowait

    def to_text(self):
        return self.apply_nowait(f"Depo Laser Gate {self.state}") + "\n"

class TriggerLaser(PascalCommand, PascalNowaitMixin, PascalSyncMixin):
    def __init__(self, num_pulse:int, frequency:int, sync:bool, nowait:bool) -> None:
        super().__init__()
        self.num_pulse = num_pulse
        self.frequency = frequency
        self.sync = sync
        self.nowait = nowait

    def to_text(self):
        cmd = f"Trigger Laser N={self.num_pulse:d} (0) F={self.frequency:.1f}"
        cmd = self.apply_sync(cmd)
        cmd = self.apply_nowait(cmd)
        return cmd + "\n"

class CombiLaser(PascalCommand, PascalNowaitMixin, PascalSyncMixin):
    def __init__(self, num_pulse:int, frequency:int, mask1_distance:float, mask2_distance:float, sync:bool, nowait:bool) -> None:
        super().__init__()
        self.num_pulse = num_pulse
        self.frequency = frequency
        self.mask1_distance = mask1_distance
        self.mask2_distance = mask2_distance
        self.sync = sync
        self.nowait = nowait

    def to_text(self):
        cmd = f"Combi N={self.num_pulse:d}(0) F={self.frequency:.1f} M1={self.mask1_distance:.2f} M2={self.mask2_distance:.2f}"
        cmd = self.apply_sync(cmd)
        cmd = self.apply_nowait(cmd)
        return cmd + "\n"

class SetMFCControl(PascalCommand):
    def __init__(self, enable:Union[bool, str, PascalEnable]) -> None:
        super().__init__()
        self.enable = PascalEnable(enable) if not isinstance(enable, PascalEnable) else enable

    def to_text(self):
        return f"MFC Control {self.enable}\n"

class SetMFCCV(PascalCommand):
    def __init__(self, valve:Union[str, MFCCVs], state:Union[bool, str, PascalState]) -> None:
        super().__init__()
        valve = valve.value if isinstance(valve, MFCCVs) else valve
        self.valve = valve
        self.state = PascalState(state) if not isinstance(state, PascalState) else state

    def to_text(self):
        return f"Valve {self.state} {self.valve}\n"

class SetMFC1Flow(PascalCommand):
    def __init__(self, MFC_flow:float) -> None:
        super().__init__()
        self.MFC_flow = MFC_flow

    def to_text(self):
        return f"MFC1 Flow Set= {self.MFC_flow:.2f}\n"

class SetMFC2Flow(PascalCommand):
    def __init__(self, MFC_flow) -> None:
        super().__init__()
        self.MFC_flow = MFC_flow

    def to_text(self):
        return f"MFC2 Flow Set= {self.MFC_flow:.2f}\n"


class SelectControlMFC(PascalCommand):
    def __init__(self, MFC:Union[str, MFCs]) -> None:
        super().__init__()
        MFC = MFC.value if isinstance(MFC, MFCs) else MFC
        self.MFC = MFC

    def to_text(self):
        return f"Press Control {self.MFC}\n"

class SelectPressureGauge(PascalCommand):
    def __init__(self, gauge:Union[str, PressureGauges]) -> None:
        super().__init__()
        self.gauge = gauge.value if isinstance(gauge, PressureGauges) else gauge

    def to_text(self):
        return f"Pressure Gauge {self.gauge}\n"

class SetPressure(PascalCommand):
    def __init__(self, pressure:float) -> None:
        super().__init__()
        self.pressure = pressure

    def to_text(self):
        log_text = _drop_tailling_zero(f"{self.pressure:.2E}")
        return f"Set Pressure= {log_text}\n"

class PressureControl(PascalCommand):
    def __init__(self, state:Union[bool, str, PascalState]) -> None:
        super().__init__()
        self.state = PascalState(state) if not isinstance(state, PascalState) else state

    def to_text(self):
        return f"Pressure Control {self.state}\n"


class SelectCoilPosition(PascalCommand):
    def __init__(self, position:Union[str, CoilPositions] ) -> None:
        super().__init__()
        position = position.value if isinstance(position, CoilPositions) else position
        self.position= position

    def to_text(self):
        return f"Select Coil Position {self.position}\n"


# class SelectRHEEDGunX(PascalCommand):
#     def __init__(self, axis:RHEEDGunXAxes) -> None:
#         super().__init__()
#         axis = axis.value if isinstance(axis, RHEEDGunXAxes) else axis
#         self.axis = axis

#     def to_text(self):
#         return f"Select RHEED Gun-X axis {self.axis}\n"

class SetRHEEDGunX(PascalCommand):
    def __init__(self, position:float) -> None:
        super().__init__()
        self.position = position

    def to_text(self):
        return f"Set RHEED Gun-X axis ={self.position:.2f}\n"

class SetLogInterval(PascalCommand):
    def __init__(self, time:int) -> None:
        super().__init__()
        self.time = time

    def to_text(self):
        return f"Log Interval {self.time}\n"

class DataLogging(PascalCommand):
    def __init__(self, file_name, state:Union[bool, str, PascalState]) -> None:
        super().__init__()
        self.file_name = file_name
        self.state = PascalState(state) if not isinstance(state, PascalState) else state
    def to_text(self):
        if self.state:
            return f"Data Logging File={self.file_name}\n"
        else:
            return f"Data Logging OFF\n"
        
class Comment(PascalCommand):
    def __init__(self, comment) -> None:
        super().__init__()
        self.comment = comment

    def to_text(self):
        return f"/ {self.comment}\n"
