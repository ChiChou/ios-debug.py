#!/usr/bin/env python3

import argparse
import asyncio
import plistlib
import shlex
import shutil
import signal
import sys

from typing import Dict, Literal, Optional

from utils import find_port, ignore_signal


class Tool:
    udids: str | None
    over_network: bool
    server_port: int
    is_windows: bool
    idevice_args: list[str]

    child_stdio: Dict[Literal['stdin', 'stdout', 'stderr'], int] = {
        'stdin': asyncio.subprocess.PIPE,
        'stdout': asyncio.subprocess.PIPE,
        'stderr': asyncio.subprocess.PIPE,
    }

    def __init__(self, port: int, udid: Optional[str], network: bool = False):
        self.is_windows = sys.platform == 'win32'

        if self.is_windows:
            raise NotImplementedError('Windows is not supported yet')

        self.server_port = port
        self.udid = udid
        self.over_network = network

        idevice_args = []
        if udid:
            idevice_args.extend(['-u', shlex.quote(udid)])
        if network:
            idevice_args.append('-n')

        self.idevice_args = idevice_args

        dev_null = '\\\\.\\NUL' if sys.platform == 'win32' else '/dev/null'
        inetcat_flags = ' '.join(idevice_args)
        inetcat_cmd = f'inetcat {inetcat_flags} 22'
        self.ssh_args = [
            '-o', 'StrictHostKeyChecking=no',
            '-o', f'UserKnownHostsFile={dev_null}',
            '-o', 'LogLevel=ERROR',
            '-o', f'ProxyCommand={inetcat_cmd}',
        ]

    async def get_app_path(self, bundle_name_or_id: str):
        installer = await asyncio.create_subprocess_exec(
            'ideviceinstaller', *self.idevice_args, '-l', '-o', 'xml', '-o', 'list_all', **self.child_stdio
        )
        stdout, stderr = await installer.communicate()
        if b'--system' in stderr:  # nightly build
            installer = await asyncio.create_subprocess_exec(
                'ideviceinstaller', *self.idevice_args, 'list', '--xml', '--all', **self.child_stdio
            )
            stdout, stderr = await installer.communicate()

        apps = plistlib.loads(stdout)

        for app in apps:
            if app.get('CFBundleName') == bundle_name_or_id or app.get('CFBundleIdentifier') == bundle_name_or_id:
                return app['Path']

        raise ValueError(f'App not found: {bundle_name_or_id}')

    async def ssh(self, *args):
        return await asyncio.create_subprocess_exec('ssh', *self.ssh_args, *args, **self.child_stdio)

    async def iproxy(self, local, remote):
        return await asyncio.create_subprocess_exec('iproxy', *self.idevice_args, str(local), str(remote), **self.child_stdio)


async def main():
    parser = argparse.ArgumentParser('ios-debug')

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--app', '--bundle', metavar='BUNDLE',
                       help='Target param is a bundle ID instead of process name or pid')
    group.add_argument('-f', '--spawn', metavar='PATH',
                       help='Spawn a new process at path (not recommended)')
    group.add_argument('target', help='target name or pid', nargs='?')

    idevice_options = parser.add_argument_group()
    idevice_options.add_argument(
        '--udid', '-u', metavar='UDID', help='target specific device by UDID, default is first device connected via USB')
    idevice_options.add_argument(
        '--network', '-n', action='store_true', help='Connect to network device')

    parser.add_argument('--server', '-s', metavar='PATH', default='/var/root/debugserver',
                        help='Path to debugserver on jailbroken device')
    parser.add_argument('--port', '-p', metavar='PORT', type=int,
                        default=54321, help='Remote port for debugserver')
    args = parser.parse_args()

    for cmd in ['ssh', 'iproxy', 'lldb', 'ideviceinstaller']:
        if not shutil.which(cmd):
            raise FileNotFoundError(f'{cmd} not found')

    tool = Tool(args.port, args.udid, args.network)
    listen = '127.1:%d' % args.port
    debugserver = shlex.quote(args.server)

    if args.app:
        app_path = await tool.get_app_path(args.app)
        cmd = f'{debugserver} -x backboard {listen} {shlex.quote(app_path)}'
    elif args.spawn:
        cmd = f'{debugserver} {listen} {shlex.quote(args.spawn)}'
    else:
        cmd = f'{debugserver} {listen} -a {shlex.quote(args.target)}'

    # kill any existing debugserver
    kill = await tool.ssh('root@', 'killall -9 debugserver')
    await kill.wait()

    # run debugserver over ssh
    debugserver = await tool.ssh('-tt', 'root@', cmd)

    if not debugserver.stdout:
        raise RuntimeError('Failed to start debugserver')

    try:
        await debugserver.stdout.readuntil(b'Listening to port')
    except asyncio.IncompleteReadError as err:
        sys.stderr.write('Failed to start debugserver, unexpected output:\n')
        sys.stderr.write(err.partial.decode())
        debugserver.terminate()
        await debugserver.wait()
        sys.exit(1)

    local_port = await find_port()
    lldb_script = [
        '--one-line', f'process connect connect://127.1:{local_port}',
        '--one-line', 'bt',
        '--one-line', 'reg read'
    ]

    iproxy = await tool.iproxy(local_port, args.port)

    with ignore_signal(signal.SIGINT):
        lldb = await asyncio.create_subprocess_exec('lldb', *lldb_script)

        await asyncio.gather(
            lldb.wait(),
            debugserver.wait(),
        )

        iproxy.send_signal(signal.SIGINT)
        await iproxy.wait()


if __name__ == '__main__':
    asyncio.run(main())
