import subprocess
import time
import psutil
import os
from pathlib import Path


def is_process_running(process):
    return process.poll() is None

def start_node(node_file):
    return subprocess.Popen(['python', node_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def start_api_node(node_file):
    return subprocess.Popen(['fastapi', 'run', node_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def test_launch_nodes():
    nodes_folder = Path('nodes')
    nodes_folder = (Path(__file__).parent.parent).absolute() / nodes_folder
    print(os.path.abspath(nodes_folder))
    node_files = [f for f in os.listdir(nodes_folder) if f.endswith('.py')]
    processes = []

    try:
        # Start all node processes
        for node_file in node_files:
            if node_file == "api.py":
                process = start_api_node(os.path.join(nodes_folder, node_file))
            else:
                process = start_node(os.path.join(nodes_folder, node_file))
            processes.append((node_file, process))

        # Wait for a short time to allow processes to start
        time.sleep(5)

        # Check if all processes are running
        all_running = all(is_process_running(process) for _, process in processes)

        if all_running:
            print("All nodes successfully launched and running.")
        else:
            print("Some nodes failed to launch or stopped running.")

        # Print status of each node
        for node_file, process in processes:
            status = "Running" if is_process_running(process) else "Stopped"
            print(f"{node_file}: {status}")

    finally:
        # Terminate all processes
        for _, process in processes:
            if is_process_running(process):
                process.terminate()

        # Wait for processes to terminate
        time.sleep(2)

        # Force kill any remaining processes
        for _, process in processes:
            if is_process_running(process):
                process.kill()

if __name__ == "__main__":
    test_launch_nodes()