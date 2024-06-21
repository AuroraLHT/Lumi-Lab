import tqdm
import requests
import json

n_trials = 1000
host = "10.229.54.118:8000"

for i in tqdm.tqdm( range(n_trials), total = n_trials):
    r = requests.get(f'http://{host}/chamber/log')
    chamber_log = json.loads( r.content )
