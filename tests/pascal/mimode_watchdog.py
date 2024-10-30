
import sys
import logging
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEvent, LoggingEventHandler, FileSystemEventHandler
import datetime

ASSIT_FILE = Path("C:\\Users\\Public\\MIMode\\MItest.txt")
PROGRAM_FILE = Path("C:\\Users\\Public\\MIMode\\Depo_Sample")

def get_mi_state(filename):
    res = filename.split("_")
    if len(res) > 1:
        state = res[0]
    else:
        state = ""
    return state


class CustomizedFileSystemHandler(FileSystemEventHandler):
    def __init__(self) -> None:
        super().__init__()

    # def on_any_event(self, event: FileSystemEvent) -> None:
    #     print(f"on event: {event}")

    def on_moved(self, event: FileSystemEvent) -> None:
        src_filename = Path(event.src_path).stem
        dest_filename = Path(event.dest_path).stem
        print(datetime.datetime.now().isoformat(), f"assist file state move {get_mi_state(filename=src_filename)} -> {get_mi_state(filename=dest_filename)}" )
        
    def on_modified(self, event):
        # Event is modified, you can process it now
        # if not event.is_directory:
        filename = Path(event.src_path).stem
        print(datetime.datetime.now().isoformat(), f"assist file modified to state -> {get_mi_state(filename=filename)}" )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    # path = sys.argv[1] if len(sys.argv) > 1 else '.'
    path = ASSIT_FILE.parent
    observer = Observer(timeout=0.2)
    # event_handler = LoggingEventHandler()
    # observer.schedule(event_handler, path, recursive=False)

    event_handler = CustomizedFileSystemHandler()
    observer.schedule(event_handler, path, recursive=False)

    observer.start()
    print("Observer started.")

    try:
        while observer.is_alive():
            observer.join(1)
    finally:
        observer.stop()
        observer.join()