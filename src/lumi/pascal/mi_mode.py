import sys
import logging
from pathlib import Path
import uuid

from watchdog.observers import Observer
from watchdog.events import FileSystemEvent, LoggingEventHandler, FileSystemEventHandler
import datetime

from dataclasses import dataclass
import threading
import queue
import collections
import time
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple, TypedDict, Union
import asyncio

class MIModeResponseHeader(TypedDict):
    pass

class MIState(Enum):
    IDLE = ""
    COMPLETED = "Completed"
    ABORTED = "Aborted"
    LOAD = "Load"
    LOADED = "Loaded"
    RUNNING = "Running"


SCRIPT_PREFIX = "script"


def clean_mi_files(
    mi_folder: Union[str, Path], assit_filename: str, verbose: bool = False
):
    mi_folder = Path(mi_folder)
    for state in MIState:
        assit_file_with_prefix = mi_folder / (f"{state.value}_{assit_filename}")
        if assit_file_with_prefix.exists():
            assit_file_with_prefix.unlink()
            if verbose:
                logging.info(assit_file_with_prefix, " removed")
        else:
            if verbose:
                logging.info(assit_file_with_prefix, "not removed")

    for script_file in mi_folder.glob(f"{SCRIPT_PREFIX}*"):
        script_file.unlink()
        if verbose:
            logging.info(script_file, " removed")


def is_mi_file_trigger(filename: str, base_filename: str) -> bool:
    return filename.endswith(base_filename)


def get_mi_state(filename: str, base_filename: str) -> str:
    assert (
        filename.endswith(base_filename)
    ), f"filename {filename} is not expected given base filename {base_filename}"

    res = filename.split("_")
    if len(res) > 1:
        state, name = res[0], res[1]
    else:
        state, name = "", filename

    state = MIState(state)
    return state


class MIModeExecution:
    def __init__(
        self, uuid: str, commands: str, assist_filename: str, mi_folder: Union[str, Path], on_state_change_callbacks: Dict[str, Callable]
    ) -> None:
        self.state = MIState.IDLE
        self.is_execution_finished = False
        self.is_execution_aborted = False
        self.is_cleaned_up = False
        self.is_stopped = False

        self.uuid = uuid
        self.commands = commands

        self.mi_scripts_name = f"{SCRIPT_PREFIX}_{uuid}"
        self.assist_filename = assist_filename
        self.mi_folder = Path(mi_folder)

    def change_state(self, state: MIState):
        self.state = state
        logging.info(f"mi execution state changed to {state}")

        if self.state == MIState.COMPLETED:
            self.is_execution_finished = True
        if self.state == MIState.ABORTED:
            self.is_execution_finished = True
            self.is_execution_aborted = True

    @property
    def script_file(self):
        return Path(self.mi_folder) / self.mi_scripts_name

    @property
    def assist_file(self):
        return Path(self.mi_folder) / self.assist_filename
 
    @property
    def completed_assist_file(self):
        return Path(self.mi_folder) / f"{MIState.COMPLETED.value}_{self.assist_filename}"

    @property
    def aborted_assist_file(self):
        return Path(self.mi_folder) / f"{MIState.ABORTED.value}_{self.assist_filename}"


    def generate_scripts(self):
        script_file = self.script_file
        assit_file = self.assist_file

        with script_file.open("w") as f:
            f.write(self.commands)

        with assit_file.open("w") as f:
            f.write(str(self.script_file) + "\r\n")

    def stop(self):
        script_file = self.script_file
        assit_file = self.assist_file
        script_file.unlink()
        assit_file.unlink()

        with script_file.open("w") as f:
            f.write("\r\n")

        with assit_file.open("w") as f:
            f.write(str(self.script_file) + "\r\n")

        self.is_stopped = True
        self.is_execution_finished = True

    def clean_up(self):
        self.is_cleaned_up = True
        script_file = self.script_file
        assit_file = self.assist_file
        completed_assist_file = self.completed_assist_file
        aborted_assist_file = self.aborted_assist_file

        if script_file.exists():
            script_file.unlink()
        if assit_file.exists():
            assit_file.unlink()
        if completed_assist_file.exists():
            completed_assist_file.unlink()
        if aborted_assist_file.exists():
            aborted_assist_file.unlink()

    def to_dict(self):
        return {
            "commands": self.commands,
            "commands_uuid": self.uuid,
            "state": self.state.name,
            "is_execution_finished": self.is_execution_finished,
            "is_aborted": self.is_execution_aborted,
            "is_cleaned_up": self.is_cleaned_up,
            "is_stopped": self.is_stopped,
        }


# TODO: we need to make sure the mi_server is not deleted before the mi_execution and mi_execution should be available for the mi_file_system_handler


class MIModeFileSystemHandler(FileSystemEventHandler):
    def __init__(self, based_filename: str, mi_server: "MIModeServer") -> None:
        super().__init__()
        self.based_filename = based_filename
        self.mi_server = mi_server

    def on_any_event(self, event: FileSystemEvent) -> None:
        # print(f"on event: {event}")
        pass

    @property
    def mi_execution(self):
        return self.mi_server.current_mi_execution

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        src_filename = Path(event.src_path).name
        dest_filename = Path(event.dest_path).name
        print(f"on moved: {src_filename} -> {dest_filename}")

        if is_mi_file_trigger(filename=src_filename, base_filename=self.based_filename):
            logging.info(f"assist file state move {get_mi_state(filename=src_filename, base_filename=self.based_filename)} -> {get_mi_state(filename=dest_filename, base_filename=self.based_filename)}" )
            if self.mi_execution is not None:
                self.mi_server.change_current_execution_state(
                    get_mi_state(
                        filename=dest_filename, base_filename=self.based_filename
                    )
                )
            else:
                logging.warning(
                    f"mi_execution is not set when assist file state is changed to {get_mi_state(filename=dest_filename, base_filename=self.based_filename)}"
                )

    def on_modified(self, event):
        if event.is_directory:
            return

        src_filename = Path(event.src_path).name
        print(f"on modified: {src_filename}")
        if is_mi_file_trigger(filename=src_filename, base_filename=self.based_filename):
            logging.info(f"assist file modified to state -> {get_mi_state(filename=src_filename, base_filename=self.based_filename)}" )
            if self.mi_execution is not None:
                self.mi_server.change_current_execution_state(
                    get_mi_state(
                        filename=src_filename, base_filename=self.based_filename
                    )
                )
            else:
                logging.warning(
                    f"mi_execution is not set when assist file state is changed to {get_mi_state(filename=src_filename, base_filename=self.based_filename)}"
                )


@dataclass
class MIModeServerConfig:
    queue_size: int = 1000
    idle_time: float = 0.01
    assist_file_name: str = "MItest.txt"
    mi_folder: str = ""


class MIModeServer(threading.Thread):
    current_mi_execution: MIModeExecution
    queue: "queue.Queue"
    update_queue: "queue.Queue"

    executions: collections.OrderedDict[str, MIModeExecution]
    execution_callbacks: Dict[str, Callable]
    command_futures: Dict[str, asyncio.Future]

    def __init__(self, config: MIModeServerConfig, name: str, daemon: bool):
        super().__init__(name=name, daemon=daemon)

        self._observer = None

        self.config = config
        
        self.queue = queue.Queue(maxsize=config.queue_size)
        self.update_queue = queue.Queue(maxsize=config.queue_size)

        self.executions = collections.OrderedDict()
        self.command_futures = {}
        # self.output_queue = queue.Queue(maxsize=config.queue_size)
        # self.peek_queue = collections.deque(maxlen=config.queue_size) # this queue for component that need to get the latest commands

        self._stop_event = threading.Event()
        self._hold_event = threading.Event()
        self._io_lock = threading.Lock()
        self._reader_lock = threading.Lock()

        self.current_mi_execution = None
        self.mi_execution_history = collections.deque(maxlen=5)
        self.execution_callbacks = {}

        self.register_execution_callback(self.default_execution_callback)

    def clear_queue(self):
        while not self.queue.empty():
            self.queue.get()

        while not self.update_queue.empty():
            self.update_queue.get()            

    def stop_execution(self):
        if self.current_mi_execution is not None:
            self.current_mi_execution.stop()

    def register_execution_callback(self, execution_callback: Callable[[MIModeExecution], None]):
        """
        Register a callback function that will be called at each step of the execution.
        The callback function will receive one argument:
        - mi_execution: the current mi execution
        """
        callback_uuid = str(uuid.uuid4())
        self.execution_callbacks[ callback_uuid ] = execution_callback
        return callback_uuid

    @staticmethod
    def default_execution_callback(mi_server: "MIModeServer", mi_execution: MIModeExecution):
        if not mi_server.update_queue.full():
            mi_server.update_queue.put( ( mi_execution.to_dict(), {"update_content": "current_execution"} ) )
            
        if not mi_server.update_queue.full():
            mi_server.update_queue.put( ( mi_server.list_executions(), {"update_content": "all_executions"} ) )

    def register_commands(self, commands: str, commands_uuid: str):
        logging.info(f"register commands_uuid: {commands_uuid}")
        if commands.startswith("$"):
            special_commands = commands[1:]
            if special_commands == "stop":
                self.clear_queue()
                self.stop_execution()
            elif special_commands == "clean":
                clean_mi_files(self.config.mi_folder, self.config.assist_file_name)
        else:
            # self.queue.put((commands, commands_uuid))
            self.queue.put(commands_uuid)
            mi_execution = self.create_mi_execution(commands=commands, commands_uuid=commands_uuid)
            self.executions[commands_uuid] = mi_execution

        command_future = asyncio.Future()
        self.command_futures[ commands_uuid ] = command_future
        return command_future

    def get_next_execution(self, timeout:float):
        if self.queue.empty():
            return None
        commands_uuid = self.queue.get(block=False, timeout=timeout)
        return self.executions[commands_uuid]

    def start_next_execution(self, timeout:float):
        next_execution = self.get_next_execution(timeout=timeout)
        if next_execution is not None:
            next_execution.generate_scripts()
            self.current_mi_execution = next_execution
            self.mi_execution_history.append(self.current_mi_execution)

    def change_current_execution_state(self, state: MIState):
        if self.current_mi_execution is not None:
            self.current_mi_execution.change_state(state)

        for callback in self.execution_callbacks.values():
            callback(self, self.current_mi_execution)

    def execution_finished(self, mi_execution: MIModeExecution):
        # self.output_queue.put(
        # )
        result = mi_execution.to_dict()

        future = self.command_futures.pop(mi_execution.uuid, None)
        if future:
            future.set_result(result)

        self.clean_up_mi_execution(mi_execution=mi_execution)

    def create_mi_watcher(self):
        event_handler = MIModeFileSystemHandler(self.config.assist_file_name, self)
        observer = Observer()
        observer.schedule(event_handler, path=self.config.mi_folder, recursive=True)
        observer.daemon = self.daemon
        self._observer = observer
        logging.info("observer created")

    def is_mi_execution_running(self):
        return self.current_mi_execution is not None

    def wait_for_execution_finished(self):
        if self.current_mi_execution is None:
            return True
        elif self.current_mi_execution.is_execution_finished:
            return True
        else:
            time.sleep(self.config.idle_time)
            return False

    def create_mi_execution(self, commands, commands_uuid):
        mi_execution = MIModeExecution(
            uuid=commands_uuid, 
            commands=commands, 
            assist_filename=self.config.assist_file_name, 
            mi_folder=self.config.mi_folder,
            on_state_change_callbacks=self.execution_callbacks,
        )
        self.executions[commands_uuid] = mi_execution
        return mi_execution

    def clean_up_mi_execution(self, mi_execution:MIModeExecution):
        if mi_execution is not None:
            mi_execution.clean_up()
        self.executions.pop(mi_execution.uuid)

    # def clean_up_mi_execution(self):
    #     if self.mi_execution is not None:
    #         self.mi_execution.clean_up()
    #         self.mi_execution = None

    def run(self):
        """
        The main loop just keep reading row from the reader. reader would be changed if a new log file is detected.
        """

        # clean up the mi folder and remove all the previous mi files
        clean_mi_files(self.config.mi_folder, self.config.assist_file_name)

        # create the mi watcher to monitor the mi mode backend state
        self.create_mi_watcher()
        self._observer.start()
        logging.info("watch dog started")

        while True:
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag:
                break

            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag:
                logging.info(f"On hold, sleep for {self.config.idle_time * 10}")
                time.sleep(self.config.idle_time * 10)
                continue

            if self.is_mi_execution_running():
                if self.wait_for_execution_finished():
                    logging.info(f"execution finished: {self.current_mi_execution.script_file}")
                    self.execution_finished(self.current_mi_execution)
                    self.current_mi_execution = None

            else:
                self.start_next_execution(timeout=0.1)

    def get_latest_execution(self) -> MIModeExecution:
        if len(self.mi_execution_history) > 0:
            return self.mi_execution_history[-1]
        else:
            return MIModeExecution(uuid="", commands="", assist_filename=self.config.assist_file_name, mi_folder=self.config.mi_folder, on_state_change_callbacks={})

    def hold(self):
        logging.info(f"MI Mode thread ({self.ident}) receives a hold signal")
        self._hold_event.set()

    def resume(self):
        logging.info(f"MI Mode thread ({self.ident}) receives a resume signal")
        self._hold_event.clear()

    def stop(self):
        logging.info(f"MI Mode thread ({self.ident}) receives a stop signal")
        self._stop_event.set()
        self._observer.stop()
        self._observer.join()

    def list_executions(self) -> List[Dict]:
        return [ v.to_dict() for k, v in self.executions.items() ]


class MIModeBackendSimulator(threading.Thread):
    def __init__(self, mi_server_config: MIModeServerConfig):
        super().__init__(name="MIModeBackendSimulator", daemon=True)
        self.mi_server_config = mi_server_config

        self.assist_file_path = (
            Path(self.mi_server_config.mi_folder)
            / self.mi_server_config.assist_file_name
        )

        self.mi_folder = Path(self.mi_server_config.mi_folder)

        self.states = [
            ("Load", 0.1),
            ("Loaded", 0.3),
            ("Running", 1),
            ("Completed", 0.1),
            # "Aborted",
        ]

        self._stop_event = threading.Event()

    def get_existing_script(self):
        files = list(self.mi_folder.glob(f"{SCRIPT_PREFIX}*"))
        if len(files) == 1:
            return files[0].name
        elif len(files) > 1:
            logging.warning(f"multiple scripts are found in {self.mi_folder}")
            return files
        else:
            return None

    def run(self):
        while True:
            stop_flag = self._stop_event.wait(0.1)
            if stop_flag:
                break

            if self.assist_file_path.exists():
                prev_assist_path = self.assist_file_path
                for state, sleep_time in self.states:
                    path = (
                        prev_assist_path.parent
                        / f"{state}_{self.assist_file_path.name}"
                    )
                    prev_assist_path.rename(path)
                    prev_assist_path = path
                    time.sleep(sleep_time)
                logging.info(f"Simulator: executed scripts: {self.get_existing_script()}")

    def stop(self):
        self._stop_event.set()
