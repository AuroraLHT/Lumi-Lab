import matplotlib.pyplot as plt
import numpy as np
import queue
from lumi.rheed.livefft import LiveFFTCalculator, LiveFFTConfig
from lumi.rheed.integrator import MultiBoxIntegratorConfig, MultiBoxIntegrator
import time
import datetime
import uuid
import tqdm

import logging

logging.basicConfig(level=logging.INFO)

fft_config = LiveFFTConfig(
    idle_time=0.005,
    output_queue_size=3000, # we keep the queue size big enought to include all simulated input
    window_size=5,
    hop_size=0.5,
    time_resolution= 1/20
)

integrator_config = MultiBoxIntegratorConfig(
    idle_time=0.005,
    output_queue_size=3000,
)

def image_simulator( t, bbox):
    # sampling interval or time step
    # we create high frequency oscillation to just to demonstrate the fft
    # real feature is 
    freq = 1
    x = 3*np.sin(2*np.pi*freq*t)

    freq = 3
    x += np.sin(2*np.pi*freq*t)

    freq = 6   
    x += 0.5* np.sin(2*np.pi*freq*t)

    img = np.zeros((520, 780), dtype=float)
    img[bbox[0]:bbox[2], bbox[1]:bbox[3]] = x

    return img

# we simulate the camera queue by passing a well-defined oscillation image to the queue at a fixed rate
camera_queue = queue.Queue()

integrator = MultiBoxIntegrator(camera=None, camera_queue=camera_queue, config=integrator_config)
integrator.start()

bbox = [30, 50, 600, 680] # xyxy format
integrator.register_bbox(bbox=bbox, bbox_id=1)

live_fft_calculator = LiveFFTCalculator(integrator=integrator, config=fft_config)

live_fft_calculator.start()
live_fft_calculator.register_integration(bbox_id=1)


time_start = time.time()
integrations = []
ffts = []

n_frames = 1200
for i in tqdm.tqdm(range(n_frames), total=n_frames, desc="frames"):
    frame_time = time.time()
    time_stamp = str(datetime.datetime.fromtimestamp(frame_time))
    frame_uuid = str(uuid.uuid4())

    img = image_simulator(frame_time - time_start, bbox)
    # this would trigger all the automatic integration and fft
    camera_queue.put( (img, {"uuid": frame_uuid, "time_stamp":time_stamp, "time":frame_time}) )
    # so we add a random delay to simulate the camera capture delay
    time_end = time.time()
    time.sleep( 1/30 + np.random.rand()*1/60)

while not integrator.output_queue.empty():
    integration_content = integrator.output_queue.get()
    integration, integration_header = integration_content

    integrations.append( (integration_header["time"], integration) )


while not live_fft_calculator.output_queue.empty():
    live_fft_content = live_fft_calculator.output_queue.get()
    live_fft, live_fft_header = live_fft_content
    ffts.append( live_fft)

print("total time: ", time_end - time_start)
print("length of integrations: ", len(integrations))
print("length of ffts: ", len(ffts))

live_fft_calculator.stop()
integrator.stop()

t = [i[0] for i in integrations]
v = [i[1]["mean"] for i in integrations]
plt.plot(t, v)
plt.show()

fft_result = ffts[0]
fft_freq = fft_result["fft_freq"]

stft_matrix = np.zeros( (len(fft_freq), len(ffts) ) )
stft_freq = fft_freq
stft_time = [ffts[i]["time_end"] for i in range(len(ffts))]
print("stft_time: ", np.array(stft_time).min(), np.array(stft_time).max())
print("stft_freq: ", np.array(stft_freq).min(), np.array(stft_freq).max())

for i in range(len(ffts)):
    stft_matrix[:, i] = np.array(ffts[i]["fft_mag"])

plt.imshow(stft_matrix, aspect='auto', origin='lower', extent=[stft_time[0],  stft_time[-1], fft_freq[0], fft_freq[-1] ])
plt.xlabel("time (s)")
plt.ylabel("frequency (Hz)")
plt.colorbar()

# plt.imshow(stft_matrix)
# plt.ylim(1, 20)
plt.show()

live_fft_calculator.join()
integrator.join()
