#!/usr/bin/env python

"""Peeks at a message queue, downloading multiple messages without ACKing them."""

import os
import pika
import argparse

def callback( ch, method, properties, body ):
	"""Prints each message."""
	print body

# Could consider an ACKing option:
#	ch.basic_ack(delivery_tag = method.delivery_tag)

if __name__ == "__main__":
	parser = argparse.ArgumentParser('Peek at a message queue, downloading messages without ack-ing so that they remain on the queue.')
	parser.add_argument('--amqp-url', dest='amqp_url', type=str, 
					default="amqp://guest:guest@localhost:5672/%2f", 
					help="AMQP endpoint to use. [default: %(default)s]" )
	parser.add_argument('--num', dest='qos_num', type=int, 
					default=10, help="Maximum number of messages to peek at. [default: %(default)s]")
	parser.add_argument('exchange', metavar='exchange', help="Name of the exchange to use.")
	parser.add_argument('queue', metavar='queue', help="Name of queue to view messages from.")
	
	args = parser.parse_args()
	
	parameters = pika.URLParameters(args.amqp_url)
	connection = pika.BlockingConnection( parameters )
	channel = connection.channel()
	channel.queue_bind(queue=args.queue, exchange=args.exchange)
	channel.basic_qos(prefetch_count=args.qos_num)
	channel.basic_consume( callback, queue=args.queue, no_ack=False )
	channel.start_consuming()

