import csv
import threading
from pathlib import Path
import time
import queue
import collections
from dataclasses import dataclass
import datetime

import logging

from watchdog.observers import Observer
from watchdog.events import LoggingEventHandler, FileSystemEventHandler

from .chamber_log import process_row

class FileModifyHandler(FileSystemEventHandler):
    def __init__(self, log_reader) -> None:
        super().__init__()
        self.log_reader = log_reader

    def on_modified(self, event):
        # Event is modified, you can process it now
        if not event.is_directory:
            print("Watchdog received modified event - % s." % event.src_path)
            # print(event)
            # self.log_reader._log_file_path = event.src_path
            self.log_reader.update_log_file(event.src_path)


@dataclass
class LogReaderConfig:
    queue_size : int = 10
    idle_time : float = 0.01
    log_path : str = ""

class LogReader(threading.Thread):

    def __init__(self, config:LogReaderConfig, name:str, daemon:bool):
        super().__init__(name=name, daemon=daemon)

        self._log_file_path = None
        self._csv_file = None
        self._csv_reader = None
        self._csv_header = None
        self._observer = None

        self.config =  config
        self.queue = queue.Queue(maxsize=config.queue_size)
        self.peek_queue = collections.deque(maxlen=config.queue_size) # this queue for component that need to get the latest image

        self._stop_event = threading.Event()
        self._hold_event = threading.Event()
        self._io_lock = threading.Lock()
        self._reader_lock = threading.Lock()

    def update_log_file(self, log_file_path):
        if log_file_path != self._log_file_path:
            self._log_file_path = log_file_path
            self.close_reader()
            self.open_reader(self._log_file_path, 20)
            
    def create_directory_watcher(self):
        event_handler = FileModifyHandler(self)
        observer = Observer()
        observer.schedule(event_handler, path=self.config.log_path, recursive=True)
        observer.daemon = self.daemon
        self._observer = observer
        logging.info("observer created")


    def close_reader(self):
        with self._reader_lock:
            if self._csv_header is not None:
                self._csv_reader= None
                self._csv_file.close()
                self._csv_file=None            
                self._csv_header=None

    def open_reader(self, file_name, timeout, jump_to_end=True):
        logging.info(f"Open file {file_name}")
        filename = Path(file_name)
        start = time.time()

        while True:
            if filename.exists():
                with self._reader_lock:
                    self._csv_file = filename.open(mode='r')
                    self._csv_reader = csv.DictReader(self._csv_file)
                    self._csv_header = self._csv_reader.fieldnames
                    if jump_to_end:
                        for row in self._csv_reader: pass # quick jump to the last row
                    self._csv_row_readed = 0
                    break
        
            if time.time() - start < timeout:
                logging.info(f"file {filename} does not exist")
                time.sleep(0.1)
            else:
                raise TimeoutError(f"Cannot read the log csv file {file_name}")
            
            
    def run(self):
        """
            The main loop just keep reading row from the reader. reader would be changed if a new log file is detected.
        """
        self.create_directory_watcher()
        self._observer.start()
        logging.info("watch dog started")

        while True:
            # a= time.time()
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : break

            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag : 
                logging.info(f"On hold, sleep for {self.config.idle_time * 10}")
                time.sleep(self.config.idle_time * 10)
                continue
            # b= time.time()

            if self._csv_reader is not None:
                with self._reader_lock:
                    # st = time.time()
                    for row in self._csv_reader:
                        row = process_row(row)

                        content = (row, {})

                        if not self.queue.full():
                            self.queue.put(content)
                        with self._io_lock:
                            self.peek_queue.appendleft(content)

                        self._csv_row_readed += 1
                        # et = time.time()
                        # print("read", round(et - st, 5))

                    # et2 = time.time()
                    # print("total", round(et2 - st, 5))

            else:
                # await asyncio.sleep(1)
                logging.info("empty csv_reader")
                time.sleep(1)
                continue
            # c = time.time()
            # print(b-a, c-a)

    def get_log(self):
        with self._io_lock:
            if len(self.peek_queue):
                # get the latest frame without popping
                return self.peek_queue[0]
            return (None, None)

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

    @property
    def entries(self):
        if self._csv_header is not None:
            return self._csv_header
        else:
            return []
        # row, headers = self.get_log()
        # if row is not None:
        #     return list(row.keys())
        # else:
        #     return []

@dataclass
class TestLogReaderConfig(LogReaderConfig):
    publish_interval : float = 1

class TestLogReader(LogReader):
    def __init__(self, config: TestLogReaderConfig, name: str, daemon: bool):
        super().__init__(config, name, daemon)

    def get_content(self):
        if self._csv_reader is not None:
            return [ row for row in self._csv_reader ]
        else:
            logging.info(f"Test logger not able to get content from the csv {self._log_file_path}")
            return []
            
    def run(self):
        """
            The main loop just keep reading row from the reader. reader would be changed if a new log file is detected.
        """
        self.open_reader(self.config.log_path, 10, jump_to_end=False)
        simulation_rows = self.get_content()

        logging.info("Test source started")

        while True:
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : 
                logging.info(f"Log reader thread ({self.ident}) exits the run loop")
                break


            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag : 
                logging.info(f"Log reader thread ({self.ident}) on hold, sleep for {self.config.idle_time * 10}")
                time.sleep(self.config.idle_time * 10)
                continue

            with self._reader_lock:
                row = dict( **simulation_rows[self._csv_row_readed % len(simulation_rows)] )
                row = process_row(row)

                content = (row, {})

                if not self.queue.full():
                    self.queue.put(content)
                with self._io_lock:
                    self.peek_queue.appendleft(content)

                self._csv_row_readed += 1
                time.sleep(self.config.publish_interval)

    def stop(self):
        logging.info(f"Log reader thread ({self.ident}) receives a stop signal")
        self._stop_event.set()

