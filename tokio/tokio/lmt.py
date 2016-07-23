#!/usr/bin/env python

import os
import sys
import time
import datetime
import MySQLdb
import numpy as np
import tokio

_LMT_TIMESTEP = 5.0

_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_MYSQL_FETCHMANY_LIMIT = 10000

_QUERY_OST_DATA = """
SELECT
    UNIX_TIMESTAMP(TIMESTAMP_INFO.`TIMESTAMP`) as ts,
    OST_NAME as ostname,
    READ_BYTES as read_b,
    WRITE_BYTES as write_b
FROM
    OST_DATA
INNER JOIN TIMESTAMP_INFO ON TIMESTAMP_INFO.TS_ID = OST_DATA.TS_ID
INNER JOIN OST_INFO ON OST_INFO.OST_ID = OST_DATA.OST_ID
WHERE
    TIMESTAMP_INFO.`TIMESTAMP` >= '%s'
AND TIMESTAMP_INFO.`TIMESTAMP` < '%s'
ORDER BY ts, ostname;
"""

### Find the most recent timestamp for each OST before a given time range.  This
### is to calculate the first row of diffs for a time range.  There is an
### implicit assumption that there will be at least one valid data point for
### each OST in the 24 hours preceding t_start.  If this is not the case, not
### every OST will be represented in the output of this query.
_QUERY_FIRST_OST_DATA = """
SELECT
    UNIX_TIMESTAMP(TIMESTAMP_INFO.`TIMESTAMP`),
    OST_INFO.OST_NAME,
    OST_DATA.READ_BYTES,
    OST_DATA.WRITE_BYTES
FROM
    (
        SELECT
            OST_DATA.OST_ID AS ostid,
            MAX(OST_DATA.TS_ID) AS newest_tsid
        FROM
            OST_DATA
        INNER JOIN TIMESTAMP_INFO ON TIMESTAMP_INFO.TS_ID = OST_DATA.TS_ID
        WHERE
            TIMESTAMP_INFO.`TIMESTAMP` < '{datetime}'
        AND TIMESTAMP_INFO.`TIMESTAMP` > SUBTIME(
            '{datetime}',
            '{lookbehind}'
        )
        GROUP BY
            OST_DATA.OST_ID
    ) AS last_ostids
INNER JOIN OST_DATA ON last_ostids.newest_tsid = OST_DATA.TS_ID AND last_ostids.ostid = OST_DATA.OST_ID
INNER JOIN OST_INFO on OST_INFO.OST_ID = last_ostids.ostid
INNER JOIN TIMESTAMP_INFO ON TIMESTAMP_INFO.TS_ID = last_ostids.newest_tsid
"""

_QUERY_TIMESTAMP_MAPPING = """
SELECT
    UNIX_TIMESTAMP(`TIMESTAMP`)
FROM
    TIMESTAMP_INFO
WHERE
    `TIMESTAMP` >= '%s'
AND `TIMESTAMP` < '%s'
ORDER BY
    TS_ID
"""

def connect(*args, **kwargs):
    return LMTDB( *args, **kwargs )

class LMTDB(object):
    def __init__(self, dbhost=None, dbuser=None, dbpassword=None, dbname=None):
        if dbhost is None:
            dbhost = os.environ.get('PYLMT_HOST')
        if dbuser is None:
            dbuser = os.environ.get('PYLMT_USER')
        if dbpassword is None:
            dbpassword = os.environ.get('PYLMT_PASSWORD')
        if dbname is None:
            dbname = os.environ.get('PYLMT_DB')

        ### establish db connection
        self.db = MySQLdb.connect( 
            host=dbhost,
            user=dbuser,
            passwd=dbpassword,
            db=dbname)

        ### the list of OST names is an immutable property of a database, so
        ### fetch and cache it here
        self.ost_names = []
        for row in self._query_mysql('SELECT DISTINCT OST_NAME from OST_INFO;'):
            self.ost_names.append(row[0])
        self.ost_names = tuple(self.ost_names)

    def __enter__(self):
        return self

    def __die__(self):
        if self.db:
            self.db.close()
            sys.stderr.write('closing DB connection\n')

    def __exit__(self, exc_type, exc_value, traceback):
        if self.db:
            self.db.close()
            sys.stderr.write('closing DB connection\n')

    def close( self ):
        if self.db:
            self.db.close()
            sys.stderr.write('closing DB connection\n')


    def get_rw_data( self, t_start, t_stop, timestep ):
        """
        Wrapper function for _get_rw_data that breaks a single large query into
        smaller queries over smaller time ranges.  This is an optimization to
        avoid the O(N*M) scaling of the JOINs in the underlying SQL query.
        """
        _TIME_CHUNK = datetime.timedelta(hours=1)
        t0 = t_start

        buf_r = None
        buf_w = None
        while t0 < t_stop:
            tf = t0 + _TIME_CHUNK
            if tf > t_stop:
                tf = t_stop
            ( tmp_r, tmp_w ) = self._get_rw_data( t0, tf, timestep )
            tokio._debug_print( "Retrieved %.2f GiB read, %.2f GiB written" % (
                 (tmp_r[-1,:].sum() - tmp_r[0,:].sum())/2**30,
                 (tmp_w[-1,:].sum() - tmp_w[0,:].sum())/2**30) )

            ### first chunk of output
            if buf_r is None:
                buf_r = tmp_r
                buf_w = tmp_w
            ### subsequent chunks get concatenated
            else:
                assert( tmp_r.shape[1] == buf_r.shape[1] )
                assert( tmp_w.shape[1] == buf_w.shape[1] )
                print buf_r.shape, tmp_r.shape
                buf_r = np.concatenate(( buf_r, tmp_r ), axis=0)
                buf_w = np.concatenate(( buf_w, tmp_w ), axis=0)
            t0 += _TIME_CHUNK

        tokio._debug_print( "Finished because t0(=%s) !< t_stop(=%s)" % (
                t0.strftime( _DATE_FMT ), 
                tf.strftime( _DATE_FMT ) ))
        return ( buf_r, buf_w )

    def _get_rw_data( self, t_start, t_stop, binning_timestep ):
        """
        Return a tuple of three objects:
            1. a tuple of N strings that encode ost names
            2. a M*N matrix of int64s that encode the total read bytes for N STs
               over M timesteps
            3. a M*N matrix of int64s that encode the total write bytes for N
               STs over M timesteps

        timestep governs the M dimension and is a required input parameter
        because we don't want to guess what the timestep of the underlying LMT
        data might be (because it can be variable!).  Time will be binned
        appropriately if binning_timestep > lmt_timestep.

        the number of OSTs (the N dimension) is derived from the database.
        """
        tokio._debug_print( "Retrieving %s >= t > %s" % (
            t_start.strftime( _DATE_FMT ),
            t_stop.strftime( _DATE_FMT ) ) )
        query_str = _QUERY_OST_DATA % ( 
            t_start.strftime( _DATE_FMT ), 
            t_stop.strftime( _DATE_FMT ) 
        )
        rows = self._query_mysql( query_str )

        ### Get the number of timesteps (# rows)
        ts_ct = int((t_stop - t_start).total_seconds() / binning_timestep)
        t0 = int(time.mktime(t_start.timetuple()))

        ### Get the number of OSTs and their names (# cols)
        ost_ct = len(self.ost_names)

        ### Initialize everything to -0.0; we use the signed zero to distinguish
        ### the absence of data from a measurement of zero
        buf_r = np.full( shape=(ts_ct, ost_ct), fill_value=-0.0, dtype='f8' )
        buf_w = np.full( shape=(ts_ct, ost_ct), fill_value=-0.0, dtype='f8' )

        if len(rows) > 0:
            for row in rows:
                icol = int((row[0] - t0) / binning_timestep)
                irow = self.ost_names.index( row[1] )
                buf_r[icol,irow] = row[2]
                buf_w[icol,irow] = row[3]

        return ( buf_r, buf_w )


    def gen_rw_data( self, t_start, t_stop ):
        """
        Return a generator that gets the read/write bytes data from LMT between
        [ t_start, t_stop ).  Split the time range into 1-hour chunks to avoid
        issuing massive JOINs to the LMT server and bogging down.
        """
        _TIME_CHUNK = datetime.timedelta(hours=1)

        t0 = t_start
        while t0 < t_stop:
            tf = t0 + _TIME_CHUNK
            if tf > t_stop:
                tf = t_stop
            query_str = _QUERY_OST_DATA % ( 
                t0.strftime( _DATE_FMT ), 
                tf.strftime( _DATE_FMT ) 
            )
            tokio._debug_print( "Retrieving %s >= t > %s" % (
                t0.strftime(_DATE_FMT),
                tf.strftime(_DATE_FMT)))
            t0 += _TIME_CHUNK
            for ret_tup in self._gen_query_mysql( query_str ):
                yield ret_tup
        tokio._debug_print( 
            "Finished because t0(=%s) !< t_stop(=%s)" % (
            t0.strftime( _DATE_FMT ), 
            tf.strftime( _DATE_FMT )))


    def get_timestamp_map( self, t_start, t_stop ):
        """
        Get the timestamps associated with a t_start/t_stop from LMT
        """
        query_str = _QUERY_TIMESTAMP_MAPPING % (
            t_start.strftime( _DATE_FMT ), 
            t_stop.strftime( _DATE_FMT ) 
        )
        return self._gen_query_mysql( query_str )


    def get_last_rw_data_before( self, t, lookbehind=None ):
        """
        Get the last datum reported by each OST before the given timestamp t.
        Useful for calculating the change in bytes for the very first row
        returned by a query.

        Input:
            1. t is a datetime.datetime before which we want to find data
            2. lookbehind is a datetime.timedelta is how far back we're willing
               to look for valid data for each OST.  The larger this is, the
               slower the query
        Output is a tuple of:
            1. buf_r - a matrix of size (1, N) with the last read byte value for
               each of N OSTs
            2. buf_w - a matrix of size (1, N) with the last write byte value
               for each of N OSTs
            3. buf_t - a matrix of size (1, N) with the timestamp from which
               each buf_r and buf_w row datum was found
        """
        if lookbehind is None:
            lookbehind = datetime.timedelta(hours=1)

        lookbehind_str = "%d %02d:%02d:%02d" % (
            lookbehind.days,
            lookbehind.seconds / 3600, 
            lookbehind.seconds % 3600 / 60, 
            lookbehind.seconds % 60 )

        ost_ct = len(self.ost_names)
        buf_r = np.full( shape=(1, ost_ct), fill_value=-0.0, dtype='f8' )
        buf_w = np.full( shape=(1, ost_ct), fill_value=-0.0, dtype='f8' )
        buf_t = np.full( shape=(1, ost_ct), fill_value=-0.0, dtype='i8' )

        query_str = _QUERY_FIRST_OST_DATA.format( datetime=t.strftime( _DATE_FMT ), lookbehind=lookbehind_str )
        for tup in self._query_mysql( query_str ):
            try:
                tidx = self.ost_names.index( tup[1] )
            except ValueError:
                raise ValueError("unknown OST [%s] not present in %s" % (tup[1], self.ost_names))
            buf_r[0, tidx] = tup[2]
            buf_w[0, tidx] = tup[3]
            buf_t[0, tidx] = tup[0]

        return ( buf_r, buf_w, buf_t )


    def _query_mysql( self, query_str ):
        """
        Connects to MySQL, run a query, and yield the full output tuple.  No
        buffering or other witchcraft.
        """
        cursor = self.db.cursor()
        t0 = time.time()
        cursor.execute( query_str )
        tokio._debug_print("Executed query in %f sec" % ( time.time() - t0 ))

        t0 = time.time()
        rows = cursor.fetchall()
        tokio._debug_print("%d rows fetched in %f sec" % (_MYSQL_FETCHMANY_LIMIT, time.time() - t0))
        return rows



    def _gen_query_mysql( self, query_str ):
        """
        Generator function that connects to MySQL, runs a query, and yields
        output rows.
        """
        cursor = self.db.cursor()
        t0 = time.time()
        cursor.execute( query_str )
        tokio._debug_print("Executed query in %f sec" % ( time.time() - t0 ))

        ### Iterate over chunks of output
        while True:
            t0 = time.time()
            rows = cursor.fetchmany(_MYSQL_FETCHMANY_LIMIT)
            if rows == ():
                break
            for row in rows:
                yield row
            tokio._debug_print("%d rows fetched in %f sec" % (_MYSQL_FETCHMANY_LIMIT, time.time() - t0))


    def _gen_query_mysql_fetchmany( self, query_str ):
        """
        Generator function that connects to MySQL, runs a query, and yields
        multiple output rows at once.
        """
        t0 = time.time()
        cursor = self.db.cursor()
        cursor.execute( query_str ) ### this is what takes a long time
        tokio._debug_print("Executed query in %f sec" % ( time.time() - t0 ))

        while True:
            t0 = time.time()
            rows = cursor.fetchmany(_MYSQL_FETCHMANY_LIMIT)
            tokio._debug_print("fetchmany took %f sec" % ( time.time() - t0 ))
            if rows == ():
                break
            yield rows

if __name__ == '__main__':
    pass