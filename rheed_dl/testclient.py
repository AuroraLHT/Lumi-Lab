from pathlib import Path
import requests
import websockets
import numpy as np
from tqdm.asyncio import trange, tqdm
from rhana.io.tokyo_u import RHEEDStreamReader
import asyncio

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

async def main():
    async with websockets.connect('ws://127.0.0.1:8000/ws/test2') as websocket:
    # async with websockets.connect('ws://omicron.issp.u-tokyo.ac.jp:8000/ws/unet') as websocket:
        # _batch = []
        counter = 0
        for frame_i in tqdm(beams[0]['frames'][:5000]):
            # frame, fh = reader.read_frame(frame_i)
            frame = np.random.randint(0, 100, (600, 800), dtype=np.uint16)
            await websocket.send(frame.tobytes())
            counter+=1

            # if len(_batch) == n_batch:
            if counter == n_batch:
                # batch = np.stack(_batch, axis=0)
                # await websocket.send(batch.tobytes())
                for j in range(n_batch):
                    response = await websocket.recv()
                    # mask = np.frombuffer(response, "bool").reshape(2, 600, 800)
                # _batch = []
                counter = 0

        if counter<n_batch:
            for i in range(n_batch - counter):
                await websocket.send(frame.tobytes())
            for j in range(n_batch):
                response = await websocket.recv()
                # mask = np.frombuffer(response, "bool").reshape(2, 600, 800)
                
asyncio.get_event_loop().run_until_complete(main())