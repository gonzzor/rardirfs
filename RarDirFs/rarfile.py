# -*- coding: utf-8 -*-
#
# Copyright (c) 2009, Jonas Jonsson <jonas@websystem.se>
# All rights reserved.
#
# See file LICENSE for license details
#
# Copyright (c) 2005-2008  Marko Kreen <markokr@gmail.com>
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

"""
    RAR archive reader.

    Modifed to support partial reading of uncompressed archives with only one
    file.
"""

import os, re
from struct import pack, unpack
from binascii import crc32
from cStringIO import StringIO
from tempfile import mkstemp

# export only interesting items
__all__ = ['is_rarfile', 'RarInfo', 'RarFile']

# whether to speed up decompression by using tmp archive
_use_extract_hack = 1

# command line to use for extracting
_extract_cmd = 'unrar p -inul "%s" "%s"'

class Error(Exception):
    """Base class for rarfile errors."""
class BadRarFile(Error):
    """Incorrect data in archive."""
class NotRarFile(Error):
    """The file is not RAR archive."""
class BadRarName(Error):
    """Cannot guess multipart name components."""
class NoRarEntry(Error):
    """File not found in RAR"""

#
# rar constants
#

RAR_ID = "Rar!\x1a\x07\x00"

# block types
RAR_BLOCK_MARK          = 0x72 # r
RAR_BLOCK_MAIN          = 0x73 # s
RAR_BLOCK_FILE          = 0x74 # t
RAR_BLOCK_OLD_COMMENT   = 0x75 # u
RAR_BLOCK_OLD_EXTRA     = 0x76 # v
RAR_BLOCK_OLD_SUB       = 0x77 # w
RAR_BLOCK_OLD_RECOVERY  = 0x78 # x
RAR_BLOCK_OLD_AUTH      = 0x79 # y
RAR_BLOCK_SUB           = 0x7a # z
RAR_BLOCK_ENDARC        = 0x7b # {

# main header flags
RAR_MAIN_VOLUME         = 0x0001
RAR_MAIN_COMMENT        = 0x0002
RAR_MAIN_LOCK           = 0x0004
RAR_MAIN_SOLID          = 0x0008
RAR_MAIN_NEWNUMBERING   = 0x0010
RAR_MAIN_AUTH           = 0x0020
RAR_MAIN_RECOVERY       = 0x0040
RAR_MAIN_PASSWORD       = 0x0080
RAR_MAIN_FIRSTVOLUME    = 0x0100

# file header flags
RAR_FILE_SPLIT_BEFORE   = 0x0001
RAR_FILE_SPLIT_AFTER    = 0x0002
RAR_FILE_PASSWORD       = 0x0004
RAR_FILE_COMMENT        = 0x0008
RAR_FILE_SOLID          = 0x0010
RAR_FILE_DICTMASK       = 0x00e0
RAR_FILE_DICT64         = 0x0000
RAR_FILE_DICT128        = 0x0020
RAR_FILE_DICT256        = 0x0040
RAR_FILE_DICT512        = 0x0060
RAR_FILE_DICT1024       = 0x0080
RAR_FILE_DICT2048       = 0x00a0
RAR_FILE_DICT4096       = 0x00c0
RAR_FILE_DIRECTORY      = 0x00e0
RAR_FILE_LARGE          = 0x0100
RAR_FILE_UNICODE        = 0x0200
RAR_FILE_SALT           = 0x0400
RAR_FILE_VERSION        = 0x0800
RAR_FILE_EXTTIME        = 0x1000
RAR_FILE_EXTFLAGS       = 0x2000

RAR_ENDARC_NEXT_VOLUME  = 0x0001
RAR_ENDARC_DATACRC      = 0x0002
RAR_ENDARC_REVSPACE     = 0x0004

# flags common to all blocks
RAR_SKIP_IF_UNKNOWN     = 0x4000
RAR_LONG_BLOCK          = 0x8000

# Host OS types
RAR_OS_MSDOS = 0
RAR_OS_OS2   = 1
RAR_OS_WIN32 = 2
RAR_OS_UNIX  = 3

#
# Public interface
#

def is_rarfile(fn):
    '''Check quickly whether file is rar archive.'''
    buf = open(fn, "rb").read(len(RAR_ID))
    return buf == RAR_ID

class RarInfo:
    '''An entry in rar archive.'''

    compress_size = None
    file_size = None
    host_os = None
    CRC = None
    date_time = None        # tuple of (year, mon, day, hr, min, sec)
    extract_version = None
    compress_type = None
    name_size = None
    mode = None
    flags = None
    type = None
    filename = None
    unicode_filename = None

    # RAR internals
    header_size = None
    header_crc = None
    file_offset = None
    add_size = None
    next_file_offset = None # file_offset for next volume
    next_add_size = None # add_size for next volume
    next_compress_size = None # compress_size for next volume
    header_data = None
    header_unknown = None
    header_offset = None
    volume = None

    def isdir(self):
        '''Returns True if the entry is a directory.'''
        if self.type == RAR_BLOCK_FILE:
            return (self.flags & RAR_FILE_DIRECTORY) == RAR_FILE_DIRECTORY
        return False

class RarFile:
    '''Rar archive handling.'''

    def __init__(self, rarfile, mode="r", charset=None, info_callback=None, only_first='no'):
        self.rarfile = rarfile
        self.charset = charset

        self.info_list = {}
        self.is_solid = 0
        self.uses_newnumbering = 0
        self.uses_volumes = 0
        self.info_callback = info_callback
        self.got_mainhdr = 0
        self._gen_volname = self._gen_oldvol
        self.only_first = only_first
        self.has_comment = False

        if not only_first in ('yes', 'no', 'auto'):
            raise ValueError('only_first only accepts yes, no and auto')

        if mode != "r":
            raise NotImplementedError("RarFile supports only mode=r")

        self._parse()

    def namelist(self):
        '''Return list of filenames in rar'''
        return self.info_list.keys()

    def infolist(self):
        '''Return rar entries.'''
        return self.info_list.values()

    def getinfo(self, fname):
        '''Return RarInfo for fname.'''
        ret = self.info_list.get(fname)
        if not ret:
            fname = fname.replace("/", "\\")
            ret = self.info_list.get(fname)

            if not ret:
                raise NoRarEntry("No such file")

        return ret

    def read(self, fname):
        '''Return decompressed data.'''
        inf = self.getinfo(fname)

        if inf.isdir():
            raise TypeError("Directory does not have any data")

        if inf.compress_type == 0x30:
            res = self._extract_clear(inf)
        elif _use_extract_hack and not self.is_solid and not self.uses_volumes:
            res = self._extract_hack(inf)
        else:
            res = self._extract_unrar(self.rarfile, inf)

        if crc32(res) != inf.CRC:
            raise BadRarFile('CRC check failed')

        return res

    def read_partial(self, fname, offset, length):
        '''Read just a part of a file'''
        inf = self.getinfo(fname)

        if inf.isdir():
            raise TypeError("Directory does not have any data")

        if inf.compress_type == 0x30:
            ret = self._extract_clear_partial(inf, offset, length)
        else:
            raise TypeError("Only STORE method support")

        return ret

    def close(self):
        """Release open resources."""
        pass

    def printdir(self):
        """Print archive file list to stdout."""
        for f in self.info_list:
            print f

    # store entry
    def _process_entry(self, item):
        # RAR_BLOCK_NEWSUB has files too: CMT, RR
        if item.type == RAR_BLOCK_SUB:
            if item.filename == 'CMT':
                self.has_comment = True

        if item.type == RAR_BLOCK_FILE:
            # If we only want first, skip this step
            if self.only_first == 'yes' and not self._must_read_next(item.volume):
                return

            # use only first part
            if (item.flags & RAR_FILE_SPLIT_BEFORE) == 0:
                self.info_list[item.filename] = item
            else:
                # Add information about second part, used in _extract_partial
                inf = self.info_list[item.filename]
                if not inf.next_add_size:
                    inf.next_add_size = item.add_size
                if not inf.next_file_offset:
                    inf.next_file_offset = item.file_offset
                if not inf.next_compress_size:
                    inf.next_compress_size = item.compress_size

        if self.info_callback:
            self.info_callback(item)

    def _must_read_next(self, volume):
        """Determine if the next volume must be read"""
        # It is only the second volume that might of extra interest
        if volume > 0:
            return False

        # If there is no info_list, read the next volume
        if not self.info_list:
            return True

        # Is the archive split?
        split_after = not self.info_list.values()[0].flags & RAR_FILE_SPLIT_AFTER == 0

        if self.has_comment and split_after:
            return True

        return False

    # read rar
    def _parse(self):
        fd = open(self.rarfile, "rb")
        id = fd.read(len(RAR_ID))
        if id != RAR_ID:
            raise NotRarFile("Not a Rar archive")

        volume = 0  # first vol (.rar) is 0
        more_vols = 0
        while 1:
            h = self._parse_header(fd)
            if not h:
                # If we don't have a comment, the next volume RAR_BLOCK_FILE
                # will start at the same position. However if a comment is
                # present and the file is split into more than one archive,
                # continue and read next archive.
                if not self._must_read_next(volume):
                    if self.only_first == 'yes':
                        break
                    if self.only_first == 'auto' and len(self.info_list) == 1:
                        break
                if more_vols:
                    volume += 1
                    fd = open(self._gen_volname(volume), "rb")
                    more_vols = 0
                    if fd:
                        continue
                break
            h.volume = volume

            if h.type == RAR_BLOCK_MAIN and not self.got_mainhdr:
                if h.flags & RAR_MAIN_NEWNUMBERING:
                    self.uses_newnumbering = 1
                    self._gen_volname = self._gen_newvol
                self.uses_volumes = h.flags & RAR_MAIN_VOLUME
                self.is_solid = h.flags & RAR_MAIN_SOLID
                self.got_mainhdr = 1
            elif h.type == RAR_BLOCK_ENDARC:
                more_vols = h.flags & RAR_ENDARC_NEXT_VOLUME

            # store it
            self._process_entry(h)

            # skip data
            if h.add_size > 0:
                fd.seek(h.add_size, 1)

    # read single header
    def _parse_header(self, fd):
        h = self._parse_block_header(fd)
        if h and (h.type == RAR_BLOCK_FILE or h.type == RAR_BLOCK_SUB):
            self._parse_file_header(h)
        return h

    # common header
    def _parse_block_header(self, fd):
        HDRLEN = 7
        h = RarInfo()
        h.header_offset = fd.tell()
        buf = fd.read(HDRLEN)
        if not buf:
            return None

        t = unpack("<HBHH", buf)
        h.header_crc, h.type, h.flags, h.header_size = t
        h.header_unknown = h.header_size - HDRLEN

        if h.header_size > HDRLEN:
            h.header_data = fd.read(h.header_size - HDRLEN)
        else:
            h.header_data = ""
        h.file_offset = fd.tell()

        if h.flags & RAR_LONG_BLOCK:
            h.add_size = unpack("<L", h.header_data[:4])[0]
        else:
            h.add_size = 0

        # no crc check on that
        if h.type == RAR_BLOCK_MARK:
            return h

        # check crc
        if h.type == RAR_BLOCK_MAIN:
            crcdat = buf[2:] + h.header_data[:6]
        elif h.type == RAR_BLOCK_OLD_AUTH:
            crcdat = buf[2:] + h.header_data[:8]
        else:
            crcdat = buf[2:] + h.header_data
        calc_crc = crc32(crcdat) & 0xFFFF

        # return good header
        if h.header_crc == calc_crc:
            return h

        # crc failed
        #print "CRC mismatch! ofs =", h.header_offset

        # instead panicing, send eof
        return None

    # read file-specific header
    def _parse_file_header(self, h):
        HDRLEN = 4+4+1+4+4+1+1+2+4
        fld = unpack("<LLBlLBBHL", h.header_data[ : HDRLEN])
        h.compress_size = long(fld[0]) & 0xFFFFFFFFL
        h.file_size = long(fld[1]) & 0xFFFFFFFFL
        h.host_os = fld[2]
        h.CRC = fld[3]
        h.date_time = self._parse_dos_time(fld[4])
        h.extract_version = fld[5]
        h.compress_type = fld[6]
        h.name_size = fld[7]
        h.mode = fld[8]
        pos = HDRLEN

        if h.flags & RAR_FILE_LARGE:
            h1, h2 = unpack("<LL", h.header_data[pos:pos+8])
            h.compress_size |= long(h1) << 32
            h.file_size |= long(h2) << 32
            pos += 8

        name = h.header_data[pos : pos + h.name_size ]
        pos += h.name_size
        if h.flags & RAR_FILE_UNICODE:
            nul = name.find("\0")
            h.filename = name[:nul]
            u = _UnicodeFilename(h.filename, name[nul + 1 : ])
            h.unicode_filename = u.decode()
        else:
            h.filename = name
            h.unicode_filename = None
            if self.charset:
                h.unicode_filename = name.decode(self.charset)
            else:
                # just guessing...
                h.unicode_filename = name.decode("iso-8859-1", "replace")

        if h.flags & RAR_FILE_SALT:
            h.salt = h.header_data[pos : pos + 8]
            pos += 8
        else:
            h.salt = None

        # unknown contents
        if h.flags & RAR_FILE_EXTTIME:
            h.ext_time = h.header_data[pos : ]
        else:
            h.ext_time = None

        h.header_unknown -= pos

        return h

    def _parse_dos_time(self, stamp):
        sec = stamp & 0x1F; stamp = stamp >> 5
        min = stamp & 0x3F; stamp = stamp >> 6
        hr  = stamp & 0x1F; stamp = stamp >> 5
        day = stamp & 0x1F; stamp = stamp >> 5
        mon = stamp & 0x0F; stamp = stamp >> 4
        yr = (stamp & 0x7F) + 1980
        return (yr, mon, day, hr, min, sec)

    # new-style volume name
    def _gen_newvol(self, volume):
        # allow % in filenames
        fn = self.rarfile.replace("%", "%%")

        m = re.search(r"([0-9][0-9]*)[^0-9]*$", fn)
        if not m:
            raise BadRarName("Cannot construct volume name")
        n1 = m.start(1)
        n2 = m.end(1)
        fmt = "%%0%dd" % (n2 - n1)
        volfmt = fn[:n1] + fmt + fn[n2:]
        return volfmt % (volume + 1)

    # old-style volume naming
    def _gen_oldvol(self, volume):
        if volume == 0: return self.rarfile
        i = self.rarfile.rfind(".")
        base = self.rarfile[:i]
        if self.rarfile[-3:] == '001':
          ext = '.%03d' % (volume + 1)
        elif volume <= 100:
            ext = ".r%02d" % (volume - 1)
        else:
            ext = ".s%02d" % (volume - 101)
        return base + ext

    # read uncompressed file
    def _extract_clear(self, inf):
        volume = inf.volume
        buf = ""
        cur = None
        while 1:
            f = open(self._gen_volname(volume), "rb")
            if not cur:
                f.seek(inf.header_offset)

            while 1:
                cur = self._parse_header(f)
                if cur.type in (RAR_BLOCK_MARK, RAR_BLOCK_MAIN):
                    if cur.add_size:
                        f.seek(cur.add_size, 1)
                    continue
                if cur.filename == inf.filename:
                    buf += f.read(cur.add_size)
                    break

                raise BadRarFile("Did not found file entry")

            # no more parts?
            if (cur.flags & RAR_FILE_SPLIT_AFTER) == 0:
                break

            volume += 1

        return buf

    def _extract_clear_partial(self, inf, offset, length):
        '''Read an uncompressed file partially'''

        if offset > inf.file_size:
            return ''

        if offset + length > inf.file_size:
            length = inf.file_size - offset

        if not inf.add_size:
            inf.add_size = inf.compress_size

        if not inf.next_add_size:
            inf.next_add_size = inf.add_size
            inf.next_file_offset = inf.file_offset
            inf.next_compress_size = inf.compress_size

        if offset > inf.add_size:
            volume = inf.volume + 1 + (offset - inf.add_size) / inf.next_add_size
            volume_offset = (offset - inf.add_size) % inf.next_add_size
            volume_length = inf.next_add_size - volume_offset
            file_offset = inf.next_file_offset
        else:
            volume = inf.volume
            volume_offset = offset
            volume_length = inf.add_size - volume_offset
            file_offset = inf.file_offset

        if length < volume_length:
          volume_length = length

        buf = ""
        while length > 0:
            f = open(self._gen_volname(volume), "rb")
            f.seek(file_offset + volume_offset)
            buf += f.read(volume_length)
            f.close()
            length -= volume_length

            volume_offset = 0
            volume += 1
            file_offset = inf.next_file_offset
            if length < inf.next_add_size:
                volume_length = length
            else:
                volume_length = inf.next_add_size

        return buf

    # put file compressed data into temporary .rar archive, and run
    # unrar on that, thus avoiding unrar going over whole archive
    def _extract_hack(self, inf):
        BSIZE = 32*1024

        size = inf.compress_size + inf.header_size
        rf = open(self.rarfile, "rb")
        rf.seek(inf.header_offset)

        tmpfd, tmpname = mkstemp(suffix='.rar')
        tmpf = os.fdopen(tmpfd, "wb")

        try:
            # create main header: crc, type, flags, size, res1, res2
            mh = pack("<HBHHHL", 0x90CF, 0x73, 0, 13, 0, 0)
            tmpf.write(RAR_ID + mh)
            while size > 0:
                if size > BSIZE:
                    buf = rf.read(BSIZE)
                else:
                    buf = rf.read(size)
                if not buf:
                    raise BadRarFile('read failed - broken archive')
                tmpf.write(buf)
                size -= len(buf)
            tmpf.close()

            buf = self._extract_unrar(tmpname, inf)
            return buf
        finally:
            os.unlink(tmpname)

    # extract using unrar
    def _extract_unrar(self, rarfile, inf):
        fn = inf.filename
        # linux unrar wants '/', not '\'
        fn = fn.replace("\\", "/")
        # shell escapes
        fn = fn.replace("`", "\\`")
        fn = fn.replace('"', '\\"')
        fn = fn.replace("$", "\\$")

        cmd = _extract_cmd % (rarfile, fn)
        fd = os.popen(cmd, "r")
        buf = fd.read()
        err = fd.close()
        if err > 0:
            raise BadRarFile("Error while unpacking file")
        return buf

class _UnicodeFilename:
    def __init__(self, name, encdata):
        self.std_name = name
        self.encdata = encdata
        self.pos = self.encpos = 0
        self.buf = StringIO()

    def enc_byte(self):
        c = self.encdata[self.encpos]
        self.encpos += 1
        return ord(c)

    def std_byte(self):
        return ord(self.std_name[self.pos])

    def put(self, lo, hi):
        self.buf.write(chr(lo) + chr(hi))
        self.pos += 1

    def decode(self):
        hi = self.enc_byte()
        flagbits = 0
        while self.encpos < len(self.encdata):
            if flagbits == 0:
                flags = self.enc_byte()
                flagbits = 8
            flagbits -= 2
            t = (flags >> flagbits) & 3
            if t == 0:
                self.put(self.enc_byte(), 0)
            elif t == 1:
                self.put(self.enc_byte(), hi)
            elif t == 2:
                self.put(self.enc_byte(), self.enc_byte())
            else:
                n = self.enc_byte()
                if n & 0x80:
                    c = self.enc_byte()
                    for i in range((n & 0x7f) + 2):
                        lo = (self.std_byte() + c) & 0xFF
                        self.put(lo, hi)
                else:
                    for i in range(n + 2):
                        self.put(self.std_byte(), 0)
        return self.buf.getvalue().decode("utf-16le", "replace")
