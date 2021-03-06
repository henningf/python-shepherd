#!/usr/bin/env python

"""Given a series of 'jobnames' of the form 'job/launchid', will create a METS
file and form a Bagit folder for said METS."""

import os
import sys
import glob
import gzip
import bagit
import logging
import webhdfs
import argparse
import requests
import subprocess
from mets import *
from settings import *
from lxml import etree
from datetime import datetime
from dateutil.parser import parse
from xml.dom.minidom import parseString

LOGGING_FORMAT="[%(asctime)s] %(levelname)s: %(message)s"
logging.basicConfig( format=LOGGING_FORMAT, level=logging.DEBUG )
logger = logging.getLogger( "ldp06.mets" )
logging.root.setLevel( logging.DEBUG )

class SipCreator:
    def __init__( self, jobs, jobname, dummy=False, warcs=None, viral=None, logs=None ):
        """Sets up APIs."""
        self.webhdfs = webhdfs.API( prefix=WEBHDFS_PREFIX, user=WEBHDFS_USER )
        self.dummy = dummy
        self.jobname = jobname
        self.jobs = jobs
        self.warcs = warcs
        self.viral = viral
        self.logs = logs
        self.startdate = None

    def processJobs( self ):
        """Finds all WARCs and logs associated with jobs."""
        if self.warcs is not None:
            with open(self.warcs, "r") as i:
                self.warcs = [w.strip() for w in i]
        else:
            self.getWarcs()

        if self.viral is not None:
            with open(self.viral, "r") as i:
                self.viral = [v.strip() for v in i]
        else:
            self.getViral()

        if self.logs is not None:
            with open(self.logs, "r") as i:
                self.logs = [l.strip() for l in i]
        else:
            self.getLogs()

        if self.startdate is None and self.dummy:
            self.startdate = datetime.now().isoformat()

        if( self.dummy ):
            logger.info( "Getting dummy ARK identifiers..." )
            self.getDummyIdentifiers( len( self.warcs ) + len( self.viral ) + len( self.logs ) )
        else:
            logger.info( "Getting ARK identifiers..." )
            self.getIdentifiers( len( self.warcs ) + len( self.viral ) + len( self.logs ) )

    def createMets( self ):
        """Creates the Mets object."""
        logger.info( "Building METS..." )
        self.mets = Mets( self.startdate, self.warcs, self.viral, self.logs, self.identifiers )

    def writeMets( self, output=sys.stdout ):
        """Writes the METS XML to a file handle."""
        output.write( self.mets.getXml() )

    def bagit( self, directory, metadata=None ):
        """Creates a Bagit, if needs be with default metadata."""
        if metadata is None:
            metadata = { "Contact-Name": BAGIT_CONTACT_NAME, "Contact-Email": BAGIT_CONTACT_EMAIL, "Timestamp": datetime.now().strftime( "%Y-%m-%dT%H:%M:%SZ" ), "Description": BAGIT_DESCRIPTION + ";".join( self.jobs ) }
        bagit.make_bag( directory, metadata )

    def getWarcs( self ):
        """Finds all WARCs for a each job."""
        self.warcs = []
        for job in self.jobs:
            if len( glob.glob( "%s/%s/*.warc.gz.open" % ( WARC_ROOT, job ) ) ) > 0:
                raise Exception( "Open WARCs found in job %s" % job )
            for warc in glob.glob( "%s/%s/*.warc.gz" % ( WARC_ROOT, job ) ):
                logger.info( "Found %s..." % os.path.basename( warc ) )
                self.warcs.append( warc )
            if len( glob.glob( "%s/*.warc.gz.open" % ( IMAGE_ROOT ) ) ) > 0:
                raise Exception( "Open WARCs found in /images." )
            for warc in glob.glob( "%s/*.warc.gz" % ( IMAGE_ROOT ) ):
                logger.info( "Found %s..." % os.path.basename( warc ) )
                self.warcs.append( warc )
        if len( self.warcs ) == 0:
            raise Exception( "No WARCs found for %s" % self.jobs )

    def getViral( self ):
        """Finds all 'viral' WARCs for a each job."""
        self.viral = []
        for job in self.jobs:
            for viral in glob.glob( "%s/%s/*.warc.gz" % ( VIRAL_ROOT, job ) ):
                logger.info( "Found %s..." % os.path.basename( viral ) )
                self.viral.append( viral )

    def getLogs( self ):
        """Zips up all log/config. files and copies said archive to HDFS; finds the
        earliest timestamp in the logs."""
        self.logs = []
        crawl_logs = []
        for job in self.jobs:
            logger.info( "Processing job %s..." % job )
            try:
                zip = "%s/%s/%s.zip" % ( LOG_ROOT, job, os.path.basename( job ) )
                if os.path.exists( zip ):
                    logger.info( "Deleting %s..." % zip )
                    try:
                        os.remove( zip )
                    except Exception as e:
                        logger.error( "Cannot delete %s..." % zip )
                        raise Exception( "Cannot delete %s..." % zip )
                logger.info( "Zipping logs to %s" % zip )
                for crawl_log in glob.glob( "%s/%s/crawl.log*" % ( LOG_ROOT, job ) ):
                    logger.info( "Found %s..." % os.path.basename( crawl_log ) )
                    subprocess.check_output( ZIP.split() + [ zip, crawl_log ] )
                    crawl_logs.append( crawl_log )

                for log in glob.glob( "%s/%s/*-errors.log*" % ( LOG_ROOT, job ) ):
                    logger.info( "Found %s..." % os.path.basename( log ) )
                    subprocess.check_output( ZIP.split() + [ zip, log ] )

                if os.path.exists( "%s/%s/crawler-beans.cxml" % ( JOBS_ROOT, job ) ):
                    logger.info( "Found config..." )
                    subprocess.check_output( ZIP.split() + [ zip, "%s/%s/crawler-beans.cxml" % ( JOBS_ROOT, job ) ] )
                else:
                    logger.error( "Cannot find config." )
                    raise Exception( "Cannot find config." )
            except Exception as e:
                logger.error( "Problem building ZIP file: %s" % str( e ) )
                raise Exception( "Problem building ZIP file: %s" % str( e ) )
            if not self.dummy:
                if self.webhdfs.exists( zip ):
                    logger.info( "Deleting hdfs://%s..." % zip )
                    result = self.webhdfs.delete( zip )
                    if not result[ "boolean" ]:
                        logger.error( "Could not delete hdfs://%s..." % zip )
                        raise Exception( "Could not delete hdfs://%s..." % zip )
                logger.info( "Copying %s to HDFS." % os.path.basename( zip ) )
                self.webhdfs.create( zip, file=zip )
            self.logs.append( zip )
        self.startdate = self.findStartDate( crawl_logs )

    def getDummyIdentifiers( self, num ):
        """Provides a series of 'dummy' identifiers for testing purposes."""
        self.identifiers = []
        for i in range( num ):
            self.identifiers.append( "%s%s" % ( ARK_PREFIX, "{0:06d}".format( i ) ) )

    def getIdentifiers( self, num ):
        """Retrieves 'num' ARK identifiers from a webservice; alternatively calls an
        alternate method to provide 'dummy' identifiers."""
        self.identifiers = []
        if( self.dummy ):
            return getDummyIdentifiers( num )
        try:
            url = "%s%s" % ( ARK_URL, str( num ) )
            logger.debug( "Requesting %s ARKS: %s" % ( num, url ) )
            response = requests.post( url )
            data = response.content
        except Exception as e:
            logger.error( "Could not obtain ARKs: %s" % str( e ) )
            raise Exception( "Could not obtain ARKs: %s" % str( e ) )
        xml = parseString( data )
        for ark in xml.getElementsByTagName( "ark" ):
            self.identifiers.append( ark.firstChild.wholeText )
        if( len( self.identifiers ) != num ):
            raise Exception( "Problem parsing ARKs; %s, %s, %s" % ( self.jobs, self.identifiers, data ) )

    def verifyFileLocations( self ):
        """Checks that the configured file locations and job paths are sane."""
        verified = os.path.exists( LOCAL_ROOT ) and \
        os.path.exists( WARC_ROOT ) and  \
        os.path.exists( LOG_ROOT ) and  \
        os.path.exists( VIRAL_ROOT ) and  \
        os.path.exists( JOBS_ROOT )
        for job in self.jobs:
            verified = verified and \
            os.path.exists( "%s/%s" % ( JOBS_ROOT, job ) ) and  \
            os.path.exists( "%s/%s" % ( WARC_ROOT, job ) ) and  \
            os.path.exists( "%s/%s" % ( LOG_ROOT, job ) )
        return verified

    def verifyWebhdfs( self ):
        """Verifies that the WebHDFS services is available."""
        logger.info( "Verifying WebHDFS service..." )
        try:
            return self.webhdfs.exists( "/" )
        except requests.exceptions.ConnectionError, c:
            logger.error( "Cannot connect to HDFS: %s" % str( c ) )
            return False

    def verifyBinaries( self ):
        """Verifies that the Zip binaries are present and executable."""
        logger.info( "Verifying Zip binaries..." )
        if not os.access( ZIP.split()[ 0 ], os.X_OK ) or not os.access( UNZIP, os.X_OK ):
            logger.error( "Cannot find Zip binaries." )
            raise Exception( "Cannot find Zip binaries." )
        return True

    def verifySetup( self ):
        """Verifies that external dependencies are available."""
        return self.verifyWebhdfs() and self.verifyBinaries()

    def findStartDate( self, logs ):
        """Finds the earliest timestamp in a series of log files."""
        timestamps = []
        for log in logs:
            if log.endswith(".gz"):
                with gzip.open(log, "rb") as l:
                    fields = l.readline().split()
                    if len(fields) > 0:
                        timestamps.append(parse(fields[0]))
            else:
                with open( log, "rb" ) as l:
                    fields = l.readline().split()
                    if len( fields ) > 0:
                        timestamps.append( parse( fields[ 0 ] ) )
        timestamps.sort()
        return timestamps[ 0 ].strftime( "%Y-%m-%dT%H:%M:%SZ" )

if __name__ == "__main__":
    parser = argparse.ArgumentParser( description="Create METS files." )
    parser.add_argument( "jobs", metavar="J", type=str, nargs="+", help="Heritrix job name" )
    parser.add_argument( "-d", dest="dummy", action="store_true" )
    parser.add_argument( "-w", dest="warcs", help="File containing list of WARC paths." )
    parser.add_argument( "-v", dest="viral", help="File containing list of viral WARC paths." )
    parser.add_argument( "-l", dest="logs", help="File containing list of log paths." )
    args = parser.parse_args()

    jobname = datetime.now().strftime( "%Y%m%d%H%M%S" )
    sip = SipCreator( args.jobs, jobname=jobname, dummy=args.dummy, warcs=args.warcs, viral=args.viral, logs=args.logs )
    if sip.verifySetup():
        sip.processJobs()
        sip.createMets()
        if not os.path.isdir("%s/%s" % (OUTPUT_ROOT, jobname)):
            os.makedirs("%s/%s/" % (OUTPUT_ROOT, jobname))
        with open( "%s/%s/%s.xml" % ( OUTPUT_ROOT, jobname, jobname ), "wb" ) as o:
            sip.writeMets( o )
        sip.bagit( "%s/%s" % ( OUTPUT_ROOT, jobname ) )

