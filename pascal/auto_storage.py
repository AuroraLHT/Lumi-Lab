import pika
from pathlib import Path
import datetime
import csv
import json
from tqdm import tqdm

connection = pika.BlockingConnection(
    pika.ConnectionParameters(host='localhost'))
channel = connection.channel()

channel.exchange_declare(exchange='logs', exchange_type='direct')

# we setup a dedicated queue for this logger only
result = channel.queue_declare(queue='', exclusive=True)
queue_name = result.method.queue

# this worker receive chamber log and store it in the log file that would be updated in daily basis
routing_keys = ["chamber"]

for rk in routing_keys:
    channel.queue_bind(
        exchange='logs', queue=queue_name, routing_key=rk
    )

ROOT_STORAGE_FOLDER = Path("storage")
ROOT_STORAGE_FOLDER.mkdir(exist_ok=True)

pbar = tqdm()

FIELDNAMES = [
    'Time',
    'TG No.',
    'TG Rotate',
    'TG Spin Speed',
    'TG-Z',
    'Mask1',
    'Mask2',
    'Substrate',
    'FocusLns',
    'MirrorPs',
    'ATN',
    'BTFvalve',
    'LaserHz',
    'LaserPuls',
    'Laser moni',
    'Laser set',
    'MissedPuls',
    'PwrMeter',
    'HT set',
    'HT moni',
    'Delta HT curr',
    'HT Temp set',
    'HT Temp moni',
    'Delta Temp',
    'MFC1 set',
    'MFC1 moni',
    'Delta MFC1',
    'MFC2 set',
    'MFC2 moni',
    'Delta MFC2',
    'MFC3 set',
    'MFC3 moni',
    'Delta MFC3',
    'MFC4 set',
    'MFC4 moni',
    'Delta MFC4',
    'MFC5 set',
    'MFC5 moni',
    'Delta MFC5',
    'Prc Pres Main',
    'Prc Pres Main2',
    'Vac Pres Main',
    'Back Pres Main',
    'Prc Pres L/L',
    'Vac Pres L/L',
    'Back Pres L/L',
    'Heat Stat',
    'DepoLaserStat',
    'Pump Stat',
    'Valve Stat1',
    'Valve Stat2',
    'Shut Stat',
    'Motor Stat',
    'Other Stat',
    'A Utility1',
    'A Utility2',
    'A Pump1',
    'A Pump2',
    'A EXT1',
    'A EXT2',
    'A Motor1',
    'A Motor2',
    'W Pross',
    'W etc',
    'Heater power supply (PS)',
    'PID Heater control',
    'Ramp rate setting',
    'ON/OFF monitor in PS',
    'Inter-locking in excimer laser',
    'HV in excimer laser',
    'Gate shutter',
    'DP1 (Main)',
    'DP2 (2nd RHEED)',
    'DP3 (L/L)',
    'TMP1 (Main)',
    'TMP2 (RHEED)',
    'TMP3 (L/L)',
    'TMP4 (2nd RHEED)',
    'MV10 (Main)',
    'MV11 (Main bypass)',
    'FV1 (Main)',
    'MV2 (RHEED)',
    'FV2 (RHEED)',
    'MV3 (L/L)',
    'FV3 (L/L)',
    'RV3 (L/L)',
    'Sample Shutter',
    'Target spin',
    'Motor free',
    'Baking',
    'Emergency shutdown',
    'Compress air',
    'TMP1 (Main) alarm',
    'TMP20 (RHEED) alarm',
    'TMP3 (L/L) alarm',
    'TMP21 (2nd RHEED) alarm',
    'DP1 (Main) Thermal',
    'DP2 (2nd RHEED) Thermal',
    'DP3 (L/L) Thermal',
    'DP1 (Main) Vac Error',
    'DP2 (2nd RHEED) Vac Error',
    'DP3 (L/L) Vac Error',
    'Break in baking temp sensor',
    'Tripped in baking heater line',
    'Door open in laser shield',
    'Heater power supply alarm',
    'Chiller (for heater) alarm',
    'Temperature sensor failure',
    'Laser fiber head failure',
    'Pyrometer error',
    'Pulse failure in sample rotation',
    'Pulse failure in TG revolution',
    'Pulse failure in TG height (Z)',
    'Pulse failure in Mask 1',
    'Step out in L-mask encoder',
    'Limit sensor in Mask 1',
    'Pulse failure in Mask 2',
    'Limit sensor in Mask 2',
    'Pulse failure in focus lens (R)',
    'Driver trouble in focus lens (R)',
    'Pulse failure in mirror (R)',
    'Driver trouble in mirror (R)',
    'Pulse failure in attenuator (R)',
    'Limit sensor in attenuator (R)',
    'Miss-shot in excimer laser',
    'Large temperature deviation',
    'Large drive-current deviation',
    'T',
    'Mask1 confliction'
]


class CSVStorageManager:
    def __init__(self, root_folder, fieldnames):
        self.root_folder = root_folder
        self.current_date = datetime.date.today()
        self.fieldnames = fieldnames

        self.create_writer()
        # self.current_date = datetime.datetime.now().date()    
    
    def create_writer(self):
        self.csv_file_handle = ( self.root_folder / (self.current_date.isoformat() + ".csv") ).open("a")
        self.writer = csv.DictWriter(self.csv_file_handle, fieldnames=self.fieldnames)
        self.writer.writeheader()

    def close_writer(self):
        self.csv_file_handle.close()
        
    def store_item(self, item):
        current_date = datetime.date.today()
        if current_date != self.current_date:
            self.current_date = current_date
            self.close_writer()
            self.create_writer()
        
        self.writer.writerow(item)

    def __del__(self):
        self.close_writer()
        

csv_manager = CSVStorageManager(root_folder=ROOT_STORAGE_FOLDER, fieldnames=FIELDNAMES)

def callback(ch, method, properties, body):
    global csv_manager
    item = json.loads(body.decode())
    # print(item['index'])
    pbar.update(1)
    csv_manager.store_item(item)
    # print(f" [x] {method.routing_key}:{body}")


channel.basic_consume(
    queue=queue_name, on_message_callback=callback, auto_ack=True
)

channel.start_consuming()