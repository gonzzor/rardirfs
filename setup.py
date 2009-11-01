#!/usr/bin/env python
# -*- coding: utf-8 -*-

from distutils.core import setup

setup(name='RarDirFs',
      version='0.1',
      description='Mount a directory read only with direct access to files in RAR archives',
      long_description="""
Mount a directory read only where all rar archives are hidden and their
files are shown instead. Beside this it can also filter files and 
flatten out directories.""",
      author='Jonas Jonsson',
      author_email='jonas@websystem.se',
      url='http://launchpad.net/rardirfs',
      license='BSD',
      packages=['RarDirFs'],
      scripts=['rardirfs'],
      platforms=['Linux'],
      data_files=[('man/man1', ['rardirfs.1']), ('/etc/rardirfs', ['filter', 'flatten'])],
      classifiers=[
          'Development Status :: 3 - Alpha',
          'Environment :: No Input/Output (Daemon)',
          'Intended Audience :: System Administrators',
          'License :: OSI Approved :: BSD License',
          'Operating System :: POSIX :: Linux',
          'Programming Language :: Python',
          'Topic :: System :: Filesystems',
      ],
     )
