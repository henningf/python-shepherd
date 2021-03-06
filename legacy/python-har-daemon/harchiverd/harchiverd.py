#!/usr/bin/env python

"""Process which watches a configured queue for messages and for each, calls a
webservice, storing the result in a WARC file."""

import sys
import gzip
import json
import pika
import uuid
import shutil
import logging
import requests
import settings
import time
from datetime import datetime
from urlparse import urlparse
from hanzo.warctools import WarcRecord
from warcwriterpool import WarcWriterPool, warc_datetime_str

# Default log level:
logging.getLogger().setLevel(logging.ERROR)

# Log level for our code:
logger = logging.getLogger("harchiverd")
handler = logging.FileHandler(settings.LOG_FILE)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.getLevelName(settings.LOG_LEVEL))

# Report settings:
logger.info("LOG_FILE = %s", settings.LOG_FILE)
logger.info("LOG_LEVEL = %s", settings.LOG_LEVEL)
logger.info("OUTPUT_DIRECTORY = %s", settings.OUTPUT_DIRECTORY)
logger.info("WEBSERVICE = %s", settings.WEBSERVICE)
logger.info("PROTOCOLS = %s", settings.PROTOCOLS)
logger.info("AMQP_URL = %s", settings.AMQP_URL)
logger.info("AMQP_EXCHANGE = %s", settings.AMQP_EXCHANGE)
logger.info("AMQP_QUEUE = %s", settings.AMQP_QUEUE)
logger.info("AMQP_KEY = %s", settings.AMQP_KEY)
logger.info("AMQP_OUTLINK_QUEUE = %s", settings.AMQP_OUTLINK_QUEUE)

def write_outlinks(har, dir, parent):
    """Writes outlinks in the HAR to a gzipped file."""
    if dir is None:
        return
    j = json.loads(har)
    filename = "%s/%s.schedule.gz" % (dir, str(datetime.now().strftime("%s")))
    with gzip.open(filename, "wb") as o:
        for entry in j["log"]["entries"]:
            protocol = urlparse(entry["request"]["url"]).scheme
            if not protocol in settings.PROTOCOLS:
                continue
            referer = None
            for header in entry["request"]["headers"]:
                if header["name"].lower() == "referer":
                    referer = header["value"]
            if referer is not None:
                o.write("F+ %s E %s\n" % (entry["request"]["url"], referer))
            else:
                o.write("F+ %s\n" % entry["request"]["url"])

def send_amqp_message(message, client_id):
    """Send outlinks to AMQP."""
    parameters = pika.URLParameters(settings.AMQP_URL)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.exchange_declare(exchange=settings.AMQP_EXCHANGE,
                             type="direct", 
                             durable=True, 
                             auto_delete=False)
    channel.queue_declare(queue=client_id,
                          durable=True, 
                          exclusive=False, 
                          auto_delete=False)
    channel.queue_bind(queue=client_id,
           exchange=settings.AMQP_EXCHANGE,
           routing_key=client_id)
    channel.basic_publish(exchange=settings.AMQP_EXCHANGE,
        routing_key=client_id,
        properties=pika.BasicProperties(
            delivery_mode=2,
        ),
        body=message)
    connection.close()

def send_to_amqp(client_id, url,method,headers, parentUrl, parentUrlMetadata, forceFetch=False, isSeed=False):
    sent = False
    message = {
        "url": url,
        "method": method,
        "headers": headers,
        "parentUrl": parentUrl,
        "parentUrlMetadata": parentUrlMetadata
    }
    if forceFetch:
        message["forceFetch"] = True
    if isSeed:
        message["isSeed"] = True
    logger.debug("Sending message: %s" % message)
    while not sent:
        try:
            send_amqp_message(json.dumps(message), client_id)
            sent = True
        except:
            logger.error("Problem sending message: %s; %s" % (message, sys.exc_info()))
            logger.error("Sleeping for 30 seconds...")
            time.sleep(30)


def amqp_outlinks(har, client_id, parent):
    """Passes outlinks back to queue."""
    har = json.loads(har)
    parent = json.loads(parent)
    embeds = 0
    for entry in har["log"]["entries"]:
        protocol = urlparse(entry["request"]["url"]).scheme
        if not protocol in settings.PROTOCOLS:
            continue
        embeds = embeds + 1
        send_to_amqp(client_id, entry["request"]["url"],entry["request"]["method"], 
            {h["name"]: h["value"] for h in entry["request"]["headers"]}, 
            parent["url"], parent["metadata"], forceFetch=True)
    links = 0
    for entry in har["log"]["pages"]:
        for item in entry["map"]:
            # Some map regions are JavaScript rather than direct links, so only take the links:
            if 'href' in item:
                links = links + 1
                send_to_amqp(client_id, item['href'],"GET", {}, parent["url"], parent["metadata"])
    logger.info("Queued %i embeds and %i links for url '%s'." % (embeds, links, parent["url"]) )


def handle_json_message(message):
    """Parses AMQPPublishProcessor-style JSON messages."""
    logger.debug("Handling JSON message: %s" % message)
    selectors = [":root"]
    j = json.loads(message)
    url = j["url"]
    if "selectors" in j.keys():
        selectors += j["selectors"]
    return (url, j["clientId"], selectors, amqp_outlinks)

def handle_pipe_message(message):
    """Parses pipe-separated message."""
    logger.debug("Handling pipe-separated message: %s" % message)
    url = None
    dir = None
    selectors = [":root"]
    parts = message.split("|")
    if len(parts) == 1:
        url = parts[0]
    elif len(parts) == 2:
        url, dir = parts
    else:
        url = parts[0]
        dir = parts[1]
        selectors += parts[2:]
    return (url, dir, selectors, write_outlinks)

def callback(warcwriter, body):
    """Parses messages, writing results to disk.

    Arguments:
    warcwriter -- A python-warcwriterpool instance.
    body -- The incoming message body.

    """
    try:
        logger.debug("Message received: %s." % body)
        if body.startswith("{"):
            (url, handler_id, selectors, url_handler) = handle_json_message(body)
        else:
            (url, handler_id, selectors, url_handler) = handle_pipe_message(body)

        ws = "%s/%s" % (settings.WEBSERVICE, url)
        logger.debug("Calling %s" % ws)
        r = requests.post(ws, data=json.dumps(selectors))
        if r.status_code == 200:
            # Handle outlinks, passing original message...
            har = r.content
            url_handler(har, handler_id, body)
            headers = [
                (WarcRecord.TYPE, WarcRecord.METADATA),
                (WarcRecord.URL, url),
                (WarcRecord.CONTENT_TYPE, "application/json"),
                (WarcRecord.DATE, warc_datetime_str(datetime.now())),
                (WarcRecord.ID, "<urn:uuid:%s>" % uuid.uuid1()),
            ]
            warcwriter.write_record(headers, "application/json", har)
        else:
            logger.warning("None-200 response for %s; %s" % (body, r.content))
    except Exception as e:
        logger.error("%s [%s]" % (str(e), body))

def run_harchiver():
    """Maintains a connection to the queue."""

    warcwriter = WarcWriterPool(gzip=True, output_dir=settings.OUTPUT_DIRECTORY)
    while True:
        channel = None
        try:
            logger.info("Starting connection: %s" % (settings.AMQP_URL))
            parameters = pika.URLParameters(settings.AMQP_URL)
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            channel.exchange_declare(exchange=settings.AMQP_EXCHANGE,
                                     type="direct", 
                                     durable=True, 
                                     auto_delete=False)
            channel.queue_declare(queue=settings.AMQP_QUEUE, 
                                  durable=True, 
                                  exclusive=False, 
                                  auto_delete=False)
            channel.queue_bind(queue=settings.AMQP_QUEUE, 
                   exchange=settings.AMQP_EXCHANGE,
                   routing_key=settings.AMQP_KEY)
            logger.info("Started connection: %s" % (settings.AMQP_URL))
            for method_frame, properties, body in channel.consume(settings.AMQP_QUEUE):
                callback(warcwriter, body)
                channel.basic_ack(method_frame.delivery_tag)
        except Exception as e:
            logger.error("Error: %s" % e)
            if channel and channel.is_open and not channel.is_closing:
                try:
                    requeued_messages = channel.cancel()
                    logger.info("Requeued %i messages" % requeued_messages)
                except Exception as e:
                    logger.warning("Could not cancel/shutdown neatly.")
            logger.warning("Sleeping for 15 seconds before retrying...")
            time.sleep(15)

if __name__ == "__main__":
    run_harchiver()

