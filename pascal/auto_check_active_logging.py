import pika
import json
from pathlib import Path
import argparse

def exit_with_message(message, code=1):
    print(message)
    exit(code)

if __name__ == "__main__":
    
    with open("config.json", "r") as f:
        config = json.load(f)

    parser = argparse.ArgumentParser()
    parser.add_argument("log", help="path of the log, could be folder or .csv path", default=config['LogFolder'])
    parser.add_argument("--host", help="rabbitmq host", default=config['RabbitmqHost'])

    args = parser.parse_args()
    log_path = Path(args.log_path)

    if not log_path.exists():
        exit_with_message(f"log path do not exist: {log_path}", 1)

    if log_path.suffix == ".csv":
        pass
    elif log_path.is_dir():
        latest_modified_time = -1
        latest_csv_log = None

        csv_log_gene = log_path.glob("*.csv")
        for csv_log in csv_log_gene:
            file_stat = csv_log.stat()
            if latest_modified_time < file_stat.st_mtime:
                latest_modified_time = file_stat.st_atime
                latest_csv_log = csv_log

        if latest_csv_log is not None:
            log_path = latest_csv_log
        else:
            exit_with_message(f"log path {log_path} has no .csv file", 1)
    
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=args.host)
    )
    channel = connection.channel()

    channel.exchange_declare(exchange='logs', exchange_type='direct')
    channel.exchange_declare(exchange='operation', exchange_type='topic')

    channel.basic_publish(
        exchange='operation', routing_key="log.filename", body=f"{log_path.absolute()}"
    )
    print(f"Find Log file {log_path.absolute()} and send a logging request.")


    

    