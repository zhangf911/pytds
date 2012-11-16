import logging
from datetime import datetime
from StringIO import StringIO
from tds import *
from tdsproto import *
from read import *
from write import *
from tds_checks import *

logger = logging.getLogger(__name__)

#/**
# * Set type of column initializing all dependency 
# * @param curcol column to set
# * @param type   type to set
# */
def tds_set_column_type(tds, curcol, type):
    # set type
    curcol.on_server.column_type = type
    curcol.funcs = tds_get_column_funcs(tds, type)
    curcol.column_type = tds_get_cardinal_type(type, curcol.column_usertype)

    # set size
    curcol.column_cur_size = -1
    curcol.column_varint_size = tds_get_varint_size(tds, type)
    if curcol.column_varint_size == 0:
        curcol.column_cur_size = curcol.on_server.column_size = curcol.column_size = tds_get_size_by_type(type)

def tds_get_column_funcs(tds, type):
    if type in (SYBNUMERIC, SYBDECIMAL):
        return numeric_funcs
    elif type == SYBVARIANT:
        if IS_TDS7_PLUS(tds):
            return variant_funcs
    elif type in (SYBMSDATE, SYBMSTIME, SYBMSDATETIME2, SYBMSDATETIMEOFFSET):
        return msdatetime_funcs
    return default_funcs


def tds_get_cardinal_type(datatype, usertype):
    if datatype == XSYBVARBINARY:
        return SYBVARBINARY
    if datatype == XSYBBINARY:
        return SYBBINARY
    if datatype == SYBNTEXT:
        return SYBTEXT
    if datatype in (XSYBNVARCHAR, XSYBVARCHAR):
        return SYBVARCHAR
    if datatype in (XSYBNCHAR, XSYBCHAR):
        return SYBCHAR
    if datatype == SYB5INT8:
        return SYBINT8
    if datatype == SYBLONGBINARY:
        if usertype in (USER_UNICHAR_TYPE, USER_UNIVARCHAR_TYPE):
            return SYBTEXT
    return datatype

def tds_data_get_info(tds, col):
    vs = col.column_varint_size
    if vs == 8:
        col.column_size = 0x7fffffff
    elif vs in (4,5):
        col.column_size = tds_get_int(tds)
    elif vs == 2:
        # assure > 0
        col.column_size = tds_get_smallint(tds)
        # under TDS9 this means ?var???(MAX)
        if col.column_size < 0 and IS_TDS72_PLUS(tds):
            col.column_size = 0x3fffffff
            col.column_varint_size = 8
    elif vs == 1:
        col.column_size = tds_get_byte(tds)
    elif vs == 0:
        col.column_size = tds_get_size_by_type(col.column_type)

    if IS_TDS71_PLUS(tds) and is_collate_type(col.on_server.column_type):
        # based on true type as sent by server
        #
        # first 2 bytes are windows code (such as 0x409 for english)
        # other 2 bytes ???
        # last bytes is id in syscharsets
        #
        col.column_collation = tds_get_n(tds, 5)
        col.char_conv = tds_iconv_from_collate(tds, col.column_collation)

    # Only read table_name for blob columns (eg. not for SYBLONGBINARY)
    if is_blob_type(col.on_server.column_type):
        # discard this additional byte
        if IS_TDS72_PLUS(tds):
            num_parts = tds_get_byte(tds)
            # TODO do not discard first ones
            for _ in range(num_parts):
                col.table_name = tds_get_string(tds, tds_get_smallint(tds))
        else:
            col.table_name = tds_get_string(tds, tds_get_smallint(tds))
    elif IS_TDS72_PLUS(tds) and col.on_server.column_type == SYBMSXML:
        has_schema = tds_get_byte(tds)
        if has_schema:
            # discard schema informations
            tds_get_string(tds, tds_get_byte(tds))        # dbname
            tds_get_string(tds, tds_get_byte(tds))        # schema owner
            tds_get_string(tds, tds_get_smallint(tds))    # schema collection
    return TDS_SUCCESS

class _Blob():
    pass

#
# Read a data from wire
# \param tds state information for the socket and the TDS protocol
# \param curcol column where store column information
# \return TDS_FAIL on error or TDS_SUCCESS
#
def tds_data_get(tds, curcol):
    CHECK_TDS_EXTRA(tds)
    CHECK_COLUMN_EXTRA(curcol)

    logger.debug("tds_get_data: type %d, varint size %d" % (curcol.column_type, curcol.column_varint_size))
    cvs = curcol.column_varint_size
    if cvs == 4:
        #
        # LONGBINARY
        # This type just stores a 4-byte length
        #
        if curcol.column_type == SYBLONGBINARY:
            colsize = tds_get_int(tds)
        else:
            # It's a BLOB...
            size = tds_get_byte(tds)
            curcol.column_data = blob = _Blob()
            if size == 16: # Jeff's hack
                blob.textptr = tds_get_n(tds, 16)
                blob.timestamp = tds_get_n(tds, 8)
                colsize = tds_get_int(tds)
            else:
                colsize = -1
    elif cvs == 5:
        curcol.column_data = blob = _Blob()
        colsize = tds_get_int(tds)
        if colsize == 0:
            colsize = -1;
    elif cvs == 8:
        return tds72_get_varmax(tds, curcol)
    elif cvs == 2:
        colsize = tds_get_smallint(tds)
    elif cvs == 1:
        colsize = tds_get_byte(tds)
        if colsize == 0:
            colsize = -1
    elif cvs == 0:
        # TODO this should be column_size
        colsize = tds_get_size_by_type(curcol.column_type)
    else:
        colsize = -1;
    if IS_TDSDEAD(tds):
        raise Exception('TDS_FAIL')

    logger.debug("tds_get_data(): wire column size is %d" % colsize)
    # set NULL flag in the row buffer
    if colsize < 0:
        curcol.column_cur_size = -1
        return TDS_SUCCESS

    #
    # We're now set to read the data from the wire.  For varying types (e.g. char/varchar)
    # make sure that curcol->column_cur_size reflects the size of the read data, 
    # after any charset conversion.  tds_get_char_data() does that for you, 
    # but of course tds_get_n() doesn't.  
    #
    # colsize == wire_size, bytes to read
    # curcol->column_cur_size == sizeof destination buffer, room to write
    #
    if is_blob_col(curcol):
        # Blobs don't use a column's fixed buffer because the official maximum size is 2 GB.
        # Instead, they're reallocated as necessary, based on the data's size.
        # Here we allocate memory, if need be.
        #
        # TODO this can lead to a big waste of memory
        new_blob_size = colsize
        if new_blob_size == 0:
            curcol.column_cur_size = 0
            blob.textvalue = b''
            return TDS_SUCCESS

        curcol.column_cur_size = new_blob_size
        # read the data
        if USE_ICONV(tds) and curcol.char_conv:
            tds_get_char_data(tds, blob, colsize, curcol)
        else:
            assert colsize == new_blob_size
            blob.textvalue = tds_get_n(tds, colsize)
    else: # non-numeric and non-blob
        curcol.column_cur_size = colsize

        if USE_ICONV(tds) and curcol.char_conv:
            if colsize == 0:
                curcol.column_data = u''
            elif curcol.char_conv:
                curcol.column_data = read_and_convert(tds, curcol.char_conv, colsize)
            else:
                curcol.column_data = tds_get_n(tds, colsize)
            curcol.cur_size = len(curcol.column_data)
        else:
            #
            # special case, some servers seem to return more data in some conditions 
            # (ASA 7 returning 4 byte nullable integer)
            #
            discard_len = 0
            if colsize > curcol.column_size:
                discard_len = colsize - curcol.column_size
                colsize = curcol.column_size
            curcol.column_data = tds_get_n(tds, colsize)
            if discard_len > 0:
                tds_get_n(tds, discard_len)
            curcol.column_cur_size = colsize

        # pad (UNI)CHAR and BINARY types
        fillchar = '\0'
        if curcol.column_type in (SYBCHAR, XSYBCHAR) and (curcol.column_size == curcol.on_server.column_size):
            # FIXME use client charset
            fillchar = ' '
        # extra handling for SYBLONGBINARY
        if curcol.column_type == SYBLONGBINARY and curcol.column_usertype == USER_UNICHAR_TYPE or\
                curcol.column_type in (SYBCHAR, XSYBCHAR) and (curcol.column_size == curcol.on_server.column_size) or\
                curcol.column_type in (SYBBINARY, XSYBBINARY):

                    if colsize < curcol.column_size:
                        curcol.column_data.extend(fillchar*(curcol.column_size - colsize))
                    colsize = curcol.column_size

#ifdef WORDS_BIGENDIAN
#    /*
#        * MS SQL Server 7.0 has broken date types from big endian
#        * machines, this swaps the low and high halves of the
#        * affected datatypes
#        *
#        * Thought - this might be because we don't have the
#        * right flags set on login.  -mjs
#        *
#        * Nope its an actual MS SQL bug -bsb
#        */
#    /* TODO test on login, remove configuration -- freddy77 */
#    if (tds_conn(tds)->broken_dates &&
#        (curcol->column_type == SYBDATETIME ||
#            curcol->column_type == SYBDATETIME4 ||
#            curcol->column_type == SYBDATETIMN ||
#            curcol->column_type == SYBMONEY ||
#            curcol->column_type == SYBMONEY4 || (curcol->column_type == SYBMONEYN && curcol->column_size > 4)))
#            /*
#                * above line changed -- don't want this for 4 byte SYBMONEYN
#                * values (mlilback, 11/7/01)
#                */
#    {
#            unsigned char temp_buf[8];
#
#            memcpy(temp_buf, dest, colsize / 2);
#            memcpy(dest, &dest[colsize / 2], colsize / 2);
#            memcpy(&dest[colsize / 2], temp_buf, colsize / 2);
#    }
#    if (tds_conn(tds)->emul_little_endian) {
#            tdsdump_log(TDS_DBG_INFO1, "swapping coltype %d\n", tds_get_conversion_type(curcol->column_type, colsize));
#            tds_swap_datatype(tds_get_conversion_type(curcol->column_type, colsize), dest);
#    }
#endif
    return TDS_SUCCESS

def tds_data_row_len(tds):
    raise Exception('not implemented')

def tds72_get_varmax(tds, curcol):
    size = tds_get_int8(tds);

    # NULL
    if size == -1:
        curcol.column_cur_size = -1
        return TDS_SUCCESS

    blob = curcol.column_data = _Blob()
    chunk_handler = tds.chunk_handler
    chunk_handler.begin(curcol, size)
    decoder = None
    if curcol.char_conv:
        decoder = curcol.char_conv['codec'].incrementaldecoder()
    while True:
        chunk_len = tds_get_int(tds)
        if chunk_len <= 0:
            if decoder:
                val = decoder.decode('', True)
                chunk_handler.new_chunk(val)
            blob.textvalue = chunk_handler.end()
            curcol.column_cur_size = len(blob.textvalue)
            return TDS_SUCCESS
        val = tds_get_n(tds, chunk_len)
        if decoder:
            val = decoder.decode(val)
        chunk_handler.new_chunk(val)
    return TDS_SUCCESS

def tds_data_put_info(tds, col):
    size = tds_fix_column_size(tds, col)
    vs = col.column_varint_size
    if vs == 0:
        pass
    elif vs == 1:
        tds_put_byte(tds, size)
    elif vs == 2:
        tds_put_smallint(tds, size)
    elif vs in (4, 5):
        tds_put_int(tds, size)
    elif vs == 8:
        tds_put_smallint(tds, 0xffff)

    # TDS7.1 output collate information
    if IS_TDS71_PLUS(tds) and is_collate_type(col.on_server.column_type):
        tds_put_s(tds, tds.collation)

def tds_data_put(tds, curcol):
    logger.debug("tds_data_put: colsize = %d", curcol.column_cur_size)
    if curcol.value is None:
        logger.debug("tds_data_put: null param")
        vs = curcol.column_varint_size
        if vs == 5:
            tds_put_int(tds, 0)
        elif vs == 4:
            tds_put_int(tds, -1)
        elif vs == 2:
            tds_put_smallint(tds, -1)
        elif vs == 8:
            tds_put_int8(tds, -1)
        else:
            assert curcol.column_varint_size
            # FIXME not good for SYBLONGBINARY/SYBLONGCHAR (still not supported)
            tds_put_byte(tds, 0)
        return
    colsize = curcol.column_cur_size

    size = tds_fix_column_size(tds, curcol)

    # convert string if needed
    value = curcol.value
    if curcol.char_conv and colsize:
        # we need to convert data before
        # TODO this can be a waste of memory...
        value = tds_convert_string(tds, curcol.char_conv, value)
        colsize = len(value)

    #
    # TODO here we limit data sent with MIN, should mark somewhere
    # and inform client ??
    # Test proprietary behavior
    #
    if IS_TDS7_PLUS(tds):
        logger.debug("tds_data_put: not null param varint_size = %d",
                    curcol.column_varint_size)

        vs = curcol.column_varint_size
        if vs == 8:
            tds_put_int8(tds, colsize);
            tds_put_int(tds, colsize);
        elif vs == 4: # It's a BLOB...
            colsize = min(colsize, size)
            # mssql require only size
            tds_put_int(tds, colsize)
        elif vs == 2:
            colsize = min(colsize, size)
            tds_put_smallint(tds, colsize)
        elif vs == 1:
            colsize = min(colsize, size)
            tds_put_byte(tds, colsize)
        elif vs == 0:
            # TODO should be column_size
            colsize = tds_get_size_by_type(curcol.on_server.column_type)

        # put real data
        column_type = curcol.on_server.column_type
        if column_type == SYBINTN and colsize == 4:
            tds_put_int(tds, value)
        elif column_type == SYBINTN and colsize == 8:
            tds_put_int8(tds, value)
        elif column_type in (XSYBNVARCHAR, XSYBNCHAR):
            tds_put_s(tds, value)
        elif column_type in (XSYBVARBINARY, XSYBBINARY):
            tds_put_s(tds, value)
        elif column_type in (SYBDATETIME, SYBDATETIMN):
            days = (value - _base_date).days
            time = (value.hour * 60 * 60 + value.minute * 60 + value.second)*300 + value.microsecond/1000/3
            tds_put_s(tds, TDS_DATETIME.pack(days, time))
        else:
            raise Exception('not implemented')
        # finish chunk for varchar/varbinary(max)
        if curcol.column_varint_size == 8 and colsize:
            tds_put_int(tds, 0)
    else:
        raise Exception('not implemented')
        # TODO ICONV handle charset conversions for data 
        # put size of data 
#        switch (curcol->column_varint_size) {
#        case 5:	/* It's a LONGBINARY */
#                colsize = MIN(colsize, 0x7fffffff);
#                tds_put_int(tds, colsize);
#                break;
#        case 4:	/* It's a BLOB... */
#                tds_put_byte(tds, 16);
#                tds_put_n(tds, blob->textptr, 16);
#                tds_put_n(tds, blob->timestamp, 8);
#                colsize = MIN(colsize, 0x7fffffff);
#                tds_put_int(tds, colsize);
#                break;
#        case 2:
#                colsize = MIN(colsize, 8000);
#                tds_put_smallint(tds, colsize);
#                break;
#        case 1:
#                if (!colsize) {
#                        tds_put_byte(tds, 1);
#                        if (is_char_type(curcol->column_type))
#                                tds_put_byte(tds, ' ');
#                        else
#                                tds_put_byte(tds, 0);
#                        return TDS_SUCCESS;
#                }
#                colsize = MIN(colsize, 255);
#                tds_put_byte(tds, colsize);
#                break;
#        case 0:
#                /* TODO should be column_size */
#                colsize = tds_get_size_by_type(curcol->column_type);
#                break;
#        }
#
#        /* conversion error, exit with an error */
#        if (converted < 0)
#                return TDS_FAIL;
#
#        /* put real data */
#        if (blob) {
#                tds_put_n(tds, s, colsize);
#        } else {
##ifdef WORDS_BIGENDIAN
#                unsigned char buf[64];
#
#                if (tds_conn(tds)->emul_little_endian && !converted && colsize < 64) {
#                        tdsdump_log(TDS_DBG_INFO1, "swapping coltype %d\n",
#                                    tds_get_conversion_type(curcol->column_type, colsize));
#                        memcpy(buf, s, colsize);
#                        tds_swap_datatype(tds_get_conversion_type(curcol->column_type, colsize), buf);
#                        s = (char *) buf;
#                }
##endif
#                tds_put_n(tds, s, colsize);

class _ColumnFuncs(object):
    pass

def DEFINE_FUNCS(prefix, name):
    g = globals()
    funcs = _ColumnFuncs()
    funcs.get_info = g['tds_{0}_get_info'.format(name)]
    funcs.get_data = g['tds_{0}_get'.format(name)]
    funcs.row_len = g['tds_{0}_row_len'.format(name)]
    funcs.put_info = g['tds_{0}_put_info'.format(name)]
    funcs.put_data = g['tds_{0}_put'.format(name)]
    g[prefix+'_funcs'] = funcs

DEFINE_FUNCS('default', 'data')

def tds_numeric_get_info(tds, col):
    col.column_size = tds_get_byte(tds)
    col.column_prec = tds_get_byte(tds)
    col.column_scale = tds_get_byte(tds)
    # FIXME check prec/scale, don't let server crash us
    return TDS_SUCCESS

numeric_format = struct.Struct('BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB')

def tds_numeric_row_len(col):
    return numeric_format.size

class _Numeric:
    pass

def tds_numeric_get(tds,  curcol):
    CHECK_TDS_EXTRA(tds)
    CHECK_COLUMN_EXTRA(curcol)

    import pdb; pdb.set_trace()
    colsize = tds_get_byte(tds)

    # set NULL flag in the row buffer
    if colsize <= 0:
        curcol.column_cur_size = -1
        return TDS_SUCCESS

    #
    # Since these can be passed around independent
    # of the original column they came from, we embed the TDS_NUMERIC datatype in the row buffer
    # instead of using the wire representation, even though it uses a few more bytes.
    #
    curcol.column_data = num = _Numeric()
    # TODO perhaps it would be fine to change format ??
    num.precision = curcol.column_prec
    num.scale = curcol.column_scale

    # server is going to crash freetds ??
    # TODO close connection it server try to do so ??
    if colsize > 33:
        return TDS_FAIL
    num.array = tds_get_n(tds, colsize)

    if IS_TDS7_PLUS(tds):
        from token import tds_swap_numeric
        tds_swap_numeric(num)

    # corrected colsize for column_cur_size
    curcol.column_cur_size = numeric_format.size

    return TDS_SUCCESS

def tds_numeric_put_info(tds):
    raise Exception('not implemented')

def tds_numeric_put(tds):
    raise Exception('not implemented')

DEFINE_FUNCS('numeric', 'numeric')
def gen_get_varint_size():
    table = '''\
name	vendor	varint	fixed	nullable	variable	blob	numeric	unicode	ascii	size	nullable type
SYB5INT8	SYB	0	1	0	0	0	0	0	0	8	SYBINTN
SYBBINARY	ALL	1	0	1	1	0	0	0	0	-1	0
SYBBIT	ALL	0	1	0	0	0	0	0	0	1	SYBBITN
SYBBITN	MS	1	0	1	0	0	0	0	0	1	0	# Sybase bit is not nullable
SYBBLOB	SYB	??	0	1	1	1	0	Depend	Depend	-1	0
SYBBOUNDARY	SYB	1	0	1	1	0	0	0	1	-1	0
SYBCHAR	ALL	1	0	1	1	0	0	0	1	-1	0
SYBDATE	SYB	0	1	0	0	0	0	0	0	4	SYBDATEN
SYBDATEN	SYB	1	0	1	0	0	0	0	0	4	0
SYBDATETIME	ALL	0	1	0	0	0	0	0	0	8	SYBDATETIMN
SYBDATETIME4	ALL	0	1	0	0	0	0	0	0	4	SYBDATETIMN
SYBDATETIMN	ALL	1	0	1	0	0	0	0	0	-1	0
SYBMSDATE	MS	1	0	1	0	0	0	0	0	4	0
SYBMSTIME	MS	1	0	1	0	0	0	0	0	-1	0
SYBMSDATETIME2	MS	1	0	1	0	0	0	0	0	-1	0
SYBMSDATETIMEOFFSET	MS	1	0	1	0	0	0	0	0	-1	0
SYBDECIMAL	ALL	1	0	1	0	0	1	0	0	-1	0
SYBFLT8	ALL	0	1	0	0	0	0	0	0	8	SYBFLTN
SYBFLTN	ALL	1	0	1	0	0	0	0	0	-1	0
SYBIMAGE	ALL	4	0	1	1	1	0	0	0	-1	0
SYBINT1	ALL	0	1	0	0	0	0	0	0	1	SYBINTN
SYBINT2	ALL	0	1	0	0	0	0	0	0	2	SYBINTN
SYBINT4	ALL	0	1	0	0	0	0	0	0	4	SYBINTN
SYBINT8	MS	0	1	0	0	0	0	0	0	8	SYBINTN
SYBINTERVAL	SYB	0	1	0	0	0	0	0	0	8	0
SYBINTN	ALL	1	0	1	0	0	0	0	0	-1	0
SYBLONGBINARY	SYB	5	0	1	1	1	0	Depend	Depend	-1	0
SYBLONGCHAR	SYB	5	0	1	1	1	0	??	??	-1	0
SYBMONEY	ALL	0	1	0	0	0	0	0	0	8	SYBMONEYN
SYBMONEY4	ALL	0	1	0	0	0	0	0	0	4	SYBMONEYN
SYBMONEYN	ALL	1	0	1	0	0	0	0	0	-1	0
SYBMSUDT	MS	??	0	1	1	??	0	??	??	-1	0
SYBMSXML	MS	8	0	1	1	1	0	1	0	-1	0
SYBNTEXT	MS	4	0	1	1	1	0	1	0	-1	0
SYBNUMERIC	ALL	1	0	1	0	0	1	0	0	-1	0
SYBNVARCHAR	MS	??	0	1	1	0	0	1	0	-1	0	# Same as XSYBNVARCHAR ??
SYBREAL	ALL	0	1	0	0	0	0	0	0	4	SYBFLTN
SYBSENSITIVITY	SYB	1	0	1	1	0	0	0	1	-1	0
SYBSINT1	SYB	0	1	0	0	0	0	0	0	1	0
SYBTEXT	ALL	4	0	1	1	1	0	0	1	-1	0
SYBTIME	SYB	0	1	0	0	0	0	0	0	4	SYBTIMEN
SYBTIMEN	SYB	1	0	1	0	0	0	0	0	4	0
SYBUINT1	SYB	0	1	0	0	0	0	0	0	1	SYBUINTN
SYBUINT2	SYB	0	1	0	0	0	0	0	0	2	SYBUINTN
SYBUINT4	SYB	0	1	0	0	0	0	0	0	4	SYBUINTN
SYBUINT8	SYB	0	1	0	0	0	0	0	0	8	SYBUINTN
SYBUINTN	SYB	1	0	1	0	0	0	0	0	-1	0
SYBUNIQUE	MS	1	0	1	0	0	0	0	0	16	0	# have size but is nullable
SYBUNITEXT	SYB	4	0	1	1	1	0	1	0	-1	0	# UTF-16
SYBVARBINARY	ALL	1	0	1	1	0	0	0	0	-1	0
SYBVARCHAR	ALL	1	0	1	1	0	0	0	1	-1	0
SYBVARIANT	MS	4	0	1	1	0	0	Depend	Depend	-1	0	# varint ok ?
SYBVOID	MS ??	0	1	0	0	0	0	0	0	0	0
SYBXML	SYB	4	0	1	1	1	0	??	??	-1	0
XSYBBINARY	MS	2	0	1	1	0	0	0	0	-1	0
XSYBCHAR	MS	2	0	1	1	0	0	0	1	-1	0
XSYBNCHAR	MS	2	0	1	1	0	0	1	0	-1	0
XSYBNVARCHAR	MS	2	0	1	1	0	0	1	0	-1	0
XSYBVARBINARY	MS	2	0	1	1	0	0	0	0	-1	0
XSYBVARCHAR	MS	2	0	1	1	0	0	0	1	-1	0

# XSYBVARCHAR blob se TDS9 ??
# char if ascii or unicode
# there are some type that allow size 0 or a constants (SYBDATEN, SYBUNIQUE)
# some type (BITN, DATEN, UNIQUE, MSDATE) have size but are nullable
# tds_get_conversion_type from nullable to not nullable
# tds_get_cardinal_type ??

# $Id: types.txt,v 1.5 2011/05/12 19:40:57 freddy77 Exp $
'''
    lines = map(lambda l: l.split('\t'), filter(lambda l: l and not l.startswith('#'), table.split('\n')))
    header = lines[0]
    data = lines[1:]
    hd = dict((b, a) for a, b in enumerate(header))
    h = lambda l, name: l[hd[name]]
    from itertools import groupby

    gen_varint = [(h(l, 'name'), h(l, 'varint')) for l in data if h(l, 'varint') not in ('1', '??') and h(l, 'vendor').upper() not in ('MS', 'SYB')]
    ms_varint = [(h(l, 'name'), h(l, 'varint')) for l in data if h(l, 'varint') not in ('1', '??') and h(l, 'vendor').upper() == 'MS']
    syb_varint = [(h(l, 'name'), h(l, 'varint')) for l in data if h(l, 'varint') not in ('1', '??') and h(l, 'vendor').upper() == 'SYB']
    types_sizes = [(h(l, 'name'), h(l, 'size')) for l in data]
    fmt = '''\
def tds_get_varint_size(tds, datatype):
{0}
    if IS_TDS7_PLUS(tds):
{1}
    elif IS_TDS50(tds):
{2}
    return 1

def tds_get_size_by_type(servertype):
{3}

def tds_get_conversion_type(srctype, colsize):
    raise Exception('not implemented')
'''
    keyfunc = lambda (_, varint): varint
    code = fmt.format(
            '\n'.join('    if datatype in ({0},): return {1}'.format(','.join(name for name, _ in g), k) for k, g in groupby(sorted(gen_varint, key=keyfunc), keyfunc)),
            '\n'.join('        if datatype in ({0},): return {1}'.format(','.join(name for name, _ in g), k) for k, g in groupby(sorted(ms_varint, key=keyfunc), keyfunc)),
            '\n'.join('        if datatype in ({0},): return {1}'.format(','.join(name for name, _ in g), k) for k, g in groupby(sorted(syb_varint, key=keyfunc), keyfunc)),
            '\n'.join('    if servertype in ({0},): return {1}'.format(','.join(name for name, _ in g), k) for k, g in groupby(sorted(types_sizes, key=lambda (_, size): size), lambda (_, size): size)),
            )
    return compile(code, 'autogenerated_types', 'exec')

exec(gen_get_varint_size())

def USE_ICONV(tds): return tds_conn(tds).use_iconv

#
# Get column size for wire
#
def tds_fix_column_size(tds, curcol):
    size = curcol.on_server.column_size

    if not size:
        size = curcol.column_size
        if is_unicode_type(curcol.on_server.column_type):
            size *= 2

    vs = curcol.column_varint_size
    if vs == 0:
        return size
    elif vs == 1:
        size = max(min(size, 255), 1)
    elif vs == 2:
        if curcol.on_server.column_type in (XSYBNVARCHAR, XSYBNCHAR):
            mn = 2
        else:
            mn = 1
        size = max(min(size, 8000), mn)
    elif vs == 4:
        if curcol.on_server.column_type == SYBNTEXT:
            size = max(min(size, 0x7ffffffe), 2)
        else:
            size = max(min(size, 0x7fffffff), 1)
    #return curcol->on_server.column_size = size
    return size

def tds_convert_string(tds, char_conv, s):
    return char_conv['to_wire'](s)

def tds_msdatetime_get_info(tds, col):
    raise Exception('not implemented')
    col.column_scale = col.column_prec = 0
    if col.column_type != SYBMSDATE:
        col.column_scale = col.column_prec = tds_get_byte(tds)
        if col.column_prec > 7:
            raise Exception('TDS_FAIL')
    col.on_server.column_size = col.column_size = sizeof(TDS_DATETIMEALL)

def tds_msdatetime_get(tds, col):
    raise Exception('unimplemented')

def tds_msdatetime_row_len(col):
    raise Exception('unneeded')

def tds_msdatetime_put_info(tds, col):
    # TODO precision
    if col.on_server.column_type != SYBMSDATE:
        tds_put_byte(tds, 7)

_base_date = datetime(1900, 1, 1)

def tds_msdatetime_put(tds, col):
    if col.value is None:
        tds_put_byte(tds, 0)
        return

    # TODO precision
    value = col.value
    parts = []
    if col.on_server.column_type != SYBMSDATE:
        # TODO: fix this
        parts.append(struct.pack('<L', value.second)[:5])
    if col.on_server.column_type != SYBMSTIME:
        parts.append(struct.pack('<l', (value - _base_date).days)[:3])
    if col.on_server.column_type == SYBMSDATETIMEOFFSET:
        parts.append(struct.pack('<H', value.utcoffset()))
    size = reduce(lambda a, b: a + len(b), parts, 0)
    parts.insert(0, chr(size))
    tds_put_s(tds, b''.join(parts))

DEFINE_FUNCS('msdatetime', 'msdatetime')
