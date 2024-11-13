from lumi.pascal.mi_mode import MIModeBackendSimulator, MIModeServerConfig, MIModeServer
import lumi.pascal.command as command

from pathlib import Path
import time
import uuid

import logging
logging.basicConfig(level=logging.INFO)


def main():
    MI_FOLDER = Path(__file__).parent / "mi_mode_test"
    MI_FOLDER.mkdir(exist_ok=True)

    mi_server_config = MIModeServerConfig(mi_folder=MI_FOLDER, assist_file_name="assist.txt")
    mi_server = MIModeServer(mi_server_config, name="mi_server", daemon=True)
    mi_backend_simulator = MIModeBackendSimulator(mi_server_config)

    print("start mi server")
    mi_server.start()
    time.sleep(1)

    # start backend simulator
    print("start backend simulator")
    mi_backend_simulator.start()

    history = []

    scope = command.PascalScope()
    with scope:
        scope.add_child( command.DataLogging("test.txt", True) )
        scope.add_child( command.SelectTarget("A", nowait=False) )
        with command.ForLoop(100) as loop:
            loop.add_child( command.RotateSample(angle=30, nowait=True, sync=False) )
            with command.ForLoop(2) as subloop:
                subloop.add_child( command.Wait(2) )
            loop.add_child( subloop )
            loop.add_child( command.Beep() )
            
        
        scope.add_child( loop )
        scope.add_child( command.TriggerLaser(num_pulse=10, frequency=1, sync=False, nowait=False) )

    commands = str(scope)
    commands_uuid = str(uuid.uuid4())
    history.append((commands, commands_uuid) )
    mi_server.register_commands( commands, commands_uuid )

    scope = command.PascalScope()
    with scope:
        scope.add_child( command.MoveMask(mask_id=1, distance=10, sync=False, nowait=False) )
        scope.add_child( command.Beep() )
        
    commands = str(scope)
    commands_uuid = str(uuid.uuid4())
    history.append((commands, commands_uuid) )
    mi_server.register_commands( commands, commands_uuid )

    while len(mi_server.mi_execution_history) == 0:
        time.sleep(0.1)

    print("mi history length changed")

    while not (mi_server.mi_execution_history[-1].is_execution_finished is True and len(mi_server.mi_execution_history) == len(history) ):
        time.sleep(0.1)
        # print(mi_server.mi_execution_history[-1].is_execution_finished, len(mi_server.mi_execution_history), len(history))
    print("mi history length match with our input")

    # print(mi_backend_simulator.get_existing_script())
    while mi_backend_simulator.get_existing_script() is not None:
        # print("waiting for mi backend simulator to generate scripts", mi_backend_simulator.get_existing_script())
        time.sleep(0.1)
    print("mi backend simulator generated scripts is all cleared")

    mi_server.stop()
    print("stop mi server")
    mi_backend_simulator.stop()
    print("stop backend simulator")

    mi_server.join()
    mi_backend_simulator.join()



if __name__ == "__main__":
    main()