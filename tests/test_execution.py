"""Real subprocess tests using temporary session workspaces."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from tools import Tools


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = Tools(self.root / 'work', modules=('code_run',))
        self.state = {}

    def run_code(self, script, **args):
        return self.tools.dispatch('code_run', json.dumps(dict(script=script, **args)), self.state)

    def test_python_output_and_exit_code(self):
        result = self.run_code("print((128+32)*3)").data
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['stdout'], '480\n')
        self.assertEqual(result['exit_code'], 0)

    def test_nonzero_and_stderr(self):
        result = self.run_code("import sys; print('failed',file=sys.stderr); sys.exit(7)").data
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exit_code'], 7)
        self.assertIn('failed', result['stdout'])

    def test_shell_and_file_persistence(self):
        result = self.run_code("printf hello > result.txt; cat result.txt", type='shell').data
        self.assertEqual(result['stdout'], 'hello')
        result = self.run_code("from pathlib import Path; print(Path('result.txt').read_text())").data
        self.assertEqual(result['stdout'], 'hello\n')

    def test_default_workdir_is_shared(self):
        one = self.run_code("from pathlib import Path; Path('a').write_text('A')").data
        two = self.tools.dispatch('code_run', json.dumps(dict(script="from pathlib import Path; print(Path('a').exists())")), {}).data
        self.assertEqual(one['cwd'], two['cwd'])
        self.assertEqual(two['stdout'], 'True\n')

    def test_subdirectory_and_explicit_external_cwd(self):
        result = self.run_code("print('ok')", cwd='nested').data
        self.assertTrue(Path(result['cwd']).is_relative_to((self.root / 'work').resolve()))
        self.assertTrue(result['cwd'].endswith('nested'))
        self.assertEqual(self.run_code("print('external')", cwd=str(self.root/'outside')).data['stdout'], 'external\n')

    def test_timeout_returns_partial_output(self):
        start = time.monotonic()
        result = self.run_code("import time; print('started',flush=True); time.sleep(20)", timeout=0.2).data
        self.assertEqual(result['status'], 'timeout')
        self.assertIn('started', result['stdout'])
        self.assertLess(time.monotonic()-start, 4)

    def test_cancellation_terminates_process(self):
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            result = self.tools.dispatch('code_run', json.dumps(dict(script="import time; time.sleep(20)")), self.state, cancel).data
        finally:
            timer.cancel()
        self.assertEqual(result['status'], 'cancelled')

    def test_bounded_utf8_output(self):
        result = self.run_code("print('中文'*10000)", max_output_chars=100).data
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['stdout']), 100)
        self.assertNotIn('\ufffd', result['stdout'])

    @unittest.skipUnless(os.name == 'posix', 'process-group cleanup targets POSIX')
    def test_child_process_is_stopped_on_timeout(self):
        result = self.run_code("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); print(p.pid,flush=True); time.sleep(20)", timeout=0.3).data
        self.assertEqual(result['status'], 'timeout')
        pid = int(result['stdout'].strip())
        import subprocess
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not state or state.startswith('Z'), state)

    def test_precancelled_does_not_spawn(self):
        cancel = threading.Event()
        cancel.set()
        result = self.tools.dispatch('code_run', '{"script":"print(1)"}', self.state, cancel).data
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(Path(result['cwd']).exists())
