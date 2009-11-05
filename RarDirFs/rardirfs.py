# -*- coding: utf-8 -*-
#
# Copyright (c) 2009, Jonas Jonsson <jonas@websystem.se>
# All rights reserved.
#
# See file LICENSE for license details
#

import fuse
import time
import stat    # for file properties
import os      # for filesystem modes (O_RDONLY, etc)
import errno   # for error number codes (ENOENT, etc)
               # - note: these must be returned as negatives
import traceback
import subprocess
import re
import rarfile

fuse.fuse_python_api = (0, 2)
fuse.feature_assert('stateful_files', 'has_init')

def parsePatternFile(filename):
    '''
        Parse a file with one regular expression on each line.
        Lines starting with # will be ignored

        Return a list of compiled regular expression objects.
    '''

    if not filename:
        return []

    filename = os.path.abspath(filename)
    ret = []
    i = 0

    try:
        with open(filename, 'r') as f:
            for line in f:
                i += 1
                if len(line) == 0 or line[0] == '#':
                    continue
                try:
                    if line.endswith('\n'):
                        line = line[:-1]
                    r = re.compile(line)
                    ret.append(r)
                except re.Error, e:
                    print "Failed to compile pattern {0}:{1}, {2}".format(filename, i, e)
    except IOError, e:
        print e
    return ret

class CacheManager(object):
    '''
        Manage a cache of files compressed in rar archives.
    '''

    unrar_cmd = ['unrar', 'e', '-inul', '-y']

    def __init__(self, path):
        '''
            Path should be an absolute path.
        '''
        object.__init__(self)
        self.procs = {}
        self.path = path

        if not os.path.isdir(path):
            os.makedirs(path)

    def get(self, entry):
        '''
            Get a file-like object of a compressed file inside an archive.

            Will spawn an unrar process if needed thus it's not true that the
            file will be complete when returned.
        '''
        cache_dir  = os.path.join(self.path, "." + entry.realpath)
        cache_file = os.path.join(cache_dir, entry.rar_info.filename)

        if not os.path.isdir(cache_dir):
            os.makedirs(cache_dir)

        if os.path.isfile(cache_file):
            if os.path.getsize(cache_file) == entry.rar_info.file_size:
                return cache_file

            if cache_file in self.procs:
                ret_code = self.procs[cache_file].poll()
                if not ret_code:
                    # Still running or finished nicely
                    return cache_file
                raise IOError(errno.EIO, 'I/O error')

        # Now, file is either broken or not present, anyway, start to unpack it
        cmd = self.unrar_cmd[:]
        cmd.append("." + entry.realpath)
        cmd.append(entry.rar_info.filename)
        cmd.append(cache_dir)
        try:
            self.procs[cache_file] = subprocess.Popen(cmd)
            # Wait until the file is created, or a bad ret code is returned
            wait_count = 0
            while wait_count < 10:
                if not os.path.isfile(cache_file):
                    ret = self.procs[cache_file].poll()
                    if ret:
                        raise Exception('Unrar failed, returned: {0}'.format(ret))
                    wait_count += 1
                    time.sleep(0.05)
                else:
                    break
        except Exception:
            traceback.print_exc()
            raise IOError(errno.EIO, '')

        # Simply assume it worked.
        return cache_file

class VfsEntry(object):
    '''
        An entry in the vfs dictonary.
    '''

    def __init__(self, realpath):
        object.__init__(self)
        self.rar = None
        self.rar_info = None
        self.realpath = realpath

    def stat(self):
        '''
            Return either RoStat, RarStat or None if it shouldn't exist any more.
        '''
        if not os.path.exists("." + self.realpath):
            return -errno.ENOENT

        if self.rar:
            return RarStat(self.realpath, self.rar_info)
        else:
            return RoStat(self.realpath)

class RoStat(fuse.Stat):
    '''
        Same as os.lstat, but ugo+w is removed
    '''

    def __init__(self, filename):
        fuse.Stat.__init__(self)

        s = os.lstat("." + filename)
        self.st_mode = s.st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        self.st_ino = s.st_ino
        self.st_dev = s.st_dev
        self.st_nlink = s.st_nlink
        self.st_uid = s.st_uid
        self.st_gid = s.st_gid
        self.st_size = s.st_size
        self.st_atime = s.st_atime
        self.st_mtime = s.st_mtime
        self.st_ctime = s.st_ctime

class RarStat(fuse.Stat):
    '''
        Stat for a file inside a rar archive.
    '''

    def __init__(self, filename, info):
        fuse.Stat.__init__(self)

        s = os.lstat("." + filename)
        mode = 0
        if info.isdir():
            mode |= stat.S_IFDIR
        else:
            mode |= stat.S_IFREG
        mode |= s.st_mode & stat.S_IRUSR
        mode |= s.st_mode & stat.S_IRGRP
        mode |= s.st_mode & stat.S_IROTH
        self.st_mode = mode
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 1
        self.st_uid = s.st_uid
        self.st_gid = s.st_gid
        self.st_size = info.file_size
        self.st_atime = time.time()
        self.st_mtime = s.st_mtime
        self.st_ctime = time.mktime(info.date_time + (-1, -1, -1))


class RarDirFsFile(object):
    '''
        File object created by Fuse when a file is read
    '''

    def __init__(self, path, flags, *mode):
        object.__init__(self)
        accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if flags & accmode != os.O_RDONLY:
            raise IOError(errno.EROFS, '')

        self.file = None

        if os.path.exists("." + path):
            self.file = NormalFile(path)
        else:
            if not path in self.rarDirFs.vfs:
                raise IOError(errno.ENOENT, '')

            entry = self.rarDirFs.vfs[path]
            if entry.rar:
                if entry.rar_info.compress_type == 0x30:
                    self.file = UnCompressedRarFile(entry)
                else:
                    self.file = CompressedRarFile(entry, self.rarDirFs.cacheManager.get(entry))
            else:
                self.file = NormalFile(entry.realpath)

    def read(self, length, offset):
        return self.file.read(length, offset)

    def flush(self):
        pass

    def release(self, flags):
        self.file.close()

class NormalFile(object):
    '''
        A "Wrapper" around a normal file
    '''

    def __init__(self, path):
        object.__init__(self)
        self.file = open("." + path, "rb")
        self.close = self.file.close

    def read(self, length, offset):
        self.file.seek(offset)
        return self.file.read(length)

class UnCompressedRarFile(object):
    '''
        "Wrapper" around an uncompressed file inside a rar archive
    '''

    def __init__(self, entry):
        object.__init__(self)
        self.rar = entry.rar
        self.inf = entry.rar_info

    def read(self, length, offset):
        return self.rar.read_partial(self.inf.filename, offset, length)

    def close(self):
        pass

class CompressedRarFile(object):
    '''
        "Wrapper" around a compressed file inside a rar archive.
    '''

    def __init__(self, entry, filename):
        object.__init__(self)

        self.file = open(filename, 'rb')

        self.filename = filename
        self.close = self.file.close
        self.real_size = entry.rar_info.file_size


    def read(self, length, offset):
        if offset < self.real_size and offset >= os.path.getsize(self.filename):
            self.wait(offset, 1) # Wait a second
        self.file.seek(offset)
        return self.file.read(length)

    def wait(self, offset, timeout):
        '''
            Give unrar some time to fill in some data, up to timeout seconds.

            Raises IOError if timeout seconds has passed and size of file isn't
            larger the offset.
        '''
        delay = timeout/10.0
        while timeout > 0:
            time.sleep(delay)
            timeout -= delay

            if offset < os.path.getsize(self.filename):
                return

        # Tell application to try again
        raise IOError(errno.EAGAIN, '')


class RarDirFs(fuse.Fuse):
    '''
        Mount a directory read only with the content of rar files display instead.
    '''

    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)

        # Parameters filled in by calling function
        self.filter = None
        self.flatten = None
        self.srcdir = None
        self.only_first = None
        self.cache_path = None
        self.enable_unrar = None

        # Use a special class for file operations
        self.file_class = RarDirFsFile
        self.file_class.rarDirFs = self

        self.filterRes = []
        self.flattenRes = []
        self.rarRe = re.compile("^.*?(?:\.part(\d{2,3})\.rar|\.r(ar|\d{2})|(\d{2,3}))$", re.I)
        self.couldExistCache = dict()

        self.vfs = {} # Virtual path -> Real path
        self.rars = {} # real rarfile path -> RarFile object

    def shouldBeFlattened(self, path, e):
        '''
            Should the entry path/e be removed and it's content be displayed insted
        '''
        if os.path.isdir("." + os.path.join(path, e)):
            for r in self.flattenRes:
                if r.match(e):
                    return True
        return False

    def shouldBeFiltered(self, e):
        '''
            Should path component e be filtered?
        '''
        for r in self.filterRes:
            if r.match(e):
                return True
        if self.rarRe.match(e):
            return True
        return False

    def isFirstRarFile(self, e):
        '''
            Return True if e looks like the first rar file.
            Ends with part001.rar, .rar, or .001
        '''
        m = self.rarRe.match(e)
        if m and (m.group(1) in ('001', '01') or m.group(2) == 'ar' or m.group(3) == '001'):
            return True
        return False

    def couldExist(self, path):
        '''
            Check if path should exist. That is, does it contain a filtered or
            flattened path component.
        '''
        try:
            return self.couldExistCache[path]
        except KeyError:
            pass

        part = os.path.basename(path)
        if self.shouldBeFiltered(part):
            self.couldExistCache[path] = False
            return False

        if os.path.isdir("." + path):
            for r in self.flattenRes:
                if r.match(part):
                    self.couldExistCache[path] = False
                    return False

        self.couldExistCache[path] = True
        return True

    def readdir_flattened(self, path):
        '''
            Read directory at path, path is supposed to flattened.
            This means that every entry needs to have it's realpath saved.

            It's know that path is a directory.
        '''
        for e in os.listdir("." + path):
            if self.shouldBeFiltered(e) and not self.isFirstRarFile(e):
                continue
            if self.shouldBeFlattened(path, e):
                for sub in self.readdir_flattened(os.path.join(path, e)):
                    yield sub
            else:
                yield (path, e)

    def readdir_rar(self, vpath, filename):
        '''
            filename looks like a first rar-file, yield it's files

            Return a generator used to step through all entries
            If it looks like a rar file, but isn't, it will be filtered.
        '''
        rar = self.rars.get(filename, rarfile.RarFile("." + filename, only_first=self.only_first))

        for rar_info in rar.infolist():
            # Skip compressed files if unrar isn't enabled
            if rar_info.compress_type != 0x30 and not self.enable_unrar:
                continue
            # Flatten rar archive
            name = rar_info.filename.split('\\')[-1]
            if self.shouldBeFiltered(name):
                continue
            entry = VfsEntry(filename)
            entry.rar = rar
            entry.rar_info = rar_info
            self.vfs[os.path.join(vpath, name)] = entry
            yield name


    def getattr(self, path):
        if not self.couldExist(path):
            return -errno.ENOENT

        stat = -errno.ENOENT
        if os.path.exists("." + path):
            stat = RoStat(path)
        else:
            if not path in self.vfs:
                for x in self.readdir(os.path.dirname(path), 0):
                    pass
            if path in self.vfs:
                stat = self.vfs[path].stat()
                if stat == -errno.ENOENT:
                    del self.vfs[path]

        return stat

    def opendir(self, path):
        if not self.couldExist(path):
            return -errno.ENOENT
        return 0

    def readdir(self, path, offset):
        yield fuse.Direntry(".")
        yield fuse.Direntry("..")

        if os.path.exists("." + path):
            realpath = path
        else:
            if path in self.vfs:
                realpath = self.vfs[path].realpath
            else:
                raise OSError(errno.ENOENT, '')

        for e in os.listdir("." + realpath):
            if self.shouldBeFiltered(e) and not self.isFirstRarFile(e):
                continue
            if self.shouldBeFlattened(realpath, e):
                for (path_sub, e_sub) in self.readdir_flattened(os.path.join(realpath, e)):
                    if self.isFirstRarFile(e_sub):
                        for e_rar in self.readdir_rar(path, os.path.join(path_sub, e_sub)):
                            yield fuse.Direntry(e_rar)
                    else:
                        self.vfs[os.path.join(path, e_sub)] = VfsEntry(os.path.join(path_sub, e_sub))
                        yield fuse.Direntry(e_sub)
            else:
                if self.isFirstRarFile(e):
                    for e_rar in self.readdir_rar(path, os.path.join(realpath, e)):
                        yield fuse.Direntry(e_rar)
                else:
                    yield fuse.Direntry(e)

    def readlink(self, path):
        return os.readlink("." + path)

    def unlink(self, path):
        return -errno.EROFS

    def rmdir(self, path):
        return -errno.EROFS

    def symlink(self, path, path1):
        return -errno.EROFS

    def rename(self, path, path1):
        return -errno.EROFS

    def link(self, path, path1):
        return -errno.EROFS

    def chmod(self, path, mode):
        return -errno.EROFS

    def chown(self, path, user, group):
        return -errno.EROFS

    def mknod(self, path, mode, dev):
        return -errno.EROFS

    def mkdir(self, path, mode):
        return -errno.EROFS

    def utime(self, path, times):
        return -errno.EROFS

    def statfs(self):
        return os.statvfs(".")

    def fsinit(self):
        try:
            self.filterRes = parsePatternFile(self.filter)
            self.flattenRes = parsePatternFile(self.flatten)
            os.chdir(self.srcdir)
            if self.enable_unrar:
                self.cacheManager = CacheManager(self.cache_path)
            else:
                self.cacheManager = None
        except Exception, e:
            print traceback.format_exc()
            raise IOError(errno.EIO, '')


