import asyncio
from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel, AbstractExchange,  AbstractConnection, AbstractIncomingMessage, AbstractQueue,
)

#TODO : Move client to where the server is and we would import the client to here.
# this reduce the redundancy in code.