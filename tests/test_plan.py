"""Execution-plan lifecycle, persistence, stale updates and context integration."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from errors import LLMError, StorageError
from runtime import Runtime
from session import SessionStore
from tools import Tools
from tests.helpers import FakeClient, call, answer


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = Tools(self.root, modules=('calculator','file_read','todo','plan','checkpoint'))
        self.store = SessionStore(self.root / 'data')
        self.state = dict(current_plan=None)

    def invoke(self, action, **args):
        return self.tools.dispatch('plan', json.dumps(dict(action=action, **args)), self.state)

    def create(self):
        return self.invoke('create', title='修复启动', steps=['定位问题', '修复并验证']).data['plan']

    def update(self, plan, index, status):
        return self.invoke('update', plan_id=plan['id'], step_id=plan['steps'][index]['id'], status=status)

    def test_create_and_get(self):
        self.assertEqual(self.invoke('get').data, {'plan': None})
        plan = self.create()
        self.assertEqual(plan['status'], 'active')
        self.assertEqual([s['status'] for s in plan['steps']], ['pending', 'pending'])
        self.assertNotEqual(plan['steps'][0]['id'], plan['steps'][1]['id'])
        plan['title'] = 'mutated result'
        self.assertEqual(self.invoke('get').data['plan']['title'], '修复启动')

    def test_auto_completion_and_idempotent_completion(self):
        plan = self.create()
        self.assertTrue(self.update(plan, 0, 'in_progress').to_dict()['ok'])
        self.update(plan, 0, 'completed')
        self.assertEqual(self.update(plan, 1, 'completed').data['plan']['status'], 'completed')
        before = deepcopy(self.state)
        self.assertTrue(self.update(plan, 1, 'completed').to_dict()['ok'])
        self.assertEqual(self.state, before)

    def test_only_one_step_running_and_error_no_mutation(self):
        plan = self.create()
        self.update(plan, 0, 'in_progress')
        before = deepcopy(self.state)
        self.assertFalse(self.update(plan, 1, 'in_progress').to_dict()['ok'])
        self.assertEqual(self.state, before)
        self.update(plan, 0, 'pending')
        self.assertTrue(self.update(plan, 1, 'in_progress').to_dict()['ok'])

    def test_active_plan_not_overwritten(self):
        self.create()
        before = deepcopy(self.state)
        self.assertFalse(self.invoke('create', title='新任务', steps=['一步']).to_dict()['ok'])
        self.assertEqual(self.state, before)

    def test_completed_plan_replaced_without_archive(self):
        old = self.create()
        self.update(old, 0, 'completed')
        self.update(old, 1, 'completed')
        new = self.invoke('create', title='下一任务', steps=['下一步']).data['plan']
        self.assertNotEqual(old['id'], new['id'])
        self.assertNotIn('plan_history', self.state)
        self.assertNotIn(old['id'], json.dumps(self.state))
        before = deepcopy(self.state)
        self.assertFalse(self.update(old, 0, 'completed').to_dict()['ok'])
        self.assertEqual(self.state, before)

    def test_cancel_then_replace(self):
        old = self.create()
        self.assertEqual(self.invoke('cancel', plan_id=old['id']).data['plan']['status'], 'cancelled')
        self.assertTrue(self.invoke('cancel', plan_id=old['id']).to_dict()['ok'])
        self.assertFalse(self.update(old, 0, 'completed').to_dict()['ok'])
        self.assertTrue(self.invoke('create', title='新任务', steps=['执行']).to_dict()['ok'])
        self.assertFalse(self.invoke('cancel', plan_id=old['id']).to_dict()['ok'])

    def test_finished_step_cannot_reopen(self):
        plan = self.create()
        self.update(plan, 0, 'completed')
        self.assertFalse(self.update(plan, 0, 'pending').to_dict()['ok'])
        self.update(plan, 1, 'completed')
        self.assertFalse(self.invoke('cancel', plan_id=plan['id']).to_dict()['ok'])

    def test_action_fields_and_titles_validated(self):
        for action, args in [('create', {}), ('create', dict(title='x', steps=[])),
                             ('create', dict(title=' ', steps=['x'])),
                             ('create', dict(title='x', steps=[' '])),
                             ('create', dict(title='x', steps=['x']*11)),
                             ('get', dict(plan_id='x')), ('cancel', {}),
                             ('update', dict(plan_id='x', step_id='y')),
                             ('list', {}), ('add', dict(title='old'))]:
            with self.subTest(action=action, args=args):
                self.assertFalse(self.invoke(action, **args).to_dict()['ok'])
                self.assertIsNone(self.state['current_plan'])

    def test_unknown_step_rejected(self):
        plan = self.create()
        before = deepcopy(self.state)
        self.assertFalse(self.invoke('update', plan_id=plan['id'], step_id='missing', status='completed').to_dict()['ok'])
        self.assertEqual(self.state, before)

    def run_create(self):
        runtime = Runtime(FakeClient(call('plan', dict(action='create', title='任务', steps=['核查', '执行'])), answer()),
                          self.store, self.tools)
        runtime.run('a', '创建执行计划')
        return self.store.load(self.store.key('a'))['current_plan']

    def test_reconnect_recovers_in_progress_and_session_isolated(self):
        plan = self.run_create()
        runtime = Runtime(FakeClient(call('plan', dict(action='update', plan_id=plan['id'],
                          step_id=plan['steps'][0]['id'], status='in_progress')), answer()), self.store, self.tools)
        runtime.run('a', '开始第一步')
        store = SessionStore(self.root / 'data')
        client = FakeClient(answer('继续'))
        Runtime(client, store, self.tools).run('a', '续接')
        self.assertIn(plan['id'], client.requests[0][-1]['content'])
        self.assertEqual(store.load(store.key('a'))['current_plan']['steps'][0]['status'], 'in_progress')
        self.assertIsNone(store.load(store.key('b'))['current_plan'])

    def test_cancel_run_keeps_committed_plan_discards_update(self):
        plan = self.run_create()
        client = FakeClient(call('plan', dict(action='update', plan_id=plan['id'],
                          step_id=plan['steps'][0]['id'], status='completed')))
        runtime = Runtime(client, self.store, self.tools)
        original = self.tools.dispatch
        def cancel(*args):
            result = original(*args)
            runtime.cancel('a')
            return result
        with patch.object(self.tools, 'dispatch', side_effect=cancel):
            runtime.run('a', '继续')
        saved = self.store.load(self.store.key('a'))
        self.assertEqual(saved['last_run']['status'], 'cancelled')
        self.assertEqual(saved['current_plan'], plan)

    def test_api_failure_keeps_plan(self):
        client = FakeClient(call('plan', dict(action='create', title='任务', steps=['验证'])), LLMError('timeout'))
        with self.assertRaises(LLMError):
            Runtime(client, self.store, self.tools).run('a', '创建')
        self.assertEqual(self.store.load(self.store.key('a'))['current_plan']['status'], 'active')

    def test_legacy_todos_preserved_not_converted(self):
        key = self.store.key('a')
        legacy = [dict(id='old', title='用户待办', done=False)]
        self.store.save(key, dict(turns=[], summary='', todos=legacy))
        client = FakeClient(call('plan', dict(action='get')), answer())
        Runtime(client, self.store, self.tools).run('a', '查询')
        state = self.store.load(key)
        self.assertEqual(state['todos'], legacy)
        self.assertIsNone(state['current_plan'])
        self.assertNotIn('用户待办', client.requests[-1][-1]['content'])
        self.assertEqual(json.loads(next(m['content'] for m in client.requests[-1] if m['role']=='tool'))['data'], {'plan':None})

    def test_corrupt_plan_rejected(self):
        key = self.store.key('a')
        self.create()
        state = dict(turns=[], summary='', checkpoint='', current_plan=self.state['current_plan'])
        state['current_plan']['status'] = 'completed'
        self.store.save(key, state)
        with self.assertRaises(StorageError):
            self.store.load(key)

    def test_plan_survives_compaction_and_not_written_as_block(self):
        plan = self.run_create()
        key = self.store.key('a')
        state = self.store.load(key)
        state['turns'].append([dict(role='user', content='old'), dict(role='assistant', content='x'*10000)])
        self.store.save(key, state)
        client = FakeClient(answer('继续'))
        Runtime(client, self.store, self.tools, context_chars=9000).run('a', '现在做什么')
        tail = client.requests[0][-1]['content']
        self.assertEqual(tail.count('<plan>'), 1)
        self.assertIn(plan['id'], tail)
        saved = self.store.load(key)
        self.assertEqual(saved['current_plan'], plan)
        self.assertNotIn('<plan>', json.dumps(saved['turns']))
        self.assertNotIn('x'*1000, json.dumps(client.requests[0]))

    def test_failed_plan_commit_does_not_report_success(self):
        client = FakeClient(call('plan', dict(action='create', title='任务', steps=['执行'])))
        runtime = Runtime(client, self.store, self.tools)
        original = self.store.save
        writes = []
        def fail_when_plan_written(key, state):
            writes.append(state.get('current_plan'))
            if state.get('current_plan') is not None:
                raise StorageError('disk unavailable')
            return original(key, state)
        with patch.object(self.store, 'save', side_effect=fail_when_plan_written):
            with self.assertRaisesRegex(StorageError, 'disk unavailable'):
                runtime.run('a', '创建')
        saved = self.store.load(self.store.key('a'))
        self.assertIsNone(saved['current_plan'])
        self.assertIn('NOT_EXECUTED', saved['turns'][0][-1]['content'])

    def test_cancel_run_preserves_committed_in_progress(self):
        plan = self.run_create()
        message = call('plan', dict(action='update', plan_id=plan['id'],
                                  step_id=plan['steps'][0]['id'], status='in_progress'))
        client = FakeClient(message)
        runtime = Runtime(client, self.store, self.tools)
        original = client.chat
        calls = []
        def cancel_after_commit(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                return original(*args, **kwargs)
            runtime.cancel('a')
            return answer('late')
        client.chat = cancel_after_commit
        runtime.run('a', '执行')
        saved = self.store.load(self.store.key('a'))
        self.assertEqual(saved['last_run']['status'], 'cancelled')
        self.assertEqual(saved['current_plan']['status'], 'active')
        self.assertEqual(saved['current_plan']['steps'][0]['status'], 'in_progress')
