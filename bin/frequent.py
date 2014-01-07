#!/usr/local/bin/python2.7

import os
import sys
import glob
import time
import shutil
import httplib
import rfc3987
import urllib2
import logging
import argparse
import heritrix
import subprocess32
import dateutil.parser
from lxml import etree
from settings import *
from warcindexer import *
from retry_decorator import *
from datetime import datetime
from collections import Counter
from hanzo.warctools import WarcRecord
from requests.exceptions import ConnectionError

parser = argparse.ArgumentParser( description="Extract ARK-WARC lookup from METS." )
parser.add_argument( "-t", dest="timestamp", type=str, required=False, help="Timestamp" )
args = parser.parse_args()

frequencies = [ "daily", "weekly", "monthly", "quarterly", "sixmonthly", "annual" ]
heritrix_ports = { "daily": "8444", "weekly": "8443", "monthly": "8443", "quarterly": "8443", "sixmonthly": "8443", "annual": "8443" }
clamd_ports = { "daily": "3311", "weekly": "3310", "monthly": "3310", "quarterly": "3310", "sixmonthly": "3310", "annual": "3310" }

LOGGING_FORMAT="[%(asctime)s] %(levelname)s: %(message)s"
logging.basicConfig( format=LOGGING_FORMAT, level=logging.DEBUG )
logger = logging.getLogger( "frequency" )

global api

class Seed:
	def tosurt( self, url ):
		parsed = rfc3987.parse( url, rule="URI" )
		authority = parsed[ "authority" ].split( "." )
		authority.reverse()
		return "http://(" + ",".join( authority ) + ","

	def __init__( self, url, depth="capped", scope="root", ignore_robots=False ):
		self.url = url
		self.scope = scope
		self.depth = depth #capped=default, capped_large=higherLimit, deep=noLimit
		self.surt = self.tosurt( self.url )
		self.ignore_robots = ignore_robots

def verifyApi():
	try:
		api.rescan()
	except ConnectionError:
		logger.error( "Can't connect to Heritrix: ConnectionError" )
		sys.exit( 1 )

def jobsByStatus( status ):
	jobs = [] 
	for job in api.listjobs(): 
		if api.status( job ) == status:
			jobs.append( ( job, api.launchid( job ) ) )
	return jobs

def waitfor( job, status ):
	while api.status( job ) not in status:
		time.sleep( 10 )

def killRunningJob( newjob ):
	for job in api.listjobs():
		if job.startswith( newjob ) and api.status( job ) != "":
# TODO: Enable the below to render jobs before killing them.
#			logger.info( "Checking completeness for job: " + job )
#			checkCompleteness( job, api.launchid( job ) )
			logger.info( "Killing alread-running job: " + job )
			api.pause( job )
			waitfor( job, "PAUSED" )
			api.terminate( job )
			waitfor( job, "FINISHED" )
			api.teardown( job )
			waitfor( job, "" )

def setupjobdir( newjob ):
	root = HERITRIX_JOBS + "/" + newjob
	if not os.path.isdir( root ):
		logger.info( "Adding %s to Heritrix" % newjob )
		os.mkdir( root )
		api.add( HERITRIX_JOBS + "/" + newjob )
	return root

def addSurtAssociations( seeds, job ):
	script = []
	for seed in seeds:
		if seed.depth == "capped_large":
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).addSurtAssociation( \"%s\", \"higherLimit\" );" % seed.surt )
		if seed.depth == "deep":
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).addSurtAssociation( \"%s\", \"noLimit\" );" % seed.surt )
		if seed.depth != "capped":
			logger.info( "Amending cap for SURT " + seed.surt + " to " + seed.depth )
	return script

def addScopingRules( seeds, job ):
	script = []
	for seed in seeds:
		if seed.scope == "resource":
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).getOrCreateSheet( \"resourceScope\" ); " )
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).putSheetOverlay( \"resourceScope\", \"hopsCountReject.maxHops\", 1 ); " )
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).addSurtAssociation( \"%s\", \"resourceScope\" ); " % seed.surt )
		if seed.scope == "plus1":
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).getOrCreateSheet( \"plus1Scope\" ); " )
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).putSheetOverlay( \"plus1Scope\", \"hopsCountReject.maxHops\", 1 ); " )
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).putSheetOverlay( \"plus1Scope\", \"redirectAccept.enabled\", true ); " )
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).addSurtAssociation( \"%s\", \"plus1Scope\" ); " % seed.surt )
		if seed.scope == "subdomains":
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).getOrCreateSheet( \"subdomainsScope\" ); " )
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).putSheetOverlay( \"subdomainsScope\", \"onDomainAccept.enabled\", true ); " )
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).addSurtAssociation( \"%s\", \"subdomainsScope\" ); " % seed.surt )
		if seed.scope != "root":
			logger.info( "Setting scope for SURT %s to %s." % ( seed.surt, seed.scope ) )
	return script

def writeJobScript( job, script ):
	with open( "%s/%s/script" % ( HERITRIX_JOBS, job ), "wb" ) as o:
		o.writelines( "\n".join( script ) )

def runJobScript( job ):
	with open( "%s/%s/script" % ( HERITRIX_JOBS, job ), "rb" ) as i:
		script = i.read()
		api.execute( engine="beanshell", script=script, job=job )

def ignoreRobots( seeds, job ):
	script = []
	for seed in seeds:
		if seed.ignore_robots:
			script.append( "appCtx.getBean( \"sheetOverlaysManager\" ).addSurtAssociation( \"%s\", \"ignoreRobots\" );" % seed.surt )
			logger.info( "Ignoring robots.txt for SURT " + seed.surt )
	return script

def submitjob( newjob, seeds, frequency ):
	verifyApi()
	killRunningJob( newjob )
	root = setupjobdir( newjob )

	#Set up profile, etc.
	profile = HERITRIX_PROFILES + "/profile-frequent.cxml"
	if not os.path.exists( profile ):
		logger.error( "Cannot find profile for " + frequency + " [" + profile + "]" )
		return
	tree = etree.parse( profile )
	tree.xinclude()
	xmlstring = etree.tostring( tree, pretty_print=True, xml_declaration=True, encoding="UTF-8" )
	#Replace values
	xmlstring = xmlstring.replace( "REPLACE_JOB_NAME", newjob )
	xmlstring = xmlstring.replace( "REPLACE_CLAMD_PORT", clamd_ports[ frequency ] )
	xmlstring = xmlstring.replace( "REPLACE_JOB_ROOT", newjob )
	xmlstring = xmlstring.replace( "REPLACE_HERITRIX_JOBS", HERITRIX_JOBS )
	cxml = open( root + "/crawler-beans.cxml", "w" )
	cxml.write( xmlstring )
	cxml.close()
	#Copy files
	shutil.copy( HERITRIX_SHORTENERS, root )
	shutil.copy( HERITRIX_EXCLUDE, root )
	shutil.copy( HERITRIX_SURTS, root )
	#Write seeds
	seedstxt = open( root + "/seeds.txt", "w" )
	for seed in seeds:
		seedstxt.write( seed.url + "\n" )
	seedstxt.close()
	#Start the new job
	logger.info( "Building %s", newjob )
	api.build( newjob )
	waitfor( newjob, "NASCENT" )
	logger.info( "Launching %s", newjob )
	api.launch( newjob )
	waitfor( newjob, "PAUSED" )
	#Add SURT associations for caps.
	script = addSurtAssociations( seeds, newjob )
	script += addScopingRules( seeds, newjob )
	script += ignoreRobots( seeds, newjob )
	if len( script ) > 0:
		writeJobScript( newjob, script )
		runJobScript( newjob )
	logger.info( "Unpausing %s", newjob )
	api.unpause( newjob )
	waitfor( newjob, "RUNNING" )
	logger.info( "%s running. Exiting.", newjob )

@retry( urllib2.URLError, tries=20, timeout_secs=10 )
def callAct( url ):
	return urllib2.urlopen( url )

def check_frequencies():
	started_jobs = []
	for frequency in frequencies:
		SEED_FILE = "/heritrix/" + frequency + "-seeds.txt"
		seeds = []
		now = datetime.now()
		if args.timestamp is not None:
			now = dateutil.parser.parse( args.timestamp ).replace( tzinfo=None )

		try:
			xml = callAct( URL_ROOT + frequency )
			xml = xml.read()
		except urllib2.URLError, e:
			logger.error( "Cannot read ACT! " + str( e ) + " [" + frequency + "]" )
			continue
		except httplib.IncompleteRead, i:
			logger.error( "IncompleteRead: " + str( i.partial ) + " [" + frequency + "]" )
			continue

		def add_seeds( urls, depth, scope, ignore_robots ):
			for url in urls.split():
				try:
					seed = Seed( url=url, depth=depth, scope=scope, ignore_robots=ignore_robots )
					seeds.append( seed )
				except ValueError, v:
					logger.error( "INVALID URL: " + url )

		dom = etree.fromstring( xml )
		#crawlDateRange will be:
		#	blank			Determined by frequency.
		#	"start_date"		Determined by frequency if start_date < now.
		#	"start_date end_date"	Determined by frequency if start_date < now and end_date > now
		for node in dom.findall( "node" ):
			start_date = node.find( "crawlStartDate" ).text
			end_date = node.find( "crawlEndDate" ).text
			depth = node.find( "depth" ).text
			scope = node.find( "scope" ).text
			ignore_robots = ( node.find( "ignoreRobots.txt" ).text != None )

			# If there's no end-date or it's in the future, we're okay.
			if end_date is None or dateutil.parser.parse( end_date ) > now:
				# If there's no start date, use the defaults.
				if start_date is None:
					logger.error( "Empty start_date: " + node.find( "urls" ).text )
					continue
				else:
					start_date = dateutil.parser.parse( start_date )
					hotd = start_date.hour
					dotw = start_date.weekday()
					dotm = start_date.day
					moty = start_date.month

				if start_date <= now:
					# All frequencies are hour-dependent.
					if hotd == now.hour:
						if frequency == "daily": 
							add_seeds( node.find( "urls" ).text, depth, scope, ignore_robots )
						if frequency == "weekly" and dotw == now.weekday():
							add_seeds( node.find( "urls" ).text, depth, scope, ignore_robots )
						# Remaining frequencies all run on a specific day.
						if dotm == now.day:
							if frequency == "monthly":
								add_seeds( node.find( "urls" ).text, depth, scope, ignore_robots )
							if frequency == "quarterly" and moty%3 == now.month%3:
								add_seeds( node.find( "urls" ).text, depth, scope, ignore_robots )
							if frequency == "sixmonthly" and moty%6 == now.month%6:
								add_seeds( node.find( "urls" ).text, depth, scope, ignore_robots )
							if frequency == "annual" and moty == now.month:
								add_seeds( node.find( "urls" ).text, depth, scope, ignore_robots )
		if len( seeds ) > 0:
			if frequency == "daily": 
				jobname = frequency + "-" + now.strftime( "%H" ) + "00"
			if frequency == "weekly":
				jobname = frequency + "-" + now.strftime( "%a%H" ).lower() + "00"
			if frequency == "monthly":
				jobname = frequency + "-" + now.strftime( "%d%H" ) + "00"
			if frequency == "quarterly":
				jobname = frequency + "-" + str( now.month%3 ) + "-" + now.strftime( "%d%H" ) + "00"
			if frequency == "sixmonthly":
				jobname = frequency + "-" + str( now.month%6 ) + "-" + now.strftime( "%d%H" ) + "00"
			if frequency == "annual":
				jobname = frequency + "-" + now.strftime( "%m%d%H" ) + "00"
			global api
			api = heritrix.API( host="https://opera.bl.uk:" + heritrix_ports[ frequency ] + "/engine", user="admin", passwd="bl_uk", verbose=False, verify=False )
			started_jobs.append( jobname )
			submitjob( jobname, seeds, frequency )
	return started_jobs

def checkCompleteness( job, launchid ):
	logger.info( "Checking job " + job )
	warcs = glob.glob( WARC_ROOT + "/" + job + "/" + launchid + "/*.warc.gz*" )
	logger.info( "Indexing " + str( len( warcs ) ) + " WARCs." )
	index_warcs( warcs, QA_CDX, base_cdx=CDX )
	generate_path_index( warcs, PATH_INDEX )
	crawl_logs = glob.glob( LOG_ROOT + "/" + job + "/" + launchid + "/crawl.log*" )
	urls_to_render = []
	for crawl_log in crawl_logs:
		with open( crawl_log, "rb" ) as l:
			for line in l:
				url = line.split()[ 3 ]
				discovery_path = line.split()[ 4 ]
				if url.startswith( "http" ) and len( discovery_path.replace( "R", "" ) ) <= MAX_RENDER_DEPTH:
					urls_to_render.append( url )
	logger.info( "Rendering " + str( len( urls_to_render ) ) + " URLs." )
	open( WAYBACK_LOG, "wb" ).close()
	now = datetime.now().strftime( "%Y%m%d%H%M%S" )
	#TODO: Wayback should be running in proxy mode.
	for url in urls_to_render:
		try:
			ignore = subprocess32.check_output( [ PHANTOMJS, NETSNIFF, WAYBACK + now + "/" + url ], timeout=20 )
		except ( subprocess32.CalledProcessError, subprocess32.TimeoutExpired ):
			pass
	force = []
	with open( WAYBACK_LOG, "rb" ) as l:
		for line in l:
			if "F+" in line:
				force.append( re.sub( "^.*INFO: ", "", line ) )
	if len( force ) > 0:
		#Write the file 'one level up' to avoid conflicts with Heritrix reads.
		action = open( JOB_ROOT + job + "/" + now + ".force", "wb" )
		seen = []
		for line in force:
			url = line.split()[ 1 ]
			if url not in seen:
				action.write( line + "\n" )
				seen.append( url )
		action.close()
		shutil.move( JOB_ROOT + job + "/" + now + ".force", JOB_ROOT + job + "/action/" + now + ".force" )
		logger.info( "Found " + str( len( seen ) ) + " distinct new URLs." )
		# Pause briefly to allow file to be picked up.
		time.sleep( 35 )
		waitfor( job, [ "RUNNING", "EMPTY" ] )
		waitfor( job, "EMPTY" )

def humanReadable( bytes, precision=1 ):
	abbrevs = (
		( 1<<50L, "PB" ),
		( 1<<40L, "TB" ),
		( 1<<30L, "GB" ),
		( 1<<20L, "MB" ),
		( 1<<10L, "kB" ),
		( 1, "bytes" )
	)
	if bytes == 1:
		return "1 byte"
	for factor, suffix in abbrevs:
		if bytes >= factor:
			break
	return "%.*f %s" % ( precision, bytes / factor, suffix )

def logStats( logs ):
	response_codes = []
	data_size = 0
	host_regex = re.compile( "https?://([^/]+)/.*$" )
	all_hosts_data = {}
	for log in logs:
		with open( log, "rb" ) as l:
			for line in l:
				fields = line.split()
				match = host_regex.match( fields[ 5 ] )
				if match is not None:
					host = match.group( 1 )
					try:
						host_data = all_hosts_data[ host ]
					except KeyError:
						all_hosts_data[ host ] = { "data_size": 0, "response_codes": [] }
						host_data = all_hosts_data[ host ]
					host_data[ "response_codes" ].append( fields[ 1 ] )
					if fields[ 2 ] != "-":
						host_data[ "data_size" ] += int( fields[ 2 ] )
	for host, data in all_hosts_data.iteritems():
		data[ "response_codes" ] = Counter( data[ "response_codes" ] )
		data[ "data_size" ] = humanReadable( data[ "data_size" ] )
	return all_hosts_data

def checkUkwa( job, launchid ):
	frequency = job.split( "-" )[ 0 ]
	try:
		xml = callAct( URL_ROOT + frequency )
		xml = xml.read()
	except urllib2.URLError, e:
		logger.error( "Cannot read ACT! " + str( e ) + " [" + job + "/" + launchid + "]" )
		return
	except httplib.IncompleteRead, i:
		logger.error( "IncompleteRead: " + job + "/" + launchid )
		return
	for node in dom.findall( "node" ):
		wct_id = node.find( "wctId" ).text
		if wct_id is not None:
			crawl_logs = glob.glob( LOG_ROOT + "/" + job + "/" + launchid + "/crawl.log*" )
			for crawl_log in crawl_logs:		
				( data_size, response_codes ) = log_stats( crawl_log )

if __name__ == "__main__":
# Check for scheduled jobs.
	started_jobs = check_frequencies()
# Check for EMPTY jobs and render for completeness.
	for port in set( heritrix_ports.values() ):
		api = heritrix.API( host="https://opera.bl.uk:" + port + "/engine", user="admin", passwd="bl_uk", verbose=False, verify=False )
		for emptyJob, launchid in jobsByStatus( "EMPTY" ):
			if emptyJob in started_jobs:
				# Don't kill jobs we've just started.
				continue
			if emptyJob.startswith( "latest" ):
				# Don't render 'latest' job.
				continue
			logger.info( emptyJob + " is EMPTY; verifying." )
			checkCompleteness( emptyJob, launchid )
			logger.info( emptyJob + " checked; terminating." )
			api.terminate( emptyJob )
			waitfor( emptyJob, "FINISHED" )
			api.teardown( emptyJob )
			waitfor( emptyJob, "" )

