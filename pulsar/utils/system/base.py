# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license. 
# See the NOTICE for more information.
import ctypes
import os
import socket
import sys
import textwrap
import time
import signal
from time import sleep
from select import select as _select

from pulsar.utils.importer import import_module
from pulsar.utils.py2py3 import *

from .sock import *

MAXFD = 1024
SIG_NAMES = {}
SKIP_SIGNALS = ('KILL','STOP')

def all_signals():
    for sig in dir(signal):
        if sig.startswith('SIG') and sig[3] != "_":
            val = getattr(signal,sig)
            if is_int(val):
                name = sig[3:]
                if name not in SKIP_SIGNALS:
                    SIG_NAMES[val] = name
                    yield name

            
ALL_SIGNALS = tuple(all_signals())


MAXFD = 1024

if (hasattr(os, "devnull")):
   REDIRECT_TO = os.devnull
else:
   REDIRECT_TO = "/dev/null"

timeout_default = object()

CHUNK_SIZE = (16 * 1024)

MAX_BODY = 1024 * 132

weekdayname = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
monthname = [None,
             'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# Server and Date aren't technically hop-by-hop
# headers, but they are in the purview of the
# origin server which the WSGI spec says we should
# act like. So we drop them and add our own.
#
# In the future, concatenation server header values
# might be better, but nothing else does it and
# dropping them is easier.
hop_headers = set("""
    connection keep-alive proxy-authenticate proxy-authorization
    te trailers transfer-encoding upgrade
    server date
    """.split())
             
try:
    from setproctitle import setproctitle
    def set_proctitle(title):
        setproctitle("pulsar: %s" % title) 
except ImportError:
    def set_proctitle(title):
        return


def load_worker_class(uri):
    components = uri.split('.')
    if len(components) == 1:
        try:
            if uri.startswith("#"):
                uri = uri[1:]
            mod = import_module('pulsar.workers.' + uri)
            return getattr(mod,'Worker')
        except ImportError: 
            raise RuntimeError('Worker class uri "{0}" invalid or not found'.format(uri))
    klass = components.pop(-1)
    mod = import_module('.'.join(components))
    return getattr(mod, klass)


def set_owner_process(uid,gid):
    """ set user and group of workers processes """
    if gid:
        try:
            os.setgid(gid)
        except OverflowError:
            # versions of python < 2.6.2 don't manage unsigned int for
            # groups like on osx or fedora
            os.setgid(-ctypes.c_int(-gid).value)
            
    if uid:
        os.setuid(uid)
    
        
def parse_address(netloc, default_port=8000):
    netloc = to_string(netloc)
    if netloc.startswith("unix:"):
        return netloc.split("unix:")[1]

    # get host
    if '[' in netloc and ']' in netloc:
        host = netloc.split(']')[0][1:].lower()
    elif ':' in netloc:
        host = netloc.split(':')[0].lower()
    elif netloc == "":
        host = "0.0.0.0"
    else:
        host = netloc.lower()
    
    #get port
    netloc = netloc.split(']')[-1]
    if ":" in netloc:
        port = netloc.split(':', 1)[1]
        if not port.isdigit():
            raise RuntimeError("%r is not a valid port number." % port)
        port = int(port)
    else:
        port = default_port 
    return (host, port)


class IObase(object):
    # Constants from the epoll module
    _EPOLLIN = 0x001
    _EPOLLPRI = 0x002
    _EPOLLOUT = 0x004
    _EPOLLERR = 0x008
    _EPOLLHUP = 0x010
    _EPOLLRDHUP = 0x2000
    _EPOLLONESHOT = (1 << 30)
    _EPOLLET = (1 << 31)

    # Our events map exactly to the epoll events
    NONE = 0
    READ = _EPOLLIN
    WRITE = _EPOLLOUT
    ERROR = _EPOLLERR | _EPOLLHUP | _EPOLLRDHUP
    
    
class EpollProxy(object):
    '''An epoll like class.'''
    def __init__(self):
        self.read_fds = set()
        self.write_fds = set()
        self.error_fds = set()
        self.fd_dict = (self.read_fds, self.write_fds, self.error_fds)

    def register(self, fd, eventmask = None):
        eventmask = eventmask or IObase.READ
        self.fd_dict[eventmask].add(fd)

    def register(self, fd, events):
        if events & IObase.READ:
            self.read_fds.add(fd)
        if events & IObase.WRITE:
            self.write_fds.add(fd)
        if events & IObase.ERROR:
            self.error_fds.add(fd)
            # Closed connections are reported as errors by epoll and kqueue,
            # but as zero-byte reads by select, so when errors are requested
            # we need to listen for both read and error.
            self.read_fds.add(fd)
                
    def modify(self, fd, events):
        self.unregister(fd)
        self.register(fd, events)

    def unregister(self, fd):
        self.read_fds.discard(fd)
        self.write_fds.discard(fd)
        self.error_fds.discard(fd)
    
    def poll(self, timeout=None):
        raise NotImplementedError
    
    
class IOselect(EpollProxy):
    '''An epoll like select class.'''
        
    def poll(self, timeout=None):
        readable, writeable, errors = _select(
            self.read_fds, self.write_fds, self.error_fds, timeout)
        events = {}
        for fd in readable:
            events[fd] = events.get(fd, 0) | IObase.READ
        for fd in writeable:
            events[fd] = events.get(fd, 0) | IObase.WRITE
        for fd in errors:
            events[fd] = events.get(fd, 0) | IObase.ERROR
        return list(iteritems(events))
    
