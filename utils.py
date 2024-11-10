import asyncio
import signal


async def find_port():
    async def empty_callback(*args):
        pass

    server = await asyncio.start_server(empty_callback, 'localhost', 0)
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
