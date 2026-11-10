"""Session commands are local, persistent and independent of model requests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from cli import SessionCommands, chat_loop
from runtime import Runtime
from session import SessionStore
from tools import Tools
from tests.helpers import FakeClient, answer, call


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SessionStore(self.root/'data')
        self.commands = SessionCommands(self.store, 'default')

    def test_new_switch_and_list(self):
        self.commands.execute('/new work')
        self.assertEqual(self.commands.session, 'work')
        listing = self.commands.execute('/sessions').message
        self.assertIn('* work', listing)
        self.assertIn('default', listing)
        self.commands.execute('/switch default')
        self.assertEqual(self.commands.session, 'default')
        self.assertIn('default', self.commands.execute('/session').message)

    def test_new_auto_and_quoted_names(self):
        self.commands.execute('/new')
        first = self.commands.session
        self.commands.execute('/new')
        self.assertNotEqual(first, self.commands.session)
        self.commands.execute('/new "工作 汇报"')
        self.commands.execute('/switch default')
        self.commands.execute('/switch "工作 汇报"')
        self.assertEqual(self.commands.session, '工作 汇报')

    def test_duplicate_and_missing_leave_current_unchanged(self):
        for command in ['/new default', '/switch missing', '/new ""', '/switch', '/unknown', '/exit extra', '/new a b']:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    self.commands.execute(command)
                self.assertEqual(self.commands.session, 'default')
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_cross_user_isolation(self):
        legacy_key = hashlib.sha256(json.dumps(['B', 'private']).encode()).hexdigest()
        state = self.store.load(legacy_key)
        state['_meta'] = dict(user='B', session='private')
        self.store.save(legacy_key, state)
        self.assertNotIn('private', self.commands.execute('/sessions').message)
        with self.assertRaises(ValueError):
            self.commands.execute('/switch private')

    def test_reconnect_recovers_names_and_turns(self):
        self.commands.execute('/new work')
        Runtime(FakeClient(answer('hello')), self.store, Tools(self.root)).run('work','hi')
        fresh = SessionCommands(SessionStore(self.root/'data'),'work')
        self.assertIn('* work  (1轮)',fresh.execute('/sessions').message)
        self.assertEqual(fresh.store.load(fresh.store.key('work'))['turns'][0][0]['content'],'hi')

    def test_legacy_session_becomes_listable_after_switch(self):
        key = self.store.key('legacy')
        self.store.save(key,dict(turns=[],summary='',checkpoint='',todos=[]))
        self.assertNotIn('legacy',self.commands.execute('/sessions').message)
        self.commands.execute('/switch legacy')
        self.assertIn('legacy',self.commands.execute('/sessions').message)

    def test_corrupt_entry_does_not_break_listing(self):
        (self.store.root/'broken.json').write_text('broken')
        with self.assertLogs('agent',level='WARNING'):
            self.assertIn('default',self.commands.execute('/sessions').message)

    def test_commands_need_no_client(self):
        values=iter(['/help','/new work','/sessions','/switch default','/session','/exit'])
        output=[]
        def forbidden():
            self.fail('commands must not initialize the model client')
        chat_loop(self.commands,forbidden,read=lambda prompt:next(values),write=output.append)
        self.assertTrue(any('/switch' in row for row in output))
        self.assertEqual(self.commands.session,'default')
        self.assertTrue(self.commands.execute('/quit').exit)

    def test_chat_routes_to_selected_session_and_escape(self):
        values=iter(['/new work','hello','/switch default','//help','/exit'])
        client=FakeClient(answer('one'),answer('two'))
        runtime=Runtime(client,self.store,Tools(self.root))
        chat_loop(self.commands,lambda:runtime,read=lambda prompt:next(values),write=lambda text:None)
        self.assertEqual(len(client.requests),2)
        self.assertEqual(self.store.load(self.store.key('work'))['turns'][0][0]['content'],'hello')
        self.assertEqual(self.store.load(self.store.key('default'))['turns'][0][0]['content'],'/help')

    def test_switch_preserves_todo_and_plan(self):
        self.commands.execute('/new work')
        client=FakeClient(call('todo',dict(action='add',title='报告')),answer(),
                          call('plan',dict(action='create',title='检查',steps=['执行'])),answer())
        runtime=Runtime(client,self.store,Tools(self.root))
        runtime.run('work','记待办')
        runtime.run('work','创建计划')
        self.commands.execute('/switch default')
        self.assertEqual(self.store.load(self.store.key('default'))['todos'],[])
        self.commands.execute('/switch work')
        saved=self.store.load(self.store.key('work'))
        self.assertEqual(saved['todos'][0]['title'],'报告')
        self.assertEqual(saved['current_plan']['title'],'检查')

    def test_eof_and_blank_input_are_local(self):
        values=iter(['','  '])
        def read(prompt):
            try:return next(values)
            except StopIteration:raise EOFError
        chat_loop(self.commands,lambda:self.fail('no model'),read=read,write=lambda text:None)

    def test_failed_model_init_still_allows_commands(self):
        from errors import LLMError
        values=iter(['hello','/new another','/exit'])
        output=[]
        def fail():
            raise LLMError('missing config')
        chat_loop(self.commands,fail,read=lambda prompt:next(values),write=output.append)
        self.assertEqual(self.commands.session,'another')
        self.assertTrue(any('missing config' in line for line in output))
