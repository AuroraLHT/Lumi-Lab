import tqdm
import requests
import json

n_trials = 1000
host = "localhost:8000"

for i in tqdm.tqdm( range(n_trials), total = n_trials):
    r = requests.get(f'http://{host}/RHEED/image')
