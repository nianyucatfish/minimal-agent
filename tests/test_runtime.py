import json
from pathlib import Path
import tempfile
import unittest
import threading
from unittest.mock import patch
from runtime import Runtime
from session import SessionStore
from tools import Tools
from errors import LLMError, StorageError, RunCancelled
from context import compact
from tests.helpers import FakeClient, answer, call


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'docs').mkdir()
        self.tools = Tools(self.root / 'docs', modules=('calculator','file_read','todo','plan','checkpoint'))
        self.store = SessionStore(self.root / 'data')

    def runtime(self, client, **kw):
        return Runtime(client, self.store, self.tools, **kw)

    def test_plain_followup(self):
        client = FakeClient(answer('你好小鱼'), answer('小鱼'))
        runtime = self.runtime(client)
        runtime.run('a', '我叫小鱼')
        runtime.run('a', '我叫什么')
        self.assertTrue(any(m['content'] == '我叫小鱼' for m in client.requests[1]))

    def test_strategy_reminder_is_sent_once_and_not_saved(self):
        calls = [call('calculator', {'expression': '1+1'}, id=f'c{i}') for i in range(21)]
        client = FakeClient(*calls, answer())
        self.runtime(client).run('a', '计算')
        reminder_requests = [
            index for index, messages in enumerate(client.requests, start=1)
            if any(message.get('content', '').startswith('【执行提醒】') for message in messages)
        ]
        self.assertEqual(reminder_requests, [21])
        self.assertTrue(client.requests[20][-1]['content'].startswith('【工作记忆】'))
        state = self.store.load(self.store.key('a'))
        self.assertNotIn('【执行提醒】', json.dumps(state['turns'], ensure_ascii=False))

    def test_tool_results_and_followup(self):
        client = FakeClient(call('plan', dict(action='create', title='交报告', steps=['写报告'])), answer(),
                            call('plan', dict(action='get')), answer())
        runtime = self.runtime(client)
        runtime.run('a', '创建交报告计划')
        runtime.run('a', '查询当前计划')
        result = json.loads(next(m['content'] for m in reversed(client.requests[-1]) if m['role'] == 'tool'))
        self.assertEqual(result['data']['plan']['title'], '交报告')
        self.assertEqual(next(m['tool_call_id'] for m in reversed(client.requests[1]) if m['role'] == 'tool'), 'c1')

    def test_isolation_and_reload(self):
        self.runtime(FakeClient(call('plan', dict(action='create', title='A', steps=['执行'])), answer())).run('a', '加A')
        client = FakeClient(call('plan', dict(action='get')), answer())
        self.runtime(client).run('b', '查询')
        self.assertEqual(json.loads(next(m['content'] for m in reversed(client.requests[-1]) if m['role'] == 'tool'))['data'], {'plan': None})
        self.assertEqual(self.store.load(self.store.key('a'))['current_plan']['title'], 'A')

    def test_invalid_tool_and_args(self):
        state = dict(current_plan=None)
        for name, raw in [('unknown', '{}'), ('plan', '{'), ('plan', '[]'),
                          ('plan', '{"action":"oops"}'), ('plan', '{"action":"add"}')]:
            self.assertFalse(self.tools.dispatch(name, raw, state).to_dict()['ok'])

    def test_calculator_and_no_eval(self):
        self.assertEqual(self.tools.dispatch('calculator', '{"expression":"(2+3)*4"}', {}).data, 20)
        for expression in ["__import__('os')", '2**100000', '1/0', '(-1)**0.5']:
            self.assertFalse(self.tools.dispatch('calculator', json.dumps(dict(expression=expression)), {}).to_dict()['ok'])

    def test_docs_path_boundary(self):
        (self.root / 'docs' / 'a.txt').write_text('test')
        self.assertEqual(self.tools.dispatch('file_read', '{"path":"a.txt"}', {}).data['text'], 'test')
        self.assertFalse(self.tools.dispatch('file_read', '{"path":"../secret"}', {}).to_dict()['ok'])

    def test_tool_failure_can_recover(self):
        client = FakeClient(call('calculator', dict(expression='1/0')),
                            call('calculator', dict(expression='1+2')), answer('3'))
        self.assertEqual(self.runtime(client).run('a', '算一下'), '3')
        self.assertFalse(json.loads(next(m['content'] for m in reversed(client.requests[1]) if m['role'] == 'tool'))['ok'])

    def test_limit_and_trace(self):
        client = FakeClient(call('plan', dict(action='create', title='保留', steps=['执行'])))
        with self.assertRaisesRegex(LLMError, '最大'):
            self.runtime(client, max_steps=1).run('a', '添加')
        key = self.store.key('a')
        self.assertEqual(self.store.load(key)['current_plan']['title'], '保留')
        events = [json.loads(row)['event'] for row in (self.store.root / (key+'.jsonl')).read_text().splitlines()]
        self.assertEqual(events, ['run_start', 'tool', 'run_error'])

    def test_api_failure_preserves_side_effects(self):
        client = FakeClient(call('plan', dict(action='create', title='保留', steps=['执行'])), LLMError('网络错误'))
        with self.assertRaisesRegex(LLMError, '网络错误'):
            self.runtime(client).run('a', '添加')
        self.assertEqual(self.store.load(self.store.key('a'))['current_plan']['title'], '保留')

    def test_compaction_keeps_call_result_pairs(self):
        state = dict(summary='', current_plan=None, checkpoint='关键约束', turns=[])
        for i in range(10):
            state['turns'].append([dict(role='user', content=str(i)*600),
                                  call('plan', dict(action='get')),
                                  dict(role='tool', tool_call_id='c1', content='[]'), answer()])
        compact(state, 4000)
        self.assertTrue(state['summary'])
        self.assertEqual(state['checkpoint'], '关键约束')
        for turn in state['turns']:
            self.assertEqual(turn[1]['tool_calls'][0]['id'], turn[2]['tool_call_id'])

    def test_multiple_calls(self):
        response = call('plan', dict(action='create', title='A', steps=['执行']))
        response['tool_calls'] += call('plan', dict(action='get'), 'c2')['tool_calls']
        client = FakeClient(response, answer())
        self.runtime(client).run('a', '加两个')
        self.assertEqual([m['tool_call_id'] for m in client.requests[1] if m['role']=='tool'], ['c1', 'c2'])

    def test_unknown_tool_bug_propagates(self):
        def broken(context):
            raise AttributeError('bug')
        self.tools.register('broken', 'broken', {}, [], broken)
        with self.assertLogs('agent', level='ERROR'):
            with self.assertRaises(AttributeError):
                self.runtime(FakeClient(call('broken', {}))).run('a', 'test')
        saved = self.store.load(self.store.key('a'))
        self.assertEqual(saved['last_run']['status'], 'failed')
        self.assertIn('NOT_EXECUTED', saved['turns'][0][2]['content'])

    def test_cancel_late_model_result(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingClient:
            def chat(inner, messages, tools, *, cancel=None):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test timeout')
                return answer('过期结果')
        runtime = self.runtime(BlockingClient())
        output = []
        thread = threading.Thread(target=lambda: output.append(runtime.run('a', 'hello')))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(runtime.cancel('a'))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        saved = self.store.load(self.store.key('a'))
        self.assertEqual(saved['last_run']['status'], 'cancelled')
        self.assertNotIn('过期结果', json.dumps(saved, ensure_ascii=False))
        self.assertIn('取消', output[0])

    def test_stale_run_cannot_commit(self):
        key = self.store.key('a')
        old = self.store.begin(key)
        new = self.store.begin(key)
        with self.assertRaises(RunCancelled):
            self.store.commit(key, old, dict(current_plan=None))
        self.store.finish(key, old)
        self.assertIs(self.store._active[key], new)

    def test_cancel_discards_uncommitted_mutation(self):
        runtime = self.runtime(FakeClient(call('plan', dict(action='create', title='丢弃', steps=['执行']))))
        original = self.tools.dispatch
        def cancel_after_tool(*args):
            result = original(*args)
            runtime.cancel('a')
            return result
        with patch.object(self.tools, 'dispatch', side_effect=cancel_after_tool):
            runtime.run('a', 'test')
        saved = self.store.load(self.store.key('a'))
        self.assertEqual(saved['current_plan'], None)
        self.assertEqual(saved['last_run']['status'], 'cancelled')

    def test_storage_failure_is_not_swallowed(self):
        with patch.object(self.store, 'save', side_effect=StorageError('disk')):
            with self.assertRaisesRegex(StorageError, 'disk'):
                self.runtime(FakeClient(answer())).run('a', 'test')
        self.assertEqual(self.store._active, {})

    def test_same_session_serialized_other_session_progresses(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingClient:
            def chat(inner, messages, tools, *, cancel=None):
                if any(m['role'] == 'user' and m['content'] == 'first' for m in messages):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('test timeout')
                return answer()
        runtime = self.runtime(BlockingClient())
        errors = []
        def run(text):
            try:
                runtime.run('a', text)
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run, args=('first',))
        second = threading.Thread(target=run, args=('second',))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            second.start()
            self.assertEqual(runtime.run('b', 'other'), '完成')
        finally:
            release.set()
            first.join(3)
            second.join(3)
        self.assertFalse(errors)
        turns = self.store.load(self.store.key('a'))['turns']
        self.assertEqual([t[0]['content'] for t in turns], ['first', 'second'])

    def test_trace_failure_does_not_fail_run(self):
        original = Path.open
        def fail_trace(path, *args, **kwargs):
            if path.suffix == '.jsonl':
                raise OSError('trace unavailable')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_trace), self.assertLogs('agent', level='ERROR'):
            self.assertEqual(self.runtime(FakeClient(answer())).run('a', 'test'), '完成')


if __name__ == '__main__':
    unittest.main()
