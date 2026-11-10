"""User to-dos remain independent from execution plans and share session durability."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from errors import LLMError
from runtime import Runtime
from session import SessionStore
from tools import Tools
from tests.helpers import FakeClient, call, answer


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SessionStore(self.root / 'data')
        self.tools = Tools(self.root, modules=('calculator','file_read','todo','plan','checkpoint'))
        self.state = dict(current_plan=None, todos=[])

    def invoke(self, action, **args):
        return self.tools.dispatch('todo', json.dumps(dict(action=action, **args)), self.state)

    def test_add_list_complete_delete(self):
        item = self.invoke('add', title='交报告').data
        self.assertEqual(self.invoke('list').data, [item])
        self.assertTrue(self.invoke('complete', id=item['id']).data['done'])
        self.assertTrue(self.invoke('complete', id=item['id']).data['done'])
        self.invoke('delete', id=item['id'])
        self.assertEqual(self.invoke('list').data, [])

    def test_returned_data_is_independent(self):
        item = self.invoke('add', title='原始').data
        item['title'] = '变更'
        listed = self.invoke('list').data
        self.assertEqual(listed[0]['title'], '原始')
        listed.clear()
        self.assertEqual(len(self.state['todos']), 1)

    def test_errors_preserve_state(self):
        self.invoke('add', title='保留')
        before = deepcopy(self.state)
        for action, args in [('add', {}), ('add', dict(title=' ')),
                             ('list', dict(id='x')), ('delete', dict(id='missing')),
                             ('complete', dict(id='missing')), ('create', dict(title='plan', steps=['x']))]:
            self.assertFalse(self.invoke(action, **args).to_dict()['ok'])
            self.assertEqual(self.state, before)

    def test_plan_replacement_does_not_modify_user_todos(self):
        item = self.invoke('add', title='明天交报告').data
        plan = self.tools.dispatch('plan', json.dumps(dict(action='create', title='检查', steps=['执行'])), self.state).data['plan']
        self.tools.dispatch('plan', json.dumps(dict(action='update', plan_id=plan['id'], step_id=plan['steps'][0]['id'], status='completed')), self.state)
        self.assertFalse(self.state['todos'][0]['done'])
        self.tools.dispatch('plan', json.dumps(dict(action='create', title='新计划', steps=['新步骤'])), self.state)
        self.assertEqual(self.state['todos'], [item])
        current = deepcopy(self.state['current_plan'])
        self.invoke('complete', id=item['id'])
        self.invoke('delete', id=item['id'])
        self.assertEqual(self.state['current_plan'], current)

    def test_persistence_isolation_and_plan_continuity(self):
        runtime = Runtime(FakeClient(call('todo', dict(action='add', title='用户待办')), answer()), self.store, self.tools)
        runtime.run('a', '记个待办')
        fresh = SessionStore(self.root / 'data')
        client = FakeClient(call('todo', dict(action='list')), answer())
        Runtime(client, fresh, self.tools).run('a', '续接查询')
        result = json.loads(next(m['content'] for m in reversed(client.requests[-1]) if m['role']=='tool'))
        self.assertEqual(result['data'][0]['title'], '用户待办')
        self.assertEqual(fresh.load(fresh.key('b'))['todos'], [])
        self.assertIsNone(fresh.load(fresh.key('a'))['current_plan'])

    def test_old_user_todos_are_available_again(self):
        key = self.store.key('a')
        old = [dict(id='old', title='以前的待办', done=False)]
        self.store.save(key, dict(turns=[], summary='', todos=old))
        state = self.store.load(key)
        self.assertEqual(self.tools.dispatch('todo', '{"action":"list"}', state).data, old)
        self.assertIsNone(state['current_plan'])

    def test_plan_only_session_gets_empty_todos(self):
        key = self.store.key('a')
        self.store.save(key, dict(turns=[], summary='', current_plan=None))
        self.assertEqual(self.store.load(key)['todos'], [])

    def test_cancel_discards_uncommitted_user_todo(self):
        runtime = Runtime(FakeClient(call('todo', dict(action='add', title='丢弃'))), self.store, self.tools)
        original = self.tools.dispatch
        def cancel(*args):
            result = original(*args)
            runtime.cancel('a')
            return result
        with patch.object(self.tools, 'dispatch', side_effect=cancel):
            runtime.run('a', '新增')
        self.assertEqual(self.store.load(self.store.key('a'))['todos'], [])

    def test_api_failure_keeps_committed_user_todo(self):
        runtime = Runtime(FakeClient(call('todo', dict(action='add', title='保留')), LLMError('network')), self.store, self.tools)
        with self.assertRaises(LLMError):
            runtime.run('a', '新增')
        self.assertEqual(self.store.load(self.store.key('a'))['todos'][0]['title'], '保留')

    def test_tail_contains_plan_but_not_all_user_todos(self):
        key = self.store.key('a')
        self.store.save(key, dict(turns=[], summary='', current_plan=None,
                                 todos=[dict(id='x', title='仅工具读取的待办', done=False)]))
        client = FakeClient(answer())
        Runtime(client, self.store, self.tools).run('a', '你好')
        self.assertIn('<plan>null</plan>', client.requests[0][-1]['content'])
        self.assertNotIn('仅工具读取的待办', client.requests[0][-1]['content'])
