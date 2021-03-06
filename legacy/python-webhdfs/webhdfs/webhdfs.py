#!/usr/bin/env python

import os
import sys
import json
import zlib
import logging
import requests
from fnmatch import fnmatch
from StringIO import StringIO

LOGGING_FORMAT="[%(asctime)s] %(levelname)s: %(message)s"
logging.basicConfig(format=LOGGING_FORMAT, level=logging.WARNING)
logger = logging.getLogger("webhdfs")


class API():
    def __init__(self, prefix="http://localhost:14000/webhdfs/v1", verbose=False, user="hadoop"):
        self.verbose = verbose
        self.prefix = prefix
        self.user = user

    def _get(self, path="/", op="GETFILESTATUS", stream=False):
        url = "%s%s?user.name=%s&op=%s" % (self.prefix, path, self.user, op)
        r = requests.get(url, stream=stream)
        return r

    def _post(self, path, file=None, data=None, op="CREATE"):
        url = "%s%s?user.name=%s&op=%s&data=true" % (self.prefix, path, self.user, op)
        headers = {"content-type": "application/octet-stream"}
        if file is not None:
            with open(file, "rb") as f:
                r = requests.put(url, headers=headers, data=f)
        else:
            r = requests.put(url, headers=headers, data=data)
        return r

    def _delete(self, path, recursive=False):
        url = "%s%s?user.name=%s&op=DELETE&recursive=%s" % (self.prefix, path, self.user, str(recursive).lower())
        r = requests.delete(url)
        return r

    def _genblocks(self, path, chunk_size=4096, decompress=False):
        """Yields blocks from either a directory or a single file."""
        if decompress:
            d = zlib.decompressobj(zlib.MAX_WBITS|32)
        if self.isdir(path):
            directory = path
        else:
            directory = os.path.dirname(path)
        for f in self.list(path):
            if f["type"] == "FILE":
                r = self.openstream(os.path.join(directory, f["pathSuffix"]))
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        if decompress:
                            chunk = d.decompress(chunk)
                        if len(chunk) > 0:
                            yield chunk
                r.close()

    def list(self, path):
        r = self._get(path=path, op="LISTSTATUS")
        js = json.loads(r.content)
        return js["FileStatuses"]["FileStatus"]

    def find(self, path, name="*"):
        if self.isdir(path):
            for entry in self.list(path):
                for sub in self.find(os.path.join(path, entry["pathSuffix"]), name=name):
                    yield sub
        else:
            if fnmatch(path, name):
                yield path

    def file(self, path):
        r = self._get(path=path, op="GETFILESTATUS")
        return json.loads(r.text)

    def open(self, path):
        if self.isdir(path):
            raise TypeError("Cannot open a directory.")
        r = self._get(path=path, op="OPEN")
        return r.content

    def openstream(self, path):
        if self.isdir(path):
            raise TypeError("Cannot open a directory.")
        return self._get(path=path, op="OPEN", stream=True)

    def exists(self, path):
        r = self._get(path=path)
        j = json.loads(r.text)
        return j.has_key("FileStatus")

    def isdir(self, path):
        r = self._get(path=path)
        j = json.loads(r.text)
        return (self.exists(path) and j["FileStatus"]["type"] == "DIRECTORY")

    def create(self, path, file=None, data=None):
        if self.exists(path):
            raise IOError("Already exists: %s" % path)
        if (file is None and data is None) or (file is not None and data is not None):
            logger.warning("Need either input file or data.")
        else:
            if file is not None:
                r = self._post(path, file=file)
            else:
                r = self._post(path, data=data)
            return r

    def delete(self, path, recursive=False):
        if not self.exists(path):
            raise IOError("Does not exist: %s" % path)
        else:
            r = self._delete(path, recursive=recursive)
            return json.loads(r.text)

    def checksum(self, path):
        r = self._get(path=path, op="GETFILECHECKSUM")
        return json.loads(r.text)

    def getmerge(self, path, output=sys.stdout):
        """Merges one or more HDFS files into a single, local file."""
        for chunk in self._genblocks(path):
            output.write(chunk)

    def readlines(self, path, decompress=False):
        """Yields lines from either a directory or a single file."""
        previous = ""
        for chunk in self._genblocks(path, decompress=decompress):
            lines = StringIO(previous + chunk).readlines()
            for line in lines[:-1]:
                yield line
            previous = lines[-1]
        yield previous

