import csv
import threading
from pathlib import Path
import time
import queue
import collections
from dataclasses import dataclass
import datetime
import io
import logging
import ast

from watchdog.observers import Observer
from watchdog.events import LoggingEventHandler, FileSystemEventHandler

from .chamber_log import process_row
from typing import TypedDict

class ConfigFileModifyHandler(FileSystemEventHandler):
    def __init__(self, config_reader:"ConfigReader") -> None:
        super().__init__()
        self.config_reader = config_reader

    def on_modified(self, event):
        # Event is modified, you can process it now
        if not event.is_directory:
            if Path(event.src_path).name == "PLDconfig.ini":
                print("Watchdog received modified event - % s." % event.src_path)
                # print(event)
                # self.log_reader._log_file_path = event.src_path
                self.config_reader.update_config_file(event.src_path)
            # else:
            #     print(Path(event.src_path).name)
            #     print(Path(event.src_path))

    # def on_any_event(self, event):
    #     print(f"Watchdog received event {event}")

class ConfigContentHeader(TypedDict):
    pass

class ConfigParser:
    def __init__(self, config_file: io.StringIO):
        self._config_file = config_file
        self._parse_config()
    
    def _parse_config(self):
        self.content = {}
        for line in self._config_file:
            if line.startswith('['):
                section = line.strip()[1:-1]
                self.content[section] = {}
            elif "=" in line:
                key, value = line.strip().split('=', maxsplit=1)
                # value could contain = sign, so we need to split on the first = sign
                self.content[section][key.strip()] = ast.literal_eval(value.strip())
            else:
                # empty line, skip
                pass

    def get_sections(self):
        return list(self.content.keys())

    def get_configs_by_section(self, section: str):
        return self.content[section]

    def get_config(self, section: str, key: str):
        return self.content[section][key]
    
    def get_all_configs(self):
        return self.content

@dataclass
class ConfigReaderConfig:
    idle_time : float = 0.01
    config_path : str = ""

class ConfigReader(threading.Thread):

    def __init__(self, config:ConfigReaderConfig, name:str, daemon:bool):
        super().__init__(name=name, daemon=daemon)

        self._config_file_path = None
        self._config_file = None
        self._config_reader = None
        self._observer = None
        self._entries = None

        self.config =  config

        self._stop_event = threading.Event()
        self._hold_event = threading.Event()
        self._io_lock = threading.Lock()
        self._reader_lock = threading.Lock()

    def update_config_file(self, config_file_path:str):
        # if config_file_path != self._config_file_path:
        self._config_file_path = config_file_path
        # self.close_reader()
        self.open_reader(self._config_file_path, 20)
            
    def create_file_watcher(self):
        event_handler = ConfigFileModifyHandler(self)
        observer = Observer()
        path = str(Path(self.config.config_path).parent)
        # path = str(self.config.config_path)
        observer.schedule(event_handler, path=path, recursive=True)
        observer.daemon = self.daemon
        self._observer = observer
        logging.info(f"Observer created to monitor the config at {self.config.config_path}")

    def close_reader(self):
        with self._reader_lock:
            self._config_reader= None
            self._config_file=None

    def open_reader(self, file_name, timeout):
        logging.info(f"Open config .ini file {file_name}")
        file_name = Path(file_name)
        start = time.time()

        while True:
            if file_name.exists():
                with self._reader_lock:
                    with open(file_name, 'r') as self._config_file:
                        self._config_reader = ConfigParser(self._config_file)
                    break
        
            if time.time() - start < timeout:
                logging.info(f"file {file_name} does not exist")
                time.sleep(0.1)
            else:
                raise TimeoutError(f"Cannot read the config .ini file {file_name}")
            
            
    def run(self):
        """
            The main loop just keep reading row from the reader. reader would be changed if a new log file is detected.
        """
        self.create_file_watcher()
        self._observer.start()
        logging.info("Config .ini file watch dog started")

        self.update_config_file(self.config.config_path)

        while True:
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : break

            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag : 
                logging.info(f"On hold, sleep for {self.config.idle_time * 10}")
                time.sleep(self.config.idle_time * 10)
                continue

    def get_all_configs(self):
        with self._io_lock:
            return self._config_reader.get_all_configs()
        
    def get_configs_by_section(self, section: str):
        with self._io_lock:
            return self._config_reader.get_configs_by_section(section)
        
    def get_sections(self):
        with self._io_lock:
            return {"sections": self._config_reader.get_sections()}
        
    def get_config(self, section: str, key: str):
        with self._io_lock:
            return {"config": self._config_reader.get_config(section, key)}

    def hold(self):
        logging.info(f"Log reader thread ({self.ident}) receives a hold signal")
        self._hold_event.set()

    def resume(self):
        logging.info(f"Log reader thread ({self.ident}) receives a resume signal")
        self._hold_event.clear()
            
    def stop(self):
        logging.info(f"Log reader thread ({self.ident}) receives a stop signal")
        self._stop_event.set()
        self._observer.stop()
        self._observer.join()
