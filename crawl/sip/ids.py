#!/usr/bin/env python

"""
Generic methods used for verifying/indexing SIPs.
"""

from __future__ import absolute_import

import re
import logging
import tarfile
import hdfs
from lxml import etree
from StringIO import StringIO

# import the Celery app context
from crawl.celery import app
from crawl.celery import cfg

SIP_ROOT="/heritrix/sips"
NS={"mets": "http://www.loc.gov/METS/", "premis": "info:lc/xmlns/premis-v2"}
XLINK="{http://www.w3.org/1999/xlink}"

handler = logging.StreamHandler()
formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(filename)s.%(funcName)s: %(message)s")
handler.setFormatter(formatter)

# attach to root logger
logging.root.addHandler(handler)

logging.root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel( logging.INFO )
logger.info("INFO LOGGING ENABLED")
logger.debug("DEBUG LOGGING ENABLED")

def get_warc_identifiers(sip):
    for item in get_all_identifiers(sip):
        if item['mimetype'] == "application/warc":
            yield item

def get_all_identifiers(sip):
    """Parses the SIP in HDFS and retrieves FILE/ARK tuples."""
    client = hdfs.InsecureClient(cfg.get('hdfs', 'url'), user=cfg.get('hdfs', 'user'))
    tar = "%s/%s.tar.gz" % (SIP_ROOT, sip)
    status = client.status(tar,strict=False)
    if status:
        # Catch empty packages:
        if status['length'] == 0:
            logger.warning("Empty (zero byte) SIP package: %s" % tar)
            yield None
        else:
            with client.read(tar) as reader:
                t = reader.read()
                # Open the package:
                tar = tarfile.open(mode="r:gz", fileobj=StringIO(t))
                foundMets = False
                for i in tar.getmembers():
                    logger.debug("Examining %s" % i.name)
                    if i.name.endswith(".xml"):
                        foundMets = True
                        xml = tar.extractfile(i).read()
                        try:
                            tree = etree.fromstring(xml)
                            files = {}
                            n_files = 0
                            for mfile in tree.xpath("//mets:file", namespaces=NS):
                                #logger.debug("Found mets:file = %s " % etree.tostring(mfile))
                                admid = mfile.attrib["ADMID"]
                                logger.info("Found mets:file admid = %s " % admid)
                                path = mfile.xpath("mets:FLocat", namespaces=NS)[0].attrib["%shref" % XLINK]
                                files[admid] = { "path": path, "mimetype": mfile.attrib["MIMETYPE"], "size": mfile.attrib["SIZE"],
                                        "checksum_type": mfile.attrib["CHECKSUMTYPE"], "checksum": mfile.attrib["CHECKSUM"] }
                                n_files = n_files + 1
                            if len(files.keys()) != n_files:
                                logger.error("ERROR, more files than IDs")
                            n_amdsecs = 0
                            for amdsec in tree.xpath("//mets:amdSec", namespaces=NS):
                                #logger.debug("Found mets:amdSec = %s " % etree.tostring(amdsec))
                                admid = amdsec.attrib["ID"]
                                logger.info("Found mets:amdSec id = %s " % admid)
                                oiv = amdsec.xpath("mets:digiprovMD/mets:mdWrap/mets:xmlData/premis:object/premis:objectIdentifier/premis:objectIdentifierValue", namespaces=NS)
                                if oiv and len(oiv) == 1:
                                    files[admid]['ark'] = oiv[0].text
                                    n_amdsecs = n_amdsecs + 1
                                    logger.debug("Yielding %s" % files[admid] )
                                    yield files[admid]
                                else:
                                    logger.info("Skipping amdSec ID=%s" % admid)
                            if n_files != n_amdsecs:
                                logger.error("ERROR finding all amdSec elements")
                        except IndexError as i:
                            logger.error("Problem parsing METS for SIP: %s" % sip)
                            logger.exception(i)
                if not foundMets:
                    logger.error("No METS XML file found!")
    else:
        logger.warning("Could not find SIP: hdfs://%s" % tar)

def find_identifiers(output_file):
    with open(output_file, 'w') as f:
        client = hdfs.InsecureClient(cfg.get('hdfs', 'url'), user=cfg.get('hdfs', 'user'))
        for (path, dirs, files) in client.walk(SIP_ROOT):
            logger.info("Looking at path "+path)
            for file in files:
                logger.info("Looking at file " + file)
                if file.endswith('.tar.gz'):
                    sip = "%s/%s" % (path, file)
                    sip = sip[len(SIP_ROOT) + 1:]
                    sip = sip[:-7]
                    logger.info("Scanning %s..." % sip)
                    for waid in get_all_identifiers(sip):
                        f.write("%s %s\n" % (sip, waid) )

def main():
    find_identifiers('identifiers.txt')

# Test
#for waid in get_all_identifiers("weekly-wed2300/20141210230151"):
#    print(waid)
#sys.exit(0)

if __name__ == "__main__":
    main()
