import unittest
import tempfile
import os
import time
import threading
import io
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from lumi.config import settings

# Import the classes to test
import sys
sys.path.append('src')
from lumi.pascal.config_reader import (
    ConfigParser, 
    ConfigReader, 
    ConfigReaderConfig, 
    ConfigFileModifyHandler
)


class TestConfigParser(unittest.TestCase):
    """Test cases for ConfigParser class using real PLD config file"""
    
    def setUp(self):
        """Set up test fixtures using real PLD config"""
        # Use the real PLD config file as reference
        self.real_config_path = settings.pascal.config_reader.test.config_path
        
        # Create a sample config based on the real PLD config structure
        self.sample_config = """[MotorSettings]
X1speed=30000    
Y1speed=10000    
X2speed=50000    
Y2speed=500    
CPU_Xspeed=100000    
CPU_Yspeed=50000    
X1accel=10000    
Y1accel=1000    
X2accel=1000    
Y2accel=100    
CPU_Xaccel=50    
CPU_Yaccel=2000    
X1speedmax=100000    
Y1speedmax=100000    
X2speedmax=100000    
Y2speedmax=2000    
CPU_Xspeedmax=500000    
CPU_Yspeedmax=50000    
X1ratio=10000.000000    
Y1ratio=200.000000    
X2ratio=12000.000000    
Y2ratio=1000.000000    
CPU_Xratio=72000.000000    
CPU_Yratio=75000.000000    
X1offset=0    
Y1offset=0    
X2offset=24000    
Y2offset=0    
CPU_Xoffset=72000    
CPU_Yoffset=1500000    
X1twist_speed=1.000000    
X1backlashcancel=10    
Mask1setmax=160.000000    
Mask2setmax=5.000000    
Lenssetmax=34.300000    
TG-Zsetmax=20.000000    
ORGmode=2    

[TGsettings]
TGcurr=0    
TGnum=7    
TGoffset=0    
TGrot=1    
TGbuck=1000    
TG1name="SpotSizeCrys"
TG2name="TbFeO3"
TG3name="La0.7Sr0.3MnO3"
TG4name="GdFeO3"
TG5name="SmFeO3"
TG6name="Target6"
TG7name="Clear"
TG8name="Monitor"
TG1angle=51.500000    
TG2angle=103.000000    
TG3angle=154.500000    
TG4angle=205.500000    
TG5angle=257.000000    
TG6angle=308.500000    
TG7angle=0.000000    
TG8angle=-15.000000    
TG1twist=15.000000    
TG2twist=15.000000    
TG3twist=15.000000    
TG4twist=15.000000    
TG5twist=15.000000    
TG6twist=15.000000    
TG7twist=15.000000    
TG1twistTime=30.000000    
TG2twistTime=30.000000    
TG3twistTime=30.000000    
TG4twistTime=30.000000    
TG5twistTime=30.000000    
TG6twistTime=30.000000    
TG7twistTime=30.000000    
TG_PCD=90.400000    
TG_TRposi=0.000000    
TG_ROTratio=25.000000    
TG1adj=-4.500000    
TG2adj=-4.500000    
TG3adj=-4.500000    
TG4adj=-4.500000    
TG5adj=-4.500000    
TG6adj=0.000000    

[Sample_settings]
Sample_trans=5.000000    
Sample_tempcheck=0.000000    
Mask1retract=0.000000    
Mask2retract=0.000000    
MoveEndDelay(msec)=0    
ZeroAdjust(0:Disable/1:Enable)=0    
SmplShutterClose=5.000000    
SmplShutterOpen=0.000000    
MaskSample0=100.000000    
MaskSample1=122.000000    
MaskSample2=144.000000    
MaskSample3=160.000000    
MaskSample4=160.000000    
MaskSample5=160.000000    

[WarningCheck]
Excimer=10    
HeatingTemp=10    
HeatingCurr=5.000000    
Pressure=5.000000    
MFC1=1.000000    
MFC2=5.000000    
MFC3=5.000000    
Mask1BadPosition=10.000000    

[Password]
Password="PLD"
"""
    
    def test_parse_config_sections(self):
        """Test parsing config sections correctly using PLD config structure"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        expected_sections = ['MotorSettings', 'TGsettings', 'Sample_settings', 'WarningCheck', 'Password']
        self.assertEqual(parser.get_sections(), expected_sections)
    
    def test_parse_motor_settings_section(self):
        """Test parsing MotorSettings section from PLD config"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        motor_config = parser.get_configs_by_section('MotorSettings')
        
        # Test some key motor settings
        self.assertEqual(motor_config['X1speed'], '30000')
        self.assertEqual(motor_config['Y1speed'], '10000')
        self.assertEqual(motor_config['CPU_Xspeed'], '100000')
        self.assertEqual(motor_config['X1ratio'], '10000.000000')
        self.assertEqual(motor_config['Y1ratio'], '200.000000')
        self.assertEqual(motor_config['X1offset'], '0')
        self.assertEqual(motor_config['Y2offset'], '0')
        self.assertEqual(motor_config['CPU_Xoffset'], '72000')
        self.assertEqual(motor_config['CPU_Yoffset'], '1500000')
        self.assertEqual(motor_config['X1twist_speed'], '1.000000')
        self.assertEqual(motor_config['Mask1setmax'], '160.000000')
        self.assertEqual(motor_config['ORGmode'], '2')
    
    def test_parse_tg_settings_section(self):
        """Test parsing TGsettings section from PLD config"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        tg_config = parser.get_configs_by_section('TGsettings')
        
        # Test TG settings
        self.assertEqual(tg_config['TGcurr'], '0')
        self.assertEqual(tg_config['TGnum'], '7')
        self.assertEqual(tg_config['TGbuck'], '1000')
        self.assertEqual(tg_config['TG1name'], '"SpotSizeCrys"')
        self.assertEqual(tg_config['TG2name'], '"TbFeO3"')
        self.assertEqual(tg_config['TG3name'], '"La0.7Sr0.3MnO3"')
        self.assertEqual(tg_config['TG1angle'], '51.500000')
        self.assertEqual(tg_config['TG2angle'], '103.000000')
        self.assertEqual(tg_config['TG1twist'], '15.000000')
        self.assertEqual(tg_config['TG_PCD'], '90.400000')
        self.assertEqual(tg_config['TG1adj'], '-4.500000')
    
    def test_parse_sample_settings_section(self):
        """Test parsing Sample_settings section from PLD config"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        sample_config = parser.get_configs_by_section('Sample_settings')
        
        # Test sample settings
        self.assertEqual(sample_config['Sample_trans'], '5.000000')
        self.assertEqual(sample_config['Sample_tempcheck'], '0.000000')
        self.assertEqual(sample_config['MoveEndDelay(msec)'], '0')
        self.assertEqual(sample_config['ZeroAdjust(0:Disable/1:Enable)'], '0')
        self.assertEqual(sample_config['SmplShutterClose'], '5.000000')
        self.assertEqual(sample_config['MaskSample0'], '100.000000')
        self.assertEqual(sample_config['MaskSample1'], '122.000000')
        self.assertEqual(sample_config['MaskSample5'], '160.000000')
    
    def test_parse_warning_check_section(self):
        """Test parsing WarningCheck section from PLD config"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        warning_config = parser.get_configs_by_section('WarningCheck')
        
        # Test warning check settings
        self.assertEqual(warning_config['Excimer'], '10')
        self.assertEqual(warning_config['HeatingTemp'], '10')
        self.assertEqual(warning_config['HeatingCurr'], '5.000000')
        self.assertEqual(warning_config['Pressure'], '5.000000')
        self.assertEqual(warning_config['MFC1'], '1.000000')
        self.assertEqual(warning_config['MFC2'], '5.000000')
        self.assertEqual(warning_config['MFC3'], '5.000000')
        self.assertEqual(warning_config['Mask1BadPosition'], '10.000000')
    
    def test_parse_password_section(self):
        """Test parsing Password section from PLD config"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        password_config = parser.get_configs_by_section('Password')
        
        # Test password setting
        self.assertEqual(password_config['Password'], '"PLD"')
    
    def test_get_config_specific_key(self):
        """Test getting specific config key from PLD config"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        # Test various key types from PLD config
        self.assertEqual(parser.get_config('MotorSettings', 'X1speed'), '30000')
        self.assertEqual(parser.get_config('MotorSettings', 'X1ratio'), '10000.000000')
        self.assertEqual(parser.get_config('TGsettings', 'TG1name'), '"SpotSizeCrys"')
        self.assertEqual(parser.get_config('TGsettings', 'TG1angle'), '51.500000')
        self.assertEqual(parser.get_config('Sample_settings', 'Sample_trans'), '5.000000')
        self.assertEqual(parser.get_config('WarningCheck', 'MFC1'), '1.000000')
        self.assertEqual(parser.get_config('Password', 'Password'), '"PLD"')
    
    def test_get_all_config(self):
        """Test getting all config content from PLD config"""
        config_file = io.StringIO(self.sample_config)
        parser = ConfigParser(config_file)
        
        all_config = parser.get_all_configs()
        self.assertIn('MotorSettings', all_config)
        self.assertIn('TGsettings', all_config)
        self.assertIn('Sample_settings', all_config)
        self.assertIn('WarningCheck', all_config)
        self.assertIn('Password', all_config)
        self.assertEqual(len(all_config), 5)
        
        # Verify some specific values are present
        self.assertEqual(all_config['MotorSettings']['X1speed'], '30000')
        self.assertEqual(all_config['TGsettings']['TG1name'], '"SpotSizeCrys"')
        self.assertEqual(all_config['Password']['Password'], '"PLD"')
    
    def test_empty_config(self):
        """Test parsing empty config file"""
        config_file = io.StringIO("")
        parser = ConfigParser(config_file)
        
        self.assertEqual(parser.get_sections(), [])
        self.assertEqual(parser.get_all_configs(), {})
    
    def test_config_with_only_sections(self):
        """Test config with only section headers"""
        config_content = """[MotorSettings]
[TGsettings]
[Sample_settings]
[WarningCheck]
"""
        config_file = io.StringIO(config_content)
        parser = ConfigParser(config_file)
        
        expected_sections = ['MotorSettings', 'TGsettings', 'Sample_settings', 'WarningCheck']
        self.assertEqual(parser.get_sections(), expected_sections)
        
        # Each section should be empty
        for section in expected_sections:
            self.assertEqual(parser.get_configs_by_section(section), {})
    
    def test_real_pld_config_file(self):
        """Test parsing the actual PLD config file"""
        if os.path.exists(self.real_config_path):
            with open(self.real_config_path, 'r') as f:
                config_file = io.StringIO(f.read())
            
            parser = ConfigParser(config_file)
            
            # Test that all expected sections are present
            sections = parser.get_sections()
            expected_sections = [
                'MotorSettings', 'TGsettings', 'Sample_settings', 'CCDsettings', 
                'PIDsettings', 'CommSettings', 'GaugeSettings', 'PlotSettings', 
                'MFCsettings', 'WarningCheck', 'Password', 'LaserGatesettings', 
                'ATTsettings', 'HomingParameters', 'JogParameters', 'PyroCommParameters', 
                'Language', 'PressureSpikeCancel', 'RHEEDcoilPosition', 'RHEEDcoilOffset', 
                'RHEED_Gun_X-axis'
            ]
            
            for section in expected_sections:
                self.assertIn(section, sections, f"Section {section} not found in real config")
            
            # Test some specific values from the real config
            motor_config = parser.get_configs_by_section('MotorSettings')
            self.assertEqual(motor_config['X1speed'], '30000')
            self.assertEqual(motor_config['Y1speed'], '10000')
            self.assertEqual(motor_config['CPU_Xspeed'], '100000')
            
            tg_config = parser.get_configs_by_section('TGsettings')
            self.assertEqual(tg_config['TG1name'], '"SpotSizeCrys"')
            self.assertEqual(tg_config['TG2name'], '"TbFeO3"')
            self.assertEqual(tg_config['TG1angle'], '51.500000')
            
            warning_config = parser.get_configs_by_section('WarningCheck')
            self.assertEqual(warning_config['MFC1'], '1.000000')
            self.assertEqual(warning_config['MFC2'], '5.000000')
            
            password_config = parser.get_configs_by_section('Password')
            self.assertEqual(password_config['Password'], '"PLD"')
        else:
            self.skipTest(f"Real PLD config file not found at {self.real_config_path}")


class TestConfigReaderConfig(unittest.TestCase):
    """Test cases for ConfigReaderConfig dataclass"""
    
    def test_default_values(self):
        """Test default values of ConfigReaderConfig"""
        config = ConfigReaderConfig()
        self.assertEqual(config.idle_time, 0.01)
        self.assertEqual(config.config_path, "")
    
    def test_custom_values(self):
        """Test custom values of ConfigReaderConfig"""
        config = ConfigReaderConfig(idle_time=0.1, config_path="/path/to/config.ini")
        self.assertEqual(config.idle_time, 0.1)
        self.assertEqual(config.config_path, "/path/to/config.ini")


class TestConfigFileModifyHandler(unittest.TestCase):
    """Test cases for ConfigFileModifyHandler class"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.mock_config_reader = Mock()
        self.handler = ConfigFileModifyHandler(self.mock_config_reader)
    
    def test_on_modified_file(self):
        """Test on_modified method for file events"""
        mock_event = Mock()
        mock_event.is_directory = False
        mock_event.src_path = "/path/to/config.ini"
        
        self.handler.on_modified(mock_event)
        
        self.mock_config_reader.update_config_file.assert_called_once_with("/path/to/config.ini")
    
    def test_on_modified_directory(self):
        """Test on_modified method ignores directory events"""
        mock_event = Mock()
        mock_event.is_directory = True
        mock_event.src_path = "/path/to/directory"
        
        self.handler.on_modified(mock_event)
        
        self.mock_config_reader.update_config_file.assert_not_called()


class TestConfigReader(unittest.TestCase):
    """Test cases for ConfigReader class using PLD config"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "test_config.ini")
        self.config_path_2 = os.path.join(self.temp_dir, "test_config_2.ini")
        
        # Create a sample config file based on PLD config structure
        self.sample_config = """[MotorSettings]
X1speed=30000    
Y1speed=10000    
X2speed=50000    
Y2speed=500    
CPU_Xspeed=100000    
CPU_Yspeed=50000    

[TGsettings]
TGcurr=0    
TGnum=7    
TGoffset=0    
TGrot=1    
TGbuck=1000    
TG1name="SpotSizeCrys"
TG2name="TbFeO3"
TG1angle=51.500000    
TG2angle=103.000000    

[WarningCheck]
Excimer=10    
HeatingTemp=10    
HeatingCurr=5.000000    
Pressure=5.000000    
MFC1=1.000000    
MFC2=5.000000    
MFC3=5.000000    
Mask1BadPosition=10.000000    

[Password]
Password="PLD"
"""
        with open(self.config_path, 'w') as f:
            f.write(self.sample_config)
        
        self.config = ConfigReaderConfig(
            idle_time=0.01,
            config_path=self.config_path
        )
        
        self.sample_config_2 = """[MotorSettings]
X1speed=20000    
Y1speed=10000    
X2speed=50000    
Y2speed=100    
CPU_Xspeed=100000    
CPU_Yspeed=20000    

[TGsettings]
TGcurr=0    
TGnum=7    
TGoffset=0    
TGrot=1    
TGbuck=1000    
TG1name="SpotSizeCrys"
TG2name="TbFeO3"
TG1angle=51.500000    
TG2angle=103.000000    

[WarningCheck]
Excimer=10    
HeatingTemp=10    
HeatingCurr=5.000000    
Pressure=5.000000    
MFC1=1.000000    
MFC2=5.000000    
MFC3=5.000000    
Mask1BadPosition=10.000000    

[Password]
Password="PLD"
"""
        with open(self.config_path_2, 'w') as f:
            f.write(self.sample_config_2)

    def test_initialization(self):
        """Test ConfigReader initialization"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        
        self.assertEqual(reader.config, self.config)
        self.assertEqual(reader.name, "test_reader")
        self.assertTrue(reader.daemon)
        self.assertIsNone(reader._config_file_path)
        self.assertIsNone(reader._config_reader)
    
    def test_open_reader_success(self):
        """Test successful opening of config file with PLD config"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        
        reader.open_reader(self.config_path, 5)
        
        self.assertIsNotNone(reader._config_reader)
        self.assertIsInstance(reader._config_reader, ConfigParser)
        
        # Verify config content matches PLD structure
        config_content = reader._config_reader.get_all_configs()
        self.assertIn('MotorSettings', config_content)
        self.assertIn('TGsettings', config_content)
        self.assertIn('WarningCheck', config_content)
        self.assertIn('Password', config_content)
        
        # Test specific PLD config values
        self.assertEqual(config_content['MotorSettings']['X1speed'], '30000')
        self.assertEqual(config_content['MotorSettings']['Y1speed'], '10000')
        self.assertEqual(config_content['TGsettings']['TG1name'], '"SpotSizeCrys"')
        self.assertEqual(config_content['TGsettings']['TG2name'], '"TbFeO3"')
        self.assertEqual(config_content['WarningCheck']['MFC1'], '1.000000')
        self.assertEqual(config_content['Password']['Password'], '"PLD"')
    
    def test_open_reader_timeout(self):
        """Test timeout when config file doesn't exist"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        non_existent_path = "/non/existent/path/config.ini"
        
        with self.assertRaises(TimeoutError):
            reader.open_reader(non_existent_path, 0.1)
    
    def test_close_reader(self):
        """Test closing the config reader"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        
        # First open a reader
        reader.open_reader(self.config_path, 5)
        self.assertIsNotNone(reader._config_reader)
        
        # Then close it
        reader.close_reader()
        self.assertIsNone(reader._config_reader)
        self.assertIsNone(reader._config_file)
    
    def test_update_config_file(self):
        """Test updating config file path"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        
        reader.update_config_file(self.config_path_2)
        
        self.assertEqual(reader._config_file_path, self.config_path_2)
    
    def test_get_all_configs(self):
        """Test getting config content with PLD config"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        reader.open_reader(self.config_path, 5)
        
        config_content = reader.get_all_configs()
        
        self.assertIn('MotorSettings', config_content)
        self.assertIn('TGsettings', config_content)
        self.assertIn('WarningCheck', config_content)
        self.assertIn('Password', config_content)
        
        # Test specific PLD config values
        self.assertEqual(config_content['MotorSettings']['X1speed'], '30000')
        self.assertEqual(config_content['TGsettings']['TG1name'], '"SpotSizeCrys"')
        self.assertEqual(config_content['WarningCheck']['MFC1'], '1.000000')
        self.assertEqual(config_content['Password']['Password'], '"PLD"')
    
    def test_hold_and_resume(self):
        """Test hold and resume functionality"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        
        # Test hold
        reader.hold()
        self.assertTrue(reader._hold_event.is_set())
        
        # Test resume
        reader.resume()
        self.assertFalse(reader._hold_event.is_set())
    
    def test_stop(self):
        """Test stopping the reader"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        
        # Mock the observer
        reader._observer = Mock()
        
        reader.stop()
        
        self.assertTrue(reader._stop_event.is_set())
        reader._observer.stop.assert_called_once()
        reader._observer.join.assert_called_once()
    
    @patch('lumi.pascal.config_reader.Observer')
    def test_create_file_watcher(self, mock_observer_class):
        """Test creating file watcher"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        mock_observer = Mock()
        mock_observer_class.return_value = mock_observer
        
        reader.create_file_watcher()
        
        self.assertEqual(reader._observer, mock_observer)
        mock_observer.schedule.assert_called_once()
        mock_observer_class.assert_called_once()
    
    @patch('lumi.pascal.config_reader.Observer')
    def test_run_method(self, mock_observer_class):
        """Test the run method (basic functionality)"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        mock_observer = Mock()
        mock_observer_class.return_value = mock_observer
        
        # Mock the create_file_watcher method
        reader.create_file_watcher = Mock()
        reader._observer = mock_observer
        
        # Start the thread
        reader.start()
        
        # Give it a moment to start
        time.sleep(0.1)
        
        # Stop the thread
        reader.stop()
        reader.join(timeout=1)
        
        # Verify observer was started
        mock_observer.start.assert_called_once()
    
    def test_real_pld_config_file_reading(self):
        """Test reading the actual PLD config file"""
        real_config_path = "src/lumi/pascal/assets/PLDconfig_07102025.ini"
        
        if os.path.exists(real_config_path):
            # Copy the real config file to temp directory
            temp_config_path = os.path.join(self.temp_dir, "PLDconfig_07102025.ini")
            shutil.copy2(real_config_path, temp_config_path)
            
            reader = ConfigReader(self.config, "test_reader", daemon=True)
            
            # Update config path to use the copied real file
            reader.config.config_path = temp_config_path
            
            reader.open_reader(temp_config_path, 5)
            
            config_content = reader.get_all_configs()
            
            # Verify all major sections are present
            expected_sections = [
                'MotorSettings', 'TGsettings', 'Sample_settings', 'CCDsettings', 
                'PIDsettings', 'CommSettings', 'GaugeSettings', 'PlotSettings', 
                'MFCsettings', 'WarningCheck', 'Password'
            ]
            
            for section in expected_sections:
                self.assertIn(section, config_content, f"Section {section} not found")
            
            # Test specific values from real PLD config
            self.assertEqual(config_content['MotorSettings']['X1speed'], '30000')
            self.assertEqual(config_content['MotorSettings']['Y1speed'], '10000')
            self.assertEqual(config_content['TGsettings']['TG1name'], '"SpotSizeCrys"')
            self.assertEqual(config_content['TGsettings']['TG2name'], '"TbFeO3"')
            self.assertEqual(config_content['WarningCheck']['MFC1'], '1.000000')
            self.assertEqual(config_content['Password']['Password'], '"PLD"')
        else:
            self.skipTest(f"Real PLD config file not found at {real_config_path}")

    def tearDown(self):
        """Clean up test fixtures"""
        shutil.rmtree(self.temp_dir)


class TestConfigReaderIntegration(unittest.TestCase):
    """Integration tests for ConfigReader using PLD config"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "test_config.ini")
        
        self.config = ConfigReaderConfig(
            idle_time=0.01,
            config_path=self.config_path  # Watch the directory
        )

        self.sample_config = """[MotorSettings]
X1speed=30000    
Y1speed=10000    
X2speed=50000    
Y2speed=500    
CPU_Xspeed=100000    
CPU_Yspeed=50000    

[TGsettings]
TGcurr=0    
TGnum=7    
TGoffset=0    
TGrot=1    
TGbuck=1000    
TG1name="SpotSizeCrys"
TG2name="TbFeO3"
TG1angle=51.500000    
TG2angle=103.000000    

[WarningCheck]
Excimer=10    
HeatingTemp=10    
HeatingCurr=5.000000    
Pressure=5.000000    
MFC1=1.000000    
MFC2=5.000000    
MFC3=5.000000    
Mask1BadPosition=10.000000    

[Password]
Password="PLD"
"""
        with open(self.config_path, 'w') as f:
            f.write(self.sample_config)


    def tearDown(self):
        """Clean up test fixtures"""
        shutil.rmtree(self.temp_dir)
        
    def test_file_modification_detection_with_pld_config(self):
        """Test that file modifications are detected with PLD config"""
        reader = ConfigReader(self.config, "test_reader", daemon=True)
        
        # Mock the update_config_file method
        # reader.update_config_file = Mock()
        
        # Start the reader
        reader.start()
        time.sleep(0.1)

        content_before = reader.get_all_configs()
        
        # Create a config file with PLD structure
        config_content = """[MotorSettings]
X1speed=30000    
Y1speed=10000    

[TGsettings]
TGcurr=0    
TG1name="SpotSizeCrys"

[WarningCheck]
MFC1=1.000000    
"""
        with open(self.config_path, 'w') as f:
            f.write(config_content)
        
        # Give time for file system events
        time.sleep(0.2)

        content_after = reader.get_all_configs()

        self.assertNotEqual(content_before, content_after)
        
        # Stop the reader
        reader.stop()
        reader.join(timeout=1)
        
        # The update_config_file should have been called
        # Note: This might not work reliably on all systems due to file system event timing
        # In a real scenario, you might need to adjust timing or use different testing strategies


if __name__ == '__main__':
    unittest.main()
