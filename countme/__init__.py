# countme - parsing Fedora httpd access_log files to structured data.
#
# Copyright (C) 2020, Red Hat Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Author: Will Woods <wwoods@redhat.com>
#
# The main point of this script, as it says above, is parsing access_log to
# structured data. I'm trying to avoid packing Fedora-specific data-massaging
# into this; tools further down the pipeline can be responsible for figuring
# out how to group "updates-released-f32" and "fedora-modular-source-32".

import os
import re
from datetime import date, time, datetime, timezone, timedelta
from urllib.parse import parse_qsl
from typing import NamedTuple, Optional

from .regex import COUNTME_LOG_RE, MIRRORS_LOG_RE

# TODO: clean this up so it only exports the common/needed bits
__all__ = (
    'weeknum', 'parse_logtime', 'parse_querydict',

    'ItemWriter', 'CSVWriter', 'JSONWriter', 'AWKWriter', 'SQLiteWriter',
    'ItemReader', 'CSVReader',                            'SQLiteReader',

    'make_writer', 'guessreader', 'autoreader',

    'LogItem',    'MirrorItem',    'CountmeItem',
    'LogMatcher', 'MirrorMatcher', 'CountmeMatcher',
)

# ===========================================================================
# ====== Output item definitions and helpers ================================
# ===========================================================================

DAY_LEN = 24*60*60
WEEK_LEN = 7*DAY_LEN
COUNTME_EPOCH = 345600  # =00:00:00 Mon Jan 5 00:00:00 1970 (UTC)
MONTHIDX = {
    'Jan':1, 'Feb':2, 'Mar':3, 'Apr':4, 'May':5, 'Jun':6,
    'Jul':7, 'Aug':8, 'Sep':9, 'Oct':10, 'Nov':11, 'Dec':12
}

def weeknum(timestamp):
    return (int(timestamp) - COUNTME_EPOCH) // WEEK_LEN

def strptime_logtime(logtime):
    return datetime.strptime(logtime, "%d/%b/%Y:%H:%M:%S %z")

def logtime_to_isoformat(logtime):
    # logtime: '29/Mar/2020:16:04:28 +0000'
    # ISO8601: '2020-03-29T16:04:28+00:00'
    y = logtime[7:11]
    m = MONTHIDX[logtime[3:6]]
    d = logtime[0:2]
    time = logtime[12:20]
    offh = logtime[21:24]
    offm = logtime[24:26]
    return f"{y}-{m:02}-{d}T{time}{offh}:{offm}"

def offset_to_timezone(offset):
    '''Convert a UTC offset like -0400 to a datetime.timezone instance'''
    offmin = 60*int(offset[1:3]) + int(offset[3:5])
    if offset[0] == '-':
        offmin = -offmin
    return timezone(timedelta(minutes=offmin))

def parse_logtime(logtime):
    # Equivalent to - but faster than - strptime_logtime.
    # It's like ~1.5usec vs 11usec, which might seem trivial but in my tests
    # the regex parser can handle like ~200k lines/sec - or 5usec/line - so
    # an extra ~10usec to parse the time field isn't totally insignificant.
    # (btw, slicing logtime by hand and using re.split are both marginally
    # slower. datetime.fromisoformat is slightly faster but not available
    # in Python 3.6 or earlier.)
    dt, off = logtime.split(' ',1)
    date, hour, minute, second = dt.split(':',3)
    day, month, year = date.split('/',2)
    tz = timezone.utc if off in {"+0000","-0000"} else offset_to_timezone(off)
    return datetime(int(year), MONTHIDX[month], int(day),
                    int(hour), int(minute), int(second), 0, tz)

def parse_querydict(querystr):
    '''Parse request query the way mirrormanager does (last value wins)'''
    return dict(parse_qsl(querystr))

class LogItem(NamedTuple):
    '''
    Generic access.log data holder.
    '''
    host: str
    identity: str
    time: str
    method: str
    path: str
    query: Optional[str]
    protocol: str
    status: int
    nbytes: Optional[int]
    referrer: str
    user_agent: str

    def datetime(self):
        return parse_logtime(self.time)

    def timestamp(self):
        return parse_logtime(self.time).timestamp()

    def queryitems(self):
        return parse_qsl(self.query)

    def querydict(self):
        return parse_querydict(self.query)

# TODO: would be kinda nice if there was a clear subclass / translation
# between item classes... or if compile_log_regex made the class for you?
# Or something? It feels like these things should be more closely bound.


class MirrorItem(NamedTuple):
    '''
    A basic mirrorlist/metalink metadata item.
    Each item has a timestamp, IP, and the requested repo= and arch= values.
    '''
    timestamp: int
    host: str
    repo_tag: Optional[str]
    repo_arch: Optional[str]

class CountmeItem(NamedTuple):
    '''
    A "countme" match item.
    Includes the countme value and libdnf User-Agent fields.
    '''
    timestamp: int
    host: str
    os_name: str
    os_version: str
    os_variant: str
    os_arch: str
    countme: int
    repo_tag: str
    repo_arch: str

class LogMatcher:
    '''Base class for a LogMatcher, which iterates through a log file'''
    regex = NotImplemented
    itemtuple = NotImplemented
    def __init__(self, fileobj):
        self.fileobj = fileobj
    def iteritems(self):
        # TODO: at this point we're single-threaded and CPU-bound;
        # multithreading would speed things up here.
        for line in self.fileobj:
            match = self.regex.match(line)
            if match:
                yield self.make_item(match)
    __iter__ = iteritems
    @classmethod
    def make_item(cls, match):
        raise NotImplementedError

class MirrorMatcher(LogMatcher):
    '''Match all mirrorlist/metalink items, like mirrorlist.py does.'''
    regex = MIRRORS_LOG_RE
    itemtuple = MirrorItem
    @classmethod
    def make_item(cls, match):
        timestamp = parse_logtime(match['time']).timestamp()
        query = parse_querydict(match['query'])
        return cls.itemtuple(timestamp = int(timestamp),
                             host      = match['host'],
                             repo_tag  = query.get('repo'),
                             repo_arch = query.get('arch'))

class CountmeMatcher(LogMatcher):
    '''Match the libdnf-style "countme" requests.'''
    regex = COUNTME_LOG_RE
    itemtuple = CountmeItem
    @classmethod
    def make_item(cls, match):
        timestamp = parse_logtime(match['time']).timestamp()
        query = parse_querydict(match['query'])
        return cls.itemtuple(timestamp  = int(timestamp),
                             host       = match['host'],
                             os_name    = match['os_name'],
                             os_version = match['os_version'],
                             os_variant = match['os_variant'],
                             os_arch    = match['os_arch'],
                             countme    = int(query.get('countme')),
                             repo_tag   = query.get('repo'),
                             repo_arch  = query.get('arch'))

# ===========================================================================
# ====== ItemWriters - output formatting classes ============================
# ===========================================================================

class ItemWriter:
    def __init__(self, fp, itemtuple, timefield='timestamp', **kwargs):
        self._fp = fp
        self._itemtuple = itemtuple
        self._fields = itemtuple._fields
        assert timefield in self._fields, f"{itemtuple.__name__!r} has no time field {timefield!r}"
        self._timefield = timefield
        self._get_writer(**kwargs)
    def _get_writer(self, **kwargs):
        raise NotImplementedError
    def write_item(self, item):
        raise NotImplementedError
    def write_header(self):
        pass
    def write_footer(self):
        pass

class JSONWriter(ItemWriter):
    def _get_writer(self, **kwargs):
        import json
        self._dump = json.dump
    def write_item(self, item):
        self._dump(item._asdict(), self._fp)

class CSVWriter(ItemWriter):
    def _get_writer(self, **kwargs):
        import csv
        self._writer = csv.writer(self._fp)
    def write_header(self):
        self._writer.writerow(self._fields)
    def write_item(self, item):
        self._writer.writerow(item)

class AWKWriter(ItemWriter):
    def _get_writer(self, field_separator='\t', **kwargs):
        self._fieldsep = field_separator
    def _write_row(self, vals):
        self._fp.write(self._fieldsep.join(str(v) for v in vals) + '\n')
    def write_header(self):
        self._write_row(self._fields)
    def write_item(self, item):
        self._write_row(item)

class SQLiteWriter(ItemWriter):
    '''Write each item as a new row in a SQLite database table.'''
    # We have to get a little fancier with types here since SQL tables expect
    # typed values. Good thing Python has types now, eh?
    SQL_TYPE = {
        int: "INTEGER NOT NULL",
        str: "TEXT NOT NULL",
        float: "REAL NOT NULL",
        bytes: "BLOB NOT NULL",
        Optional[int]: "INTEGER",
        Optional[str]: "TEXT",
        Optional[float]: "REAL",
        Optional[bytes]: "BLOB",
    }
    def _sqltype(self, fieldname):
        typehint = self._itemtuple.__annotations__[fieldname]
        return self.SQL_TYPE.get(typehint, "TEXT")
    def _get_writer(self, tablename='countme_raw', **kwargs):
        self._tablename = tablename
        import sqlite3
        self._con = sqlite3.connect(self._fp.name)
        self._cur = self._con.cursor()
        # Generate SQL commands so we can use them later.
        # self._create_table creates the table, with column names and types
        # matching the names and types of the fields in self._itemtuple.
        self._create_table = (
            "CREATE TABLE IF NOT EXISTS {table} ({coldefs})".format(
                table=tablename,
                coldefs=",".join(f"{f} {self._sqltype(f)}" for f in self._fields),
            )
        )
        # self._insert_item is an "INSERT" command with '?' placeholders.
        self._insert_item = (
            "INSERT INTO {table} ({colnames}) VALUES ({colvals})".format(
                table=tablename,
                colnames=",".join(self._fields),
                colvals=",".join("?" for f in self._fields),
            )
        )
        # self._create_time_index creates an index on 'timestamp' or whatever
        # the time-series field is.
        self._create_time_index = (
            "CREATE INDEX IF NOT EXISTS {timefield}_idx on {table} ({timefield})".format(
                table=tablename,
                timefield=self._timefield
            )
        )
    def write_header(self):
        self._cur.execute(self._create_table)
    def write_item(self, item):
        self._cur.execute(self._insert_item, item)
    def write_footer(self):
        self._cur.execute(self._create_time_index)
        self._con.commit()

def make_writer(name, *args, **kwargs):
    '''Convenience function to grab/instantiate the right writer'''
    if name == "csv":
        writer = CSVWriter
    elif name == "json":
        writer = JSONWriter
    elif name == "awk":
        writer = AWKWriter
    elif name == "sqlite":
        writer = SQLiteWriter
    else:
        raise ValueError(f"Unknown writer '{name}'")
    return writer(*args, **kwargs)

# ===========================================================================
# ====== ItemReaders - counterpart to ItemWriter ============================
# ===========================================================================

class ReaderError(RuntimeError):
    pass

class ItemReader:
    def __init__(self, fp, itemtuple, **kwargs):
        self._fp = fp
        self._itemtuple = itemtuple
        self._itemfields = itemtuple._fields
        self._itemfactory = itemtuple._make
        self._filefields = None
        self._get_reader(**kwargs)
        if not self._filefields:
            raise ReaderError("no field names found")
        if self._filefields != self._itemfields:
            raise ReaderError(f"field mismatch: expected {self._itemfields}, got {self._filefields}")
    def _get_reader(self):
        '''Set up the ItemReader.
        Should set self._filefields to a tuple of the fields found in fp.'''
        raise NotImplementedError
    def _iter_rows(self):
        '''Return an iterator/generator that produces a row for each item.'''
        raise NotImplementedError
    def __iter__(self):
        for item in self._iter_rows():
            yield self._itemfactory(item)

class CSVReader(ItemReader):
    def _get_reader(self, **kwargs):
        import csv
        self._reader = csv.reader(self._fp)
        self._filefields = tuple(next(self._reader))
        # If we have numbers in our fieldnames, probably there was no header
        if any(name.isnumeric() for name in self._filefields):
            header = ','.join(fields)
            raise ReaderError(f"header bad/missing, got: {header}")
    def _iter_rows(self):
        return self._reader

# TODO: AWKReader, JSONReader

class SQLiteReader(ItemReader):
    def _get_reader(self, tablename='countme_raw', **kwargs):
        import sqlite3
        self._con = sqlite3.connect(self._fp.name)
        # TODO: self._con.set_progress_handler(handler, call_interval)
        self._cur = self._con.cursor()
        self._tablename = tablename
        if False and sqlite3.sqlite_version_info >= (3,16,0):
            fields_sql = f"SELECT name FROM pragma_table_info(?)"
            self._filefields = tuple(r[0] for r in self._cur.execute(fields_sql, (tablename,)))
        else:
            fields_sql = f"PRAGMA table_info('{tablename}')"
            self._filefields = tuple(r[1] for r in self._cur.execute(fields_sql))
    def _iter_rows(self):
        fields = ",".join(self._itemfields)
        return self._cur.execute(f"SELECT {fields} FROM {self._tablename}")

# Guess the right reader based on the filename.
def guessreader(fp):
    if fp.name.endswith(".csv"):
        reader = CSVReader
    elif fp.name.endswith(".db"):
        reader = SQLiteReader
    else:
        # FIXME: better format detection!!
        # TODO: if fp is seekable, peek and figure out filetype
        reader = None
    return reader

# TODO: should have name/args more like make_writer...
def autoreader(fp, itemtuple, **kwargs):
    '''Convenience function to guess & instantiate the right writer'''
    reader = guessreader(fp)
    return reader(fp, itemtuple, **kwargs)
