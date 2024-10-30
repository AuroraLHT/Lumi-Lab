from pathlib import Path
import shutil

ASSIT_FILE = Path("C:\\Users\\Public\\MIMode\\MItest.txt")
# ASSIT_FILE_COMPLETE = Path("C:\\Users\\Public\\MIMode\\Completed_MItest.txt")
# ASSIT_FILE_ABORT = Path("C:\\Users\\Public\\MIMode\\Aborted_MItest.txt")
PROGRAM_FILE = Path("C:\\Users\\Public\\MIMode\\Depo_Sample")

PREFIXS = [
    "Completed_",
    "Aborted_",
    "Load_",
    "Loaded_",
    "Running_",
]

TEMPLATE_PATH = Path("C:\\Users\Public\\MIMode\\template")
TEMPLATE_ASSIT = TEMPLATE_PATH / "MItest.txt"
TEMPLATE_PROGRAM = TEMPLATE_PATH / "Depo_Sample"

if __name__ == "__main__":
    while input("continue? y or n: ") == "y":
        for prefix in PREFIXS:
            assit_file_with_prefix =  ASSIT_FILE.parent / (prefix + ASSIT_FILE.name)
            if assit_file_with_prefix.exists():
                assit_file_with_prefix.unlink()
                print(assit_file_with_prefix, " removed")

            else:
                print(assit_file_with_prefix, "not removed")

        shutil.copy(TEMPLATE_PROGRAM, PROGRAM_FILE)
        shutil.copy(TEMPLATE_ASSIT, ASSIT_FILE)
        
