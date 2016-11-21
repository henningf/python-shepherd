from __future__ import absolute_import

import os
import json
import datetime
import crawl.tasks
from crawl.h3 import hapyx
from tasks.monitor import CheckStatus
from tasks.common import systems
from flask import Flask
from flask import render_template, redirect, url_for, request
app = Flask(__name__)


@app.route('/')
def status():

    json_file = CheckStatus(date=datetime.datetime.today() - datetime.timedelta(minutes=1)).output().path
    app.logger.info("Attempting to load %s" % json_file)
    with open(json_file,'r') as reader:
        services = json.load(reader)

    # Log collected data:
    #app.logger.info(json.dumps(services, indent=4))

    # And render
    return render_template('dashboard.html', title="Status", services=services)

@app.route('/get-rendered-original')
def get_rendered_original():
    """
    Grabs a rendered resource.

    Only reason Wayback can't do this is that it does not like the extended URIs
    i.e. 'screenshot:http://' and replaces them with 'http://screenshot:http://'
    """
    url = request.args.get('url')
    app.logger.debug("Got URL: %s" % url)
    #
    type = request.args.get('type', 'screenshot')
    app.logger.debug("Got type: %s" % type)

    # Query URL
    qurl = "%s:%s" % (type, url)
    # Query CDX Server for the item
    # Grab the payload from the WARC and return it.


@app.route('/control/dc/pause')
def pause_dc():
    servers = json.load(systems().servers)
    services = json.load(systems().services)
    for job in ['dc0-2016', 'dc1-2016', 'dc2-2016', 'dc3-2016']:
        server = servers[services['jobs'][job]['server']]
        h = hapyx.HapyX(server['url'], username=server['user'], password=server['pass'])
        h.pause_job(services['jobs'][job]['name'])
    return redirect(url_for('status'))


@app.route('/control/dc/unpause')
def unpause_dc():
    servers = json.load(systems().servers)
    services = json.load(systems().services)
    for job in ['dc0-2016', 'dc1-2016', 'dc2-2016', 'dc3-2016']:
        server = servers[services['jobs'][job]['server']]
        h = hapyx.HapyX(server['url'], username=server['user'], password=server['pass'])
        h.unpause_job(services['jobs'][job]['name'])
    return redirect(url_for('status'))


@app.route('/stop/<frequency>')
def stop(frequency=None):
    if frequency:
        crawl.tasks.stop_start_job(frequency,restart=False)
    return redirect(url_for('status'))


if __name__ == "__main__":
    app.run(debug=True)
