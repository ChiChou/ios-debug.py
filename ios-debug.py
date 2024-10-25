#!/usr/bin/env python3

import argparse
import asyncio
import shlex
import shutil
import signal
import sys


from typing import Dict, Literal


async def find_port():
    async def empty_callback(*args):
        pass

    server = await asyncio.start_server(empty_callback, '127.1', 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


class IgnoreSignal:
    def __init__(self, signal):
        self.signal = signal

    def __enter__(self):
        self.original = signal.getsignal(self.signal)
        signal.signal(self.signal, self.signal_handler)

    def __exit__(self, exc_type, exc_value, traceback):  # reset
        signal.signal(self.signal, self.original)

    def signal_handler(self, sig, frame):
        pass


def ignore_signal(signum):
    return IgnoreSignal(signum)


child_stdio: Dict[Literal['stdin', 'stdout', 'stderr'], int] = {
    'stdin': asyncio.subprocess.PIPE,
    'stdout': asyncio.subprocess.PIPE,
    'stderr': asyncio.subprocess.PIPE,
}


async def get_app_path(bundle_name_or_id: str):
    # todo: add --udid and --network options
    installer = await asyncio.create_subprocess_exec(
        'ideviceinstaller', '-l', '-o', 'xml', '-o', 'list_all', **child_stdio
    )
    stdout, _ = await installer.communicate()

    import plistlib
    apps = plistlib.loads(stdout)

    for app in apps:
        if app.get('CFBundleName') == bundle_name_or_id or app.get('CFBundleIdentifier') == bundle_name_or_id:
            return app['Path']

    raise ValueError(f'App not found: {bundle_name_or_id}')


async def main():
    parser = argparse.ArgumentParser('ios-debug')

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--app', '--bundle', metavar='BUNDLE',
                       help='Target param is a bundle ID instead of process name or pid')
    group.add_argument('-f', '--spawn', metavar='PATH',
                       help='Spawn a new process at path (not recommended)')
    group.add_argument('target', help='target name or pid', nargs='?')
    parser.add_argument('--server', '-s', metavar='PATH', default='/var/root/debugserver',
                        help='Path to debugserver on jailbroken device')
    parser.add_argument('--port', '-p', metavar='PORT', type=int,
                        default=54321, help='Remote port for debugserver')
    args = parser.parse_args()

    for cmd in ['ssh', 'iproxy', 'lldb']:
        if not shutil.which(cmd):
            raise FileNotFoundError(f'{cmd} not found')

    listen = '127.1:%d' % args.port
    debugserver = shlex.quote(args.server)

    if args.app:
        app_path = await get_app_path(args.app)
        cmd = f'{debugserver} -x backboard {listen} {shlex.quote(app_path)}'
    elif args.spawn:
        cmd = f'{debugserver} {listen} {shlex.quote(args.spawn)}'
    else:
        cmd = f'{debugserver} {listen} -a {shlex.quote(args.target)}'

    local_port = await find_port()
    lldb_script = [
        '--one-line', f'process connect connect://127.1:{local_port}',
        '--one-line', 'bt',
        '--one-line', 'reg read'
    ]

    # kill any existing debugserver
    kill = await asyncio.create_subprocess_exec('ssh', 'root@ios', 'killall -9 debugserver', **child_stdio)
    await kill.wait()

    # run debugserver over ssh
    debugserver = await asyncio.create_subprocess_exec('ssh', '-tt', 'root@ios', cmd, **child_stdio)

    if not debugserver.stdout:
        raise RuntimeError('Failed to start debugserver')

    try:
        await debugserver.stdout.readuntil(b'Listening to port')
    except asyncio.IncompleteReadError as err:
        print('Failed to start debugserver, unexpected output:')
        print(err.partial.decode())
        debugserver.terminate()
        await debugserver.wait()
        sys.exit(1)

    # run iproxy in the background
    iproxy = await asyncio.create_subprocess_exec('iproxy', str(local_port), str(args.port), **child_stdio)

    with ignore_signal(signal.SIGINT):
        lldb = await asyncio.create_subprocess_exec('lldb', *lldb_script)

        await asyncio.gather(
            lldb.wait(),
            debugserver.wait(),
        )

        iproxy.send_signal(signal.SIGINT)
        # iproxy.terminate()
        await iproxy.wait()


if __name__ == '__main__':
    asyncio.run(main())
