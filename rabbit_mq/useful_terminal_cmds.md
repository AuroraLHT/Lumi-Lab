```bash
# if run with docker, make sure you add "sudo docker exec -it rabbitmq " as the prefix
# for example
sudo docker exec -it rabbitmq rabbitmqctl list_queues


#Listing queues
# You may wish to see what queues RabbitMQ has and how many messages are in them. You can do it (as a privileged user) using the rabbitmqctl tool:
sudo rabbitmqctl list_queues

#Forgotten acknowledgment
# It's a common mistake to miss the basic_ack. It's an easy error, but the consequences are serious. 
# Messages will be redelivered when your client quits (which may look like random redelivery), but RabbitMQ will eat more and more memory 
# as it won't be able to release any unacked messages.

#In order to debug this kind of mistake you can use rabbitmqctl to print the messages_unacknowledged field:

sudo rabbitmqctl list_queues name messages_ready messages_unacknowledged

#Listing exchanges
# To list the exchanges on the server you can run the ever useful rabbitmqctl:
sudo rabbitmqctl list_exchanges


#Listing bindings
# Using rabbitmqctl list_bindings you can verify that the code actually creates bindings and queues as we want. With two receive_logs.py programs running you should see something like:
sudo rabbitmqctl list_bindings

#Clearing all the queue and exchanges
# follow the post in https://stackoverflow.com/questions/11459676/delete-all-the-queues-from-rabbitmq
sudo rabbitmqctl stop_app
sudo rabbitmqctl reset    # Be sure you really want to do this!
sudo rabbitmqctl start_app

```