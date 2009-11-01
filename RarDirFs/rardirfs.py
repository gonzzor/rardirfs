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

class Entry(object):
    '''
        An entry in the virtual filesystem.
    '''
    def __init__(self):
        self.entries = []
        self.stat = -errno.ENOSYS
        self.populated = False
        self.rar = None
        self.info = None
        self.realpath = None
        self.name = None

    def isPopulated(self):
        if self.stat != -errno.ENOSYS:
            if stat.S_ISDIR(self.stat.st_mode):
                return self.populated
            else:
                return True

    def __str__(self):
        return "Entry(%s, %s): %s" % (self.realpath, self.name, [str(x) for x in self.entries])

    def __repr__(self):
        return "Entry()"

class RoStat(fuse.Stat):
    '''
        Same as os.lstat, but ugo+w is removed
    '''

    def __init__(self, filename):
        s = os.lstat(filename)
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

    def isdir(self):
        return self.st_mode & stat.S_IFDIR == stat.S_IFDIR

class RarStat(fuse.Stat):
    '''
        Stat for a file inside a rar archive.
    '''

    def __init__(self, filename, info):
        '''
            Setup a stat object used by fuse
            filename is full path to first file in rar archive.
            info is a RarInfo object

            - st_mode (protection bits)
            - st_ino (inode number)
            - st_dev (device)
            - st_nlink (number of hard links)
            - st_uid (user ID of owner)
            - st_gid (group ID of owner)
            - st_size (size of file, in bytes)
            - st_atime (time of most recent access)
            - st_mtime (time of most recent content modification)
            - st_ctime (platform dependent; time of most recent metadata change on
                                    Unix, or the time of creation on Windows).
        '''
        fuse.Stat.__init__(self)
        s = os.lstat(filename)
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

    def isdir(self):
        return self.st_mode & stat.S_IFDIR == stat.S_IFDIR

class RarDirFs(fuse.Fuse):
    '''
        Mount a directory read only with the content of rar files display instead.
    '''

    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)

        self.file_class = self.RarDirFsFile
        self.RarDirFsFile.rarFs = self
        self.filter = None
        self.filterRes = []
        self.flatten = None
        self.flattenRes = []
        self.rarRe = re.compile("^.*?(?:\.part(\d{2,3})\.rar|\.r(ar|\d{2})|(\d{2,3}))$", re.I)
        self.srcdir = None

        self.vfs = {} # virtual path -> (rar, info, stat)

    def populate_vfs(self, path, mustPopulate = True):
        '''
            Populate self.vfs with information based on path
        '''
        if path in self.vfs and self.vfs[path].isPopulated():
            # Repopulate if needed, then return
            self.repopulate_vfs(path)
            return

        if not os.path.exists("." + path) or not os.path.isdir("." + path):
            path = os.path.dirname(path)
            mustPopulate = True
            if not os.path.isdir("." + path):
                return

        if path in self.vfs and self.vfs[path].isPopulated():
            # Repopulate if needed, then return
            self.repopulate_vfs(path)
            return

        if not path in self.vfs:
            self.vfs[path] = Entry()
            self.vfs[path].name = os.path.basename(path)
            self.vfs[path].stat = RoStat("." + path)
            self.vfs[path].realpath = path

        if not mustPopulate:
            # Don't populate if we don't need.
            return

        self.vfs[path].populated = True

        # Now path is a dir, populate
        for e in os.listdir("." + path):
            if self.shouldFlatten(path, e):
                    for e_sub in os.listdir("." + os.path.join(path, e)):
                        self.appendToVfs(path, os.path.join(path, e), e_sub)
            else:
                self.appendToVfs(path, path, e)

    def repopulate_vfs(self, path):
        '''
            Called when path already exists in vfs and we are supposed to check if
            we need to update the entries.

            At this point path is in vfs
        '''
        try:
            if self.vfs[path].rar:
                stat = os.lstat(self.vfs[path].rar.rarfile)
            else:
                stat = os.lstat("." + os.path.join(self.vfs[path].realpath, self.vfs[path].name))
            if stat.st_mtime != self.vfs[path].stat.st_mtime:
                self.update_path(path)
        except OSError:
            self.delete_path(path)

    def update_path(self, path):
        '''
            An update of path is needed.
        '''

        realpath = self.vfs[path].realpath
        name = self.vfs[path].name

        if self.vfs[path].rar:
            # This happens when the containing first rar-file is changed
            # Simply add it again to reload it.
            self.appendToVfs(path, realpath, os.path.basename(self.vfs[path].rar.rarfile))
        else:
            # Normal file, just update the stats
            self.vfs[path].stat = RoStat("." + os.path.join(realpath, name))

            if self.vfs[path].stat.isdir() and self.vfs[path].isPopulated():
                # We have alread populated this entry/dir, repopulate it
                # Or more correctly just add new entries that doesn't exist
                realdir = os.path.join(realpath, name)

                for e in os.listdir("." + realdir):
                    if self.shouldFlatten(realdir, e):
                            for e_sub in os.listdir("." + os.path.join(realdir, e)):
                                if not os.path.join(path, e_sub) in self.vfs:
                                    self.appendToVfs(path, os.path.join(realdir, e), e_sub)
                    else:
                        if not os.path.join(path, e) in self.vfs:
                            self.appendToVfs(path, realdir, e)

    def delete_path(self, path):
        '''
            Path doesn't exists anymore, delete it as well as all entries below.
        '''

        for e in self.vfs[path].entries:
            del self.vfs[os.path.join(path, e.name)]

        del self.vfs[path]


    def shouldFlatten(self, path, e):
        '''
            Should the entry path/e be removed and it's content be displayed insted
        '''
        if os.path.isdir("." + os.path.join(path, e)) and self.flattenRes:
            for r in self.flattenRes:
                if r.match(e):
                    return True
        return False

    def shouldBeHidden(self, e):
        '''
            Called by appendToVfs to check if the name e should be added.
        '''
        if self.filterRes:
            for r in self.filterRes:
                if r.match(e):
                    return True
        return False

    def appendToVfs(self, vpath, realpath, e):
        '''
            Append entry e in realpath to vpath.

            If this is a rar-file, add first file in rar-file instead.
        '''
        if self.shouldBeHidden(e):
            return

        filename = os.path.join(realpath, e)

        m = self.rarRe.match(e)
        if m:
            if m.group(1) in ('001', '01') or m.group(2) == 'ar' or m.group(3) in ('001', '01'):
                rar = rarfile.RarFile("." + filename, only_first=True)

                for info in rar.infolist()[:1]:
                    entry = Entry()
                    entry.name = info.filename.split('\\')[-1]
                    entry.info = info
                    entry.rar = rar
                    entry.realpath = realpath
                    entry.stat = RarStat("." + filename, info)
                    self.vfs[vpath].entries.append(entry)
                    self.vfs[os.path.join(vpath, entry.name)] = entry
        else:
            entry = Entry()
            entry.name = e
            entry.realpath = realpath
            entry.stat = RoStat("." + filename)
            self.vfs[vpath].entries.append(entry)
            self.vfs[os.path.join(vpath, entry.name)] = entry

    def getattr(self, path):
        self.populate_vfs(path, mustPopulate = False)
        if path in self.vfs:
            return self.vfs[path].stat
        return -errno.ENOENT

    def readdir(self, path, offset):
        self.populate_vfs(path)
        if path in self.vfs:
            yield fuse.Direntry('.')
            yield fuse.Direntry('..')
            for e in self.vfs[path].entries:
                yield fuse.Direntry(e.name)

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
        self.filterRes = parsePatternFile(self.filter)
        self.flattenRes = parsePatternFile(self.flatten)
        os.chdir(self.srcdir)

    class RarDirFsFile(object):
        '''
            File object created by Fuse when a file is read
        '''

        def __init__(self, path, flags, *mode):
            accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
            if flags & accmode != os.O_RDONLY:
                raise IOError(errno.EROFS, '')

            if path in self.rarFs.vfs:
                entry = self.rarFs.vfs[path]
                if entry.rar:
                    self.file = RarDirFs.RarFile(entry.rar, entry.info.filename)
                else:
                    self.file = RarDirFs.NormalFile(os.path.join(entry.realpath, entry.name))

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
            self.file = open("." + path, 'r')

        def read(self, length, offset):
            self.file.seek(offset)
            return self.file.read(length)

        def close(self):
            self.file.close()

    class RarFile(object):
        '''
            "Wrapper" around the RarFile in entry
        '''

        def __init__(self, rar, filename):
            self.rar = rar
            self.filename = filename

        def read(self, length, offset):
            return self.rar.read_partial(self.filename, offset, length)

        def close(self):
            pass
