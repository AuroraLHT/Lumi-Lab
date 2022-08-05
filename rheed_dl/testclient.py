from pathlib import Path
import requests
import websockets
import numpy as np
from tqdm.asyncio import trange, tqdm
from rhana.io.tokyo_u import RHEEDStreamReader
import asyncio
import json

# rheed_bin_path = Path("/mnt/share/oxide_rheed/HT003-RHEED-0001.bin")
# rheed_dir_path =  Path("/mnt/share/oxide_rheed/HT003-RHEED-0001.dir")

rheed_bin_path = Path("/home/hliang16/projects/LnFeO3/test_data/HT003-RHEED-0001.bin")
rheed_dir_path =  Path("/home/hliang16/projects/LnFeO3/test_data/HT003-RHEED-0001.dir")

reader = RHEEDStreamReader(rheed_dir_path, rheed_bin_path)

n_batch = 4

res = requests.post(
    # url = "http://omicron.issp.u-tokyo.ac.jp:8000/input_config",
    url = "http://127.0.0.1:8000/input_config",    
    json = {
        "batch": n_batch,
        "shape": [600, 800],
        "dtype": "uint16"
    }
)

beams = reader.get_beams()

def pad_for_batching(ls, bs):
    for i in range(bs - len(ls) % bs):
        ls.append(ls[-1])
    return ls

async def main():
        websocket = await websockets.connect('ws://127.0.0.1:8000/ws/unet')
    # async with websockets.connect('ws://127.0.0.1:8000/ws/test2') as websocket:
    # async with websockets.connect('ws://omicron.issp.u-tokyo.ac.jp:8000/ws/unet') as websocket:
        # _batch = []
        counter = 0
        for frame_i in tqdm( pad_for_batching( beams[0]['frames'][:1000], n_batch ) ):
            frame, fh = reader.read_frame(frame_i)
            await websocket.send(frame.tobytes())
            counter+=1
            
            if counter % n_batch == 0:
                for j in range(n_batch):
                    response = await websocket.recv()
                    mask = np.frombuffer(response, "bool").reshape(2, 600, 800)
                    tracking_info = await websocket.recv()
                    tracking_info = json.loads(tracking_info)

                    counter = 0
        await websocket.close()

# asyncio.get_event_loop().run_until_complete(main())
asyncio.run(main())