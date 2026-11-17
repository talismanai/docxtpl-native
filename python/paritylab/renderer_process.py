"""Reap renderer descendants even when the container's PID 1 does not.

Linux adoption is confined to a short-lived helper process. The lab worker must
not become a subreaper: it can have unrelated subprocesses of its own.
"""
import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def run_renderer(command, *, timeout=120):
    if sys.platform != 'linux':
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    return subprocess.run(
        [sys.executable, '-m', __name__, '--timeout', str(timeout), '--', *command],
        capture_output=True, text=True, timeout=timeout + 10,
    )


def _children():
    try:
        return [int(pid) for pid in Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()]
    except FileNotFoundError:
        # Some procfs mounts omit the task children file.
        children = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[1]) == os.getpid():
                    children.append(int(entry.name))
            except FileNotFoundError:
                pass
        return children


def _cleanup(process):
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=2)
    deadline = time.monotonic() + 3
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        # The renderer has finished (or timed out). Terminate only descendants
        # adopted by this helper, including those that detached from its group.
        for pid in _children():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError('renderer descendants did not terminate')
        time.sleep(0.01)


class _Terminated(Exception):
    pass


def _terminate(signum, frame):
    raise _Terminated()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', required=True, type=float)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.timeout <= 0:
        parser.error('a command and positive timeout are required')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # Linux PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    signal.signal(signal.SIGTERM, _terminate)
    process = None
    try:
        process = subprocess.Popen(command)
        result = process.wait(timeout=args.timeout)
        return result if result >= 0 else 128 - result
    except subprocess.TimeoutExpired:
        print(f'renderer timed out after {args.timeout:g}s', file=sys.stderr)
        return 124
    except _Terminated:
        return 143
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _cleanup(process)


if __name__ == '__main__':
    sys.exit(main())
