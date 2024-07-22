import unittest
from command import *

class TestSingleCommand(unittest.TestCase):

    def test_wait(self):
        self.assertEqual(
            Wait(10).to_text(), 'Wait 10 sec (0)'
        )

    def test_wait_for_continue(self):
        self.assertEqual(
            WaitForContinue().to_text(), 'Wait for Continue'
        )

    def test_beep(self):
        self.assertEqual(
            Beep().to_text(), 'Beep'
        )

    def test_select_target(self):
        self.assertEqual(
            SelectTarget(Targets.C, False).to_text(), 
            'Select Target C'
        )

    def test_target_rotation_mode(self):
        self.assertEqual(
            TargetRotationMode(TargetRotationModeType.OFF).to_text(), 
            'Target Rotation Mode OFF'
        )

    def test_target_twist_mode(self):
        self.assertEqual(
            TargetTwistMode(TargetTwistModeType.OFF).to_text(), 
            'Target Twist Mode OFF'
        )

    def test_seek_target_home(self):
        self.assertEqual(
            SeekTargetHome(nowait=False).to_text(),
            "Seek Targets Home"
        )

    def test_mask_speed(self):
        self.assertEqual(
            MaskSpeed(MaskID.M1, low=1, high=100, acceleration=500).to_text(), 
            'Mask Speed M1 Low=1 High=100 Accel=500'
        )

        self.assertEqual(
            MaskSpeed(MaskID.M2, low=2, high=400, acceleration=500).to_text(), 
            'Mask Speed M2 Low=2 High=400 Accel=500'
        )

    def test_set_mask_position(self):
        self.assertEqual(
            SetMaskPosition(
                mask_id=MaskID.M1, 
                distance=0.00, 
                sync=False, nowait=False).to_text(),
            "Set Mask Position M1=0.00"
        )
    


    def test_move_mask(self):
        self.assertEqual(
            MoveMask(1, 1.00, False, True).to_text(), 
            "Move Mask M1=1.00 (Nowait)"
        )

        self.assertEqual(
            MoveMask(1, 1.00, False, False).to_text(), 
            "Move Mask M1=1.00"
        )

    def test_sample_position(self):
        self.assertEqual(
            SetSamplePosition(0.00, sync=False, nowait=False).to_text(),
            "Set Sample Position 0.00"
        )

    def test_rotate_sample(self):
        self.assertEqual(
            RotateSample(0.00, sync=False, nowait=False).to_text(),
            "Rotate Sample 0.00"
        )

    def test_sample_rotation_mode(self):
        self.assertEqual(
            SampleRotationMode(mode=SampleRotationModeType.OFF).to_text(),
            "Sample Rotation Mode OFF"
        )

    def test_heating_laser_pointer(self):
        self.assertEqual(
            HeatingLaserPointer(state=PascalState(False)).to_text(),
            "Heating Laser Pointer OFF"
        )

        self.assertEqual(
            HeatingLaserPointer(state=False).to_text(),
            "Heating Laser Pointer OFF"
        )

        self.assertEqual(
            HeatingLaserPointer(state=PascalState('OFF')).to_text(),
            "Heating Laser Pointer OFF"
        )

        self.assertEqual(
            HeatingLaserPointer(state='ON').to_text(),
            "Heating Laser Pointer ON"
        )

        self.assertEqual(
            HeatingLaserPointer(state=True).to_text(),
            "Heating Laser Pointer ON"
        )

    def test_heating_laser_lock(self):
        self.assertEqual(
            HeatingLaserLock(
                locked=PascalLock(True),
                nowait=False
            ).to_text(),
            "Heating Laser Lock LOCKED"
        )
    
    def test_heating_laser(self):
        self.assertEqual(
            HeatingLaser(
                state = False,
                nowait=True
            ).to_text(),
            "Heating Laser OFF"
        )

    def test_heating_laser(self):
        self.assertEqual(
            HeatingLaserThreshold(
                state = False,
            ).to_text(),
            "Heating Laser Threshold OFF"
        )

    def test_set_heating_current(self):
        self.assertEqual(
            SetHeatingCurrent(
                current = 0.00,
            ).to_text(),
            "Set Heating Current 0.00"
        )

    def test_set_minimum_current(self):
        self.assertEqual(
            SetMinimumCurrent(
                min_current = 9.80,
            ).to_text(),
            "Set Minimum Current 9.80"
        )

    def test_set_maximum_current(self):
        self.assertEqual(
            SetMaximumCurrent(
                max_current = 30.00,
            ).to_text(),
            "Set Maximum Current 30.00"
        )

    def test_temperature_tolerance(self):
        self.assertEqual(
            TemperatureTolerance(
                tolerance=2.0,
            ).to_text(),
            "Temperature Tolerance 2.0"
        )

    def test_temperature_ramp(self):
        self.assertEqual(
            TemperatureRamp(
                ramp_rate=50.0,
                state="ON",
            ).to_text(),
            "Temperature Ramp 50.0"
        )

    def test_temperature_set(self):
        self.assertEqual(
            TemperatureSet(
                temperature=200.0,
                nowait=False,
            ).to_text(),
            "Temperature Set 200.0"
        )

    def test_temperature_control(self):
        self.assertEqual(
            TemperatureControl(
                mode=TemperatureControlModeType.MANUAL,
            ).to_text(),
            "Temperature Control Manual"
        )

    def test_depo_laser_gate(self):
        self.assertEqual(
            DepoLaserGate(
                state="OFF",
                nowait=False,
            ).to_text(),
            "Depo Laser Gate OFF"
        )

    def test_trigger_laser(self):
        self.assertEqual(
            TriggerLaser(
                num_pulse=10,
                frequency=10,
                sync=True,
                nowait=False
            ).to_text(),
            "Trigger Laser N=10 (0) F=10.0 (Sync)"
        )

    def test_combi_laser(self):
        self.assertEqual(
            CombiLaser(
                num_pulse=10,
                frequency=10,
                mask1_distance=0.00,
                mask2_distance=0,
                sync=True,
                nowait=False
            ).to_text(),
            "Combi N=10(0) F=10.0 M1=0.00 M2=0.00 (Sync)"
        )

    def test_MFC_control(self):
        self.assertEqual(
            SetMFCControl(
                enable=True
            ).to_text(),
            "MFC Control Enable"
        )

    def test_MFCCV(self):
        self.assertEqual(
            SetMFCCV(
                valve=MFCCVs.CV301,
                state=True,
            ).to_text(),
            "Valve ON CV301"
        )

    def test_set_MFC1Flow(self):
        self.assertEqual(
            SetMFC1Flow(
                MFC_flow=1.00,
            ).to_text(),
            "MFC1 Flow Set= 1.00"
        )

    def test_set_MFC2Flow(self):
        self.assertEqual(
            SetMFC2Flow(
                MFC_flow=1.00,
            ).to_text(),
            "MFC2 Flow Set= 1.00"
        )

    def test_select_control_MFC(self):
        self.assertEqual(
            SelectControlMFC(
                MFC=MFCs.MFC1
            ).to_text(),
            "Press Control MFC1"
        )

    def test_select_pressure_gauge(self):
        self.assertEqual(
            SelectPressureGauge(
                gauge=PressureGauges.CDG10,
            ).to_text(),
            "Pressure Gauge CDG10"
        )


    def test_set_pressure(self):
        self.assertEqual(
            SetPressure(
                pressure=1e-1
            ).to_text(),
            "Set Pressure= 1.00E-1"
        )

    def test_pressure_control(self):
        self.assertEqual(
            PressureControl(
                state=False
            ).to_text(),
            "Pressure Control OFF"
        )

    def test_select_coil_position(self):
        self.assertEqual(
            SelectCoilPosition(
                position=CoilPositions.A,
            ).to_text(),
            "Select Coil Position A"
        )
    
    def test_rheed_gun_x_axis(self):
        self.assertEqual(
            SelectRHEEDGunX(
                axis= RHEEDGunXAxes.P1
            ).to_text(),
            "Select RHEED Gun-X axis 1"
        )

    def test_log_interval(self):
        self.assertEqual(
            SetLogInterval(
                time=10
            ).to_text(),
            "Log Interval 10"
        )

    def test_data_logging(self):
        self.assertEqual(
            DataLogging(
                file_name=None,
                state=False,
            ).to_text(),
            "Data Logging OFF"
        )


    def test_comment(self):
        self.assertEqual(
            Comment(
                "something"
            ).to_text(),
            "/ something"
        )


class TestScope(unittest.TestCase):
    def test_loop(self):
        with ForLoop(2) as subloop:
            subloop.add_child( Wait(2) )
        self.assertEqual(
            subloop.to_text(),
            """Loop 2 (0)
| Wait 2 sec (0)
Loop End
"""
        )

    def test_nest(self):
        scope = PascalScope()
        with scope:
            scope.add_child( DataLogging("test.txt", True) )
            scope.add_child( SelectTarget("A", nowait=False) )
            with ForLoop(100) as loop:
                loop.add_child( RotateSample(angle=30, nowait=True, sync=False) )
                with ForLoop(2) as subloop:
                    subloop.add_child( Wait(2) )
                loop.add_child( subloop )
                loop.add_child( Beep() )
                
            
            scope.add_child( loop )
            scope.add_child( TriggerLaser(num_pulse=10, frequency=1, sync=False, nowait=False) )

        self.assertEqual(
            scope.to_text(),
            """Data Logging File=test.txt
Select Target A
Loop 100 (0)
| Rotate Sample 30.00 (Nowait)
| Loop 2 (0)
| | Wait 2 sec (0)
| Loop End
| Beep
Loop End
Trigger Laser N=10 (0) F=1.0
"""
        )
                      
if __name__ == '__main__':
    unittest.main()