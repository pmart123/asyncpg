# Copyright (C) 2016-present the ayncpg authors and contributors
# <see AUTHORS file>
#
# This module is part of asyncpg and is released under
# the Apache 2.0 License: http://www.apache.org/licenses/LICENSE-2.0


import asyncio
import errno
import os
import os.path
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import asyncpg


class ClusterError(Exception):
    pass

if sys.platform == 'linux':
    def ensure_dead_with_parent():
        import ctypes
        import signal

        try:
            PR_SET_PDEATHSIG = 1
            libc = ctypes.CDLL(ctypes.util.find_library('c'))
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        except Exception as e:
            print(e)
else:
    def ensure_dead_with_parent():
        pass


def find_available_port(port_range=(49152, 65535), max_tries=1000):
    low, high = port_range

    port = low
    try_no = 0

    while try_no < max_tries:
        try_no += 1
        port = random.randint(low, high)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('127.0.0.1', port))
        except socket.error as e:
            if e.errno == errno.EADDRINUSE:
                continue
        finally:
            sock.close()

        break
    else:
        port = None

    return port


class Cluster:
    def __init__(self, data_dir, *, pg_config_path=None):
        self._data_dir = data_dir
        self._pg_config_path = pg_config_path
        self._pg_config = None
        self._pg_config_data = None
        self._pg_ctl = None
        self._daemon_pid = None
        self._daemon_process = None
        self._connection_addr = None

    def get_data_dir(self):
        return self._data_dir

    def get_status(self):
        if self._pg_ctl is None:
            self._init_env()

        process = subprocess.run(
            [self._pg_ctl, 'status', '-D', self._data_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.stdout, process.stderr

        if process.returncode == 4:
            return 'not-initialized'
        elif process.returncode == 3:
            return 'stopped'
        elif process.returncode == 0:
            r = re.match(r'.*PID:\s+(\d+).*', stdout.decode())
            if not r:
                raise ClusterError(
                    'could not parse pg_ctl status output: {}'.format(
                        stdout.decode()))
            self._daemon_pid = int(r.group(1))
            return self._test_connection(timeout=0)
        else:
            raise ClusterError(
                'pg_ctl status exited with status {:d}: {}'.format(
                    process.returncode, stderr))

    async def connect(self, loop=None, **kwargs):
        conn_addr = self.get_connection_addr()
        return await asyncpg.connect(
            host=conn_addr[0], port=conn_addr[1], loop=loop, **kwargs)

    def init(self, **settings):
        """Initialize cluster."""
        if self.get_status() != 'not-initialized':
            raise ClusterError(
                'cluster in {!r} has already been initialized'.format(
                    self._data_dir))

        if settings:
            extra_args = ['-o'] + ['--{}={}'.format(k, v)
                                   for k, v in settings.items()]
        else:
            extra_args = []

        process = subprocess.run(
            [self._pg_ctl, 'init', '-D', self._data_dir] + extra_args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        stderr = process.stderr

        if process.returncode != 0:
            raise ClusterError(
                'pg_ctl init exited with status {:d}: {}'.format(
                    process.returncode, stderr.decode()))

    def start(self, wait=60, *, server_settings={}, **opts):
        """Start the cluster."""
        status = self.get_status()
        if status == 'running':
            return
        elif status == 'not-initialized':
            raise ClusterError(
                'cluster in {!r} has not been initialized'.format(
                    self._data_dir))

        port = opts.pop('port', None)
        if port == 'dynamic':
            port = find_available_port()

        extra_args = ['--{}={}'.format(k, v) for k, v in opts.items()]
        extra_args.append('--port={}'.format(port))

        if 'unix_socket_directories' not in server_settings:
            server_settings['unix_socket_directories'] = '/tmp'

        for k, v in server_settings.items():
            extra_args.extend(['-c', '{}={}'.format(k, v)])

        self._daemon_process = \
            subprocess.Popen(
                [self._postgres, '-D', self._data_dir, *extra_args],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=ensure_dead_with_parent)

        self._daemon_pid = self._daemon_process.pid

        self._test_connection(timeout=wait)

    def reload(self):
        """Reload server configuration."""
        status = self.get_status()
        if status != 'running':
            raise ClusterError('cannot reload: cluster is not running')

        process = subprocess.run(
            [self._pg_ctl, 'reload', '-D', self._data_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        stderr = process.stderr

        if process.returncode != 0:
            raise ClusterError(
                'pg_ctl stop exited with status {:d}: {}'.format(
                    process.returncode, stderr.decode()))

    def stop(self, wait=60):
        process = subprocess.run(
            [self._pg_ctl, 'stop', '-D', self._data_dir, '-t', str(wait),
             '-m', 'fast'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        stderr = process.stderr

        if process.returncode != 0:
            raise ClusterError(
                'pg_ctl stop exited with status {:d}: {}'.format(
                    process.returncode, stderr.decode()))

        if (self._daemon_process is not None and
                self._daemon_process.returncode is None):
            self._daemon_process.kill()

    def destroy(self):
        status = self.get_status()
        if status == 'stopped' or status == 'not-initialized':
            shutil.rmtree(self._data_dir)
        else:
            raise ClusterError('cannot destroy {} cluster'.format(status))

    def get_connection_addr(self):
        status = self.get_status()
        if status != 'running':
            raise ClusterError('cluster is not running')

        if self._connection_addr is None:
            self._connection_addr = self._connection_addr_from_pidfile()

        return self._connection_addr['host'], self._connection_addr['port']

    def reset_hba(self):
        """Remove all records from pg_hba.conf."""
        status = self.get_status()
        if status == 'not-initialized':
            raise ClusterError(
                'cannot modify HBA records: cluster is not initialized')

        pg_hba = os.path.join(self._data_dir, 'pg_hba.conf')

        try:
            with open(pg_hba, 'w'):
                pass
        except IOError as e:
            raise ClusterError(
                'cannot modify HBA records: {}'.format(e)) from e

    def add_hba_entry(self, *, type='host', database, user, address=None,
                      auth_method, auth_options=None):
        """Add a record to pg_hba.conf."""
        status = self.get_status()
        if status == 'not-initialized':
            raise ClusterError(
                'cannot modify HBA records: cluster is not initialized')

        if type not in {'local', 'host', 'hostssl', 'hostnossl'}:
            raise ValueError('invalid HBA record type: {!r}'.format(type))

        pg_hba = os.path.join(self._data_dir, 'pg_hba.conf')

        record = '{} {} {}'.format(type, database, user)

        if type != 'local':
            if address is None:
                raise ValueError(
                    '{!r} entry requires a valid address'.format(type))
            else:
                record += ' {}'.format(address)

        record += ' {}'.format(auth_method)

        if auth_options is not None:
            record += ' ' + ' '.join(
                '{}={}'.format(k, v) for k, v in auth_options)

        try:
            with open(pg_hba, 'a') as f:
                print(record, file=f)
        except IOError as e:
            raise ClusterError(
                'cannot modify HBA records: {}'.format(e)) from e

    def trust_local_connections(self):
        self.reset_hba()
        self.add_hba_entry(type='local', database='all',
                           user='all', auth_method='trust')
        self.add_hba_entry(type='host', address='127.0.0.1/32',
                           database='all', user='all',
                           auth_method='trust')
        status = self.get_status()
        if status == 'running':
            self.reload()

    def _init_env(self):
        self._pg_config = self._find_pg_config(self._pg_config_path)
        self._pg_config_data = self._run_pg_config(self._pg_config)
        self._pg_ctl = self._find_pg_binary('pg_ctl')
        self._postgres = self._find_pg_binary('postgres')

    def _connection_addr_from_pidfile(self):
        pidfile = os.path.join(self._data_dir, 'postmaster.pid')

        try:
            with open(pidfile, 'rt') as f:
                piddata = f.read()
        except FileNotFoundError:
            return None

        lines = piddata.splitlines()

        if len(lines) < 6:
            # A complete postgres pidfile is at least 6 lines
            return None

        pmpid = int(lines[0])
        if self._daemon_pid and pmpid != self._daemon_pid:
            # This might be an old pidfile left from previous postgres
            # daemon run.
            return None

        portnum = lines[3]
        sockdir = lines[4]
        hostaddr = lines[5]

        if sockdir:
            if sockdir[0] != '/':
                # Relative sockdir
                sockdir = os.path.normpath(
                    os.path.join(self._data_dir, sockdir))
            host_str = sockdir
        else:
            host_str = hostaddr

        if host_str == '*':
            host_str = 'localhost'
        elif host_str == '0.0.0.0':
            host_str = '127.0.0.1'
        elif host_str == '::':
            host_str = '::1'

        return {
            'host': host_str,
            'port': portnum
        }

    def _test_connection(self, timeout=60):
        self._connection_addr = None

        loop = asyncio.new_event_loop()

        try:
            for i in range(timeout):
                if self._connection_addr is None:
                    self._connection_addr = \
                        self._connection_addr_from_pidfile()
                    if self._connection_addr is None:
                        time.sleep(1)
                        continue

                try:
                    con = loop.run_until_complete(
                        asyncpg.connect(database='postgres', timeout=5,
                                        loop=loop, **self._connection_addr))
                except (OSError, asyncio.TimeoutError,
                        asyncpg.CannotConnectNowError,
                        asyncpg.PostgresConnectionError):
                    time.sleep(1)
                    continue
                except asyncpg.PostgresError:
                    # Any other error other than ServerNotReadyError or
                    # ConnectionError is interpreted to indicate the server is
                    # up.
                    break
                else:
                    loop.run_until_complete(con.close())
                    break
        finally:
            loop.close()

        return 'running'

    def _run_pg_config(self, pg_config_path):
        process = subprocess.run(
            pg_config_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.stdout, process.stderr

        if process.returncode != 0:
            raise ClusterError('pg_config exited with status {:d}: {}'.format(
                process.returncode, stderr))
        else:
            config = {}

            for line in stdout.splitlines():
                k, eq, v = line.decode('utf-8').partition('=')
                if eq:
                    config[k.strip().lower()] = v.strip()

            return config

    def _find_pg_config(self, pg_config_path):
        if pg_config_path is None:
            pg_install = os.environ.get('PGINSTALLATION')
            if pg_install:
                pg_config_path = os.path.join(pg_install, 'pg_config')
            else:
                pathenv = os.environ.get('PATH').split(os.pathsep)
                for path in pathenv:
                    pg_config_path = os.path.join(path, 'pg_config')
                    if os.path.exists(pg_config_path):
                        break
                else:
                    pg_config_path = None

        if not pg_config_path:
            raise ClusterError('could not find pg_config executable')

        if not os.path.isfile(pg_config_path):
            raise ClusterError('{!r} is not an executable')

        return pg_config_path

    def _find_pg_binary(self, binary):
        bindir = self._pg_config_data.get('bindir')
        if not bindir:
            raise ClusterError(
                'could not find {} executable: '.format(binary) +
                'pg_config output did not provide the BINDIR value')

        bpath = os.path.join(bindir, binary)

        if not os.path.isfile(bpath):
            raise ClusterError(
                'could not find {} executable: '.format(binary) +
                '{!r} does not exist or is not a file'.format(bpath))

        return bpath


class TempCluster(Cluster):
    def __init__(self, *,
                 data_dir_suffix=None, data_dir_prefix=None,
                 data_dir_parent=None, pg_config_path=None):
        self._data_dir = tempfile.mkdtemp(suffix=data_dir_suffix,
                                          prefix=data_dir_prefix,
                                          dir=data_dir_parent)
        super().__init__(self._data_dir, pg_config_path=pg_config_path)


class RunningCluster(Cluster):
    def __init__(self, host=None, port=None):
        if host is None:
            host = os.environ.get('PGHOST') or 'localhost'

        if port is None:
            port = os.environ.get('PGPORT') or 5432

        self.host = host
        self.port = port

    def get_status(self):
        return 'running'

    async def connect(self, loop=None, **kwargs):
        return await asyncpg.connect(
            host=self.host, port=self.port, loop=loop, **kwargs)

    def init(self, **settings):
        pass

    def start(self, wait=60, **settings):
        pass

    def stop(self, wait=60):
        pass

    def destroy(self):
        pass
