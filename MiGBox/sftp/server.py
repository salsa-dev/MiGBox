# SFTP server based on paramiko
#
# Copyright (C) 2013 Benjamin Ertl
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""
SFTP server implementation based on paramiko.
Provides a SFTP server to run on the MiG server or an other central server for
synchronization.
"""

__version__ = 0.4
__author__ = 'Benjamin Ertl'

header = """
SFTP server for MiGBox - version {0}
Copyright (c) 2013 {1}

MiGBox comes with ABSOLUTELY NO WARRANTY. This is free software,
and you are welcome to redistribute it under certain conditions.

type 'license' to show license information
type 'exit' to shutdown the server
""".format(__version__, __author__)

import os
import sys
import time
import threading
import socket
import select
import json
import base64

import paramiko

from MiGBox.sync.delta import blockchecksums, delta, patch
from MiGBox.sftp.common  import CMD_BLOCKCHK, CMD_DELTA, CMD_PATCH 
from MiGBox.common import about
from MiGBox.sftp.server_interface import SFTPServerInterface

class Server(paramiko.ServerInterface):
    """
    This class inherits from L{paramiko.SFTPServer}.

    It handles the public key authentication.
    """

    def __init__(self, root, userkey):
        """
        Create a new server that handles the public key authentication.

        @param root: root path of the server.
        @type root: str
        @param userkey: path to the user's public key.
        @type userkey: str
        """

        super(Server, self).__init__()
        self.root = root
        self.userkey = userkey

    def check_channel_request(self, kind, chanid):
        """
        Determine if a channel request of a given type will be granted.

        This can be tighten for more security, here all requests are allowed.
        """

        return paramiko.OPEN_SUCCEEDED

    def check_auth_publickey(self, username, key):
        """
        Determine if a given key supplied by the client is acceptable for use
        in authentication.
        """

        with open(self.userkey, 'rb') as f:
            key_data = f.read()
        # file format "ssh-rsa AAA.... user@somemachine" 
        key_data = key_data.split(' ')[1]
        rsakey = paramiko.RSAKey(data=base64.decodestring(key_data))
        if key == rsakey:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        """
        Return a list of authentication methods supported.

        Here, only publickey authentication is allowed.
        """

        return 'publickey'

class SFTPServer(paramiko.SFTPServer):
    """
    This class inherits from L{paramiko.SFTPServer}.

    It is required here to overwrite/extend the paramiko.SFTPServer. 
    """

    def _process(self, t, request_number, msg):
        """
        Overwritten method for processing incoming requests to except
        and execute synchronization specific requests.

        This is a hook into the paramiko.SFTPServer implementation

        See L{paramiko.SFTPServer._process}
        """
        if t == CMD_BLOCKCHK:
            path = msg.get_string()
            bs = blockchecksums(self.server._get_path(path))
            j = json.dumps(bs)
            message = paramiko.Message()
            message.add_int(request_number)
            message.add_string(j)
            self._send_packet(t, str(message))
            return
        elif t == CMD_DELTA:
            path = msg.get_string()
            bs = json.loads(msg.get_string())
            d = delta(self.server._get_path(path), bs)
            j = json.dumps(d)
            message = paramiko.Message()
            message.add_int(request_number)
            message.add_string(j)
            self._send_packet(t, str(message))
        elif t == CMD_PATCH:
            path = msg.get_string()
            d = json.loads(msg.get_string())
            patched = patch(self.server._get_path(path), d)
            self._send_status(request_number, self.server.rename(patched, path))
        else:
            return paramiko.SFTPServer._process(self, t, request_number, msg)

    @classmethod
    def run_server(cls, conn, addr, hostkey, userkey, root):
        transport = paramiko.Transport(conn)
        transport.add_server_key(paramiko.RSAKey.from_private_key_file(hostkey))
        transport.set_subsystem_handler('sftp', cls, SFTPServerInterface)
        transport.start_server(threading.Event(), Server(root, userkey))

        while transport.is_active():
            time.sleep(1)

        transport.close()

def run(host, port, hostkey, userkey, rootpath, backlog=0, logfile=None, loglevel=None):
    """
    Main entry point to run the sftp server.

    @param host: server's name or ip address.
    @type host: str
    @param port: server's port number for listening.
    @type port: int
    @param hostkey: path to the server's private rsa key.
    @type hostkey: str
    @param userkey: path to the user's public rsa key.
    @type userkey: str
    @param rootpath: path to be served via SFTP.
    @type rootpath: str
    @param backlog: maximum number of queued connections.
    @type backlog: int
    @param logfile: path to the server's log file.
    @type logfile: str
    @param loglevel: log level, usually 'INFO' or 'DEBUG'
    @type loglevel: str
    """

    if logfile:
        loglevel = loglevel if loglevel else 'INFO'
        paramiko.util.log_to_file(logfile, loglevel)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
    server_socket.bind((host,int(port)))
    server_socket.listen(int(backlog))
    server_socket.setblocking(0)

    client_threads = []
    # might not work on windows, see python select and stdin
    input_select = [server_socket, sys.stdin]

    print header
    running = True
    while running:
        input_ready, output_ready, except_ready = select.select(input_select, [], [])
        for input_ in input_ready:
            if input_ == server_socket:
                conn, addr = server_socket.accept()
                thread = threading.Thread(target=SFTPServer.run_server,
                                          args=(conn, addr, hostkey, userkey, rootpath))
                client_threads.append(thread)
                thread.start()
            elif input_ == sys.stdin:
                in_ = sys.stdin.readline()
                if in_.rstrip() == 'license':
                    print about
                if in_.rstrip() == 'exit':
                    running = False

    print 'Server is going down ...'
    server_socket.close()
