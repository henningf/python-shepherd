#!/usr/bin/env python

"""Drives the crawls. Downloads the crawl feeds and initiates crawls as appropriate by dropping messages on the right queue."""

import os
import pika
import argparse
from shared.w3act import w3act


if __name__ == "__main__":
	parser = argparse.ArgumentParser('Peek at a message queue, downloading messages without ack-ing so that they remain on the queue.')
	default_amqp_url = "amqp://guest:guest@localhost:5672/%2f"
	parser.add_argument('--amqp-url', dest='amqp_url', 
					type=str, default=default_amqp_url, 
					help="AMQP endpoint to use (defaults to amqp://guest:guest@localhost:5672/%%2f)" )
	parser.add_argument('--num', dest='peek_num', 
					type=int, default=10, help="Maximum number of messages to peek at, (defaults to 10)")
	parser.add_argument('exchange', metavar='exchange', help="Name of the exchange to use.")
	parser.add_argument('queue', metavar='queue', help="Name of queue to view messages from.")
	args = parser.parse_args()
	
	act = w3act("http://localhost:9000/act/","wa-sysadm@bl.uk","sysAdmin")
	print(act.get_ld_export('frequent'));
	
	parameters = pika.URLParameters(args.amqp_url)
	connection = pika.BlockingConnection( parameters )
	channel = connection.channel()
	channel.queue_bind(queue=args.queue, exchange=args.exchange)

