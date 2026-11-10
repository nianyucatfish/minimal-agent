"""Human answers return through tool protocol before the Agent continues."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from tools import Tools
from runtime import Runtime
from session import SessionStore
from tests.helpers import FakeClient, answer, call


class AskUserTests(unittest.TestCase):
    def test_callback_and_runtime_followup(self):
        with tempfile.TemporaryDirectory() as directory:
            received = []
            def ask(question, options, cancel):
                received.append((question, options))
                return 'Python'
            tools = Tools(directory, modules=('ask_user',), ask_user=ask)
            client = FakeClient(call('ask_user', {'question': '选语言？', 'options': ['Python', 'Java']}),
                                answer('用 Python 实现。'))
            store = SessionStore(Path(directory) / 'data')
            self.assertEqual(Runtime(client, store, tools).run('s', '写个程序'), '用 Python 实现。')
            self.assertEqual(received, [('选语言？', ['Python', 'Java'])])
            result = next(m for m in client.requests[1] if m['role'] == 'tool')
            self.assertEqual(json.loads(result['content'])['data']['answer'], 'Python')
            self.assertEqual(store.load(store.key('s'))['last_run']['status'], 'completed')

    def test_no_callback_and_bad_input(self):
        tools = Tools(modules=('ask_user',))
        self.assertFalse(tools.dispatch('ask_user', '{"question":"选择？"}', {}).to_dict()['ok'])
        self.assertFalse(tools.dispatch('ask_user', '{"question":" "}', {}).to_dict()['ok'])
        self.assertFalse(tools.dispatch('ask_user', '{"question":"选择？","options":[1]}', {}).to_dict()['ok'])
