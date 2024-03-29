import csv
import time
# import tqdm
from tqdm.notebook import tqdm
import pika

interval = 1

line_count = 0
rows = []
with open('test_log.csv', mode='r') as csv_file:
    csv_reader = csv.DictReader(csv_file)    
    for row in csv_reader:
        if line_count == 0:
            # print(f'Column names are {", ".join(row)}')
            line_count += 1
        # print(f'\t{row["name"]} works in the {row["department"]} department, and was born in {row["birthday month"]}.')
        line_count += 1
        rows.append(row)

connection = pika.BlockingConnection(
    pika.ConnectionParameters(host='localhost')
    # pika.ConnectionParameters(host='172.17.0.2')
)
channel = connection.channel()

channel.exchange_declare(exchange='operation', exchange_type='topic')

channel.basic_publish(
    exchange='operation', routing_key="log.filename", body="test_auto.csv"
)


line_count = 0

with open('test_auto.csv', mode='w') as auto_csv_file:
    pbar = tqdm()

    fieldnames = csv_reader.fieldnames + ['index']
    writer = csv.DictWriter(auto_csv_file, fieldnames=fieldnames)
    writer.writeheader()
    
    while True:
        for row in rows:
            row = dict(row)
            row.update({'index': line_count})
            writer.writerow(row)
            auto_csv_file.flush() # this ensure the content is actually written
            line_count += 1
            pbar.update(1)
            time.sleep(interval)
        # print(f'Processed {line_count} lines.')