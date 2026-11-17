from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from paritylab.renderer_process import run_renderer


@unittest.skipUnless(sys.platform == 'linux', 'Linux renderer adoption')
class RendererProcessTests(unittest.TestCase):
    def test_output_and_exit_status(self):
        result = run_renderer([sys.executable, '-c',
            'import sys; print("output"); print("error", file=sys.stderr); sys.exit(7)'])
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, 'output\n', 'error\n'))

    def test_orphan_is_reaped_after_success(self):
        result = run_renderer([sys.executable, '-c', '''
import os, time
ready, notify = os.pipe()
child = os.fork()
if not child:
    os.setsid()
    print(os.getpid(), flush=True)
    os.write(notify, b'1')
    time.sleep(60)
    os._exit(0)
os.read(ready, 1)
os._exit(0)
'''])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path('/proc', result.stdout.strip()).exists())

    def test_timeout_reaps_detached_descendant(self):
        result = run_renderer([sys.executable, '-c', '''
import os, time
if not os.fork():
    os.setsid()
    print(os.getpid(), flush=True)
time.sleep(60)
'''], timeout=0.5)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn('timed out', result.stderr)
        self.assertFalse(Path('/proc', result.stdout.strip()).exists())

    def test_termination_reaps_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory, 'pid')
            command = [sys.executable, '-m', 'paritylab.renderer_process', '--timeout', '30', '--',
                sys.executable, '-c', '''
import os, sys, time
from pathlib import Path
if not os.fork():
    os.setsid()
    Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
''', str(pidfile)]
            with subprocess.Popen(command) as helper:
                try:
                    deadline = time.monotonic() + 5
                    while (not pidfile.exists() or not pidfile.read_text()) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(pidfile.exists())
                    pid = pidfile.read_text()
                    helper.send_signal(signal.SIGTERM)
                    self.assertEqual(helper.wait(timeout=5), 143)
                    self.assertFalse(Path('/proc', pid).exists())
                finally:
                    if helper.poll() is None:
                        helper.terminate()
                        helper.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
