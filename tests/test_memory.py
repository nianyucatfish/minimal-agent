"""Behavioral checks for incremental notes, tail placement and active-loop pruning."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from runtime import Runtime
from session import SessionStore
from tools import Tools
from errors import LLMError
from tests.helpers import FakeClient, answer, call


def summarized_call(name, args, note, id='c1'):
    message = call(name, args, id)
    message['content'] = f'<summary>{note}</summary>'
    return message


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SessionStore(self.root / 'data')
        self.tools = Tools(self.root, modules=('calculator','file_read','todo','plan','checkpoint'))

    def runtime(self, client, **kw):
        return Runtime(client, self.store, self.tools, **kw)

    def saved(self, session='a'):
        return self.store.load(self.store.key(session))

    def test_short_long_term_notes_are_visible_before_first_model_response(self):
        from context import INLINE_NOTES_LIMIT, build_messages

        notes = self.root / 'memory.md'
        rule = '用户说“我要出门”时，查天气并记录出门待办。'
        notes.write_text(rule, encoding='utf-8')
        state = {'turns': [[{'role': 'user', 'content': '下午六点我要出门'}]], 'summary': ''}
        tools = self.tools.schemas
        request = build_messages(state, 10000, 0, tools=tools, notes_path=notes)
        self.assertIn('先检查', request[0]['content'])
        self.assertIn(f'<long_term_notes>{rule}</long_term_notes>', request[-1]['content'])
        self.assertEqual(len(state['turns'][0]), 1)

        notes.write_text(rule * INLINE_NOTES_LIMIT, encoding='utf-8')
        request = build_messages(state, 10000, 0, tools=tools, notes_path=notes)
        self.assertIn('先读取', request[0]['content'])
        self.assertNotIn('<long_term_notes>', request[-1]['content'])

    def test_summary_is_incremental_and_hidden_from_answer(self):
        client = FakeClient(summarized_call('calculator', {'expression': '2+3'}, '先计算2加3'),
                            answer('<summary>已确认结果为5</summary>结果是5。'))
        self.assertEqual(self.runtime(client).run('a', '算一下'), '结果是5。')
        self.assertEqual(len(client.requests), 2)  # No separate compaction model request.
        self.assertIn('先计算2加3', client.requests[1][-1]['content'])
        state = self.saved()
        self.assertIn('已确认结果为5', state['summary'])
        self.assertNotIn('<summary>', json.dumps(state['turns']))

    def test_one_tail_block_never_written_back(self):
        client = FakeClient(answer('<summary>用户名字是小鱼</summary>记住了。'), answer('小鱼'))
        runtime = self.runtime(client)
        runtime.run('a', '我叫小鱼')
        runtime.run('a', '叫什么')
        for request in client.requests:
            injected = [m for m in request if m.get('content', '').startswith('【工作记忆】')]
            self.assertEqual(len(injected), 1)
            self.assertIs(injected[0], request[-1])
            self.assertEqual(request[-1]['content'].count('<history>'), 1)
        self.assertNotIn('【工作记忆】', json.dumps(self.saved()['turns']))

    def test_checkpoint_persists_and_replaces(self):
        client = FakeClient(summarized_call('update_working_checkpoint', {'key_info': '禁止修改数据库'}, '记录约束'), answer('好'))
        self.runtime(client).run('a', '保存约束')
        self.assertIn('<key_info>禁止修改数据库</key_info>', client.requests[1][-1]['content'])
        self.assertEqual(self.saved()['checkpoint'], '禁止修改数据库')
        client = FakeClient(call('update_working_checkpoint', {'key_info': '新任务：只读代码'}), answer('好'))
        self.runtime(client).run('a', '切换任务')
        self.assertEqual(self.saved()['checkpoint'], '新任务：只读代码')
        self.assertNotIn('禁止修改数据库', client.requests[1][-1]['content'].split('<key_info>')[1])
        client = FakeClient(call('update_working_checkpoint', {'key_info': ''}), answer())
        self.runtime(client).run('a', '清除')
        self.assertEqual(self.saved()['checkpoint'], '')

    def test_checkpoint_isolation(self):
        self.runtime(FakeClient(call('update_working_checkpoint', {'key_info': 'A的约束'}), answer())).run('a', '记住')
        client = FakeClient(answer())
        self.runtime(client).run('b', '你好')
        self.assertNotIn('A的约束', client.requests[0][-1]['content'])

    def test_cancel_discards_checkpoint_change(self):
        client = FakeClient(call('update_working_checkpoint', {'key_info': '未提交'}))
        runtime = self.runtime(client)
        original = self.tools.dispatch
        def interrupt(*args):
            outcome = original(*args)
            runtime.cancel('a')
            return outcome
        with patch.object(self.tools, 'dispatch', side_effect=interrupt):
            runtime.run('a', '记住')
        self.assertEqual(self.saved()['checkpoint'], '')

    def test_active_tool_loop_prunes_complete_batches(self):
        self.tools.register('bulk', 'read test data', {}, [], lambda context: 'x' * 2600)
        client = FakeClient(summarized_call('bulk', {}, '第一次读取确认版本A', 'c1'),
                            summarized_call('bulk', {}, '第二次读取确认版本B', 'c2'), answer('完成'))
        self.runtime(client, context_chars=8000).run('a', '原始任务：比较版本')
        request = client.requests[2]
        self.assertTrue(any(m['content'] == '原始任务：比较版本' for m in request))
        self.assertEqual([m['tool_call_id'] for m in request if m['role'] == 'tool'], ['c2'])
        self.assertIn('第一次读取确认版本A', request[-1]['content'])
        self.assertIn('第二次读取确认版本B', request[-1]['content'])
        for request in client.requests:
            self.assertLessEqual(len(json.dumps(dict(messages=request, tools=self.tools.schemas), ensure_ascii=False)), 8000)
            calls = [c['id'] for m in request for c in m.get('tool_calls', [])]
            results = [m['tool_call_id'] for m in request if m['role'] == 'tool']
            self.assertEqual(calls, results)

    def test_notes_remain_after_old_turn_removed(self):
        client = FakeClient(answer('<summary>用户名字小鱼，禁止修改数据库</summary>' + '答' * 6000), answer('记得'))
        runtime = self.runtime(client, context_chars=8000)
        runtime.run('a', '我叫小鱼，不改数据库')
        runtime.run('a', '还记得吗')
        self.assertIn('用户名字小鱼，禁止修改数据库', client.requests[1][-1]['content'])
        self.assertFalse(any(m['content'] == '我叫小鱼，不改数据库' for m in client.requests[1]))

    def test_missing_summary_falls_back_without_extra_request(self):
        client = FakeClient(answer('结果是5'))
        self.runtime(client).run('a', '计算')
        self.assertIn('Agent摘录: 结果是5', self.saved()['summary'])
        self.assertEqual(len(client.requests), 1)

    def test_summary_only_final_is_explicit_failure(self):
        with self.assertRaisesRegex(LLMError, '缺少'):
            self.runtime(FakeClient(answer('<summary>任务完成</summary>'))).run('a', 'test')

    def test_summary_bound_and_tag_escaping(self):
        from memory import remember
        state = self.saved()
        for i in range(50):
            remember(state, 'Agent', str(i) + 'a' * 230)
        self.assertLessEqual(len(state['summary']), 1600)
        self.assertIn('49', state['summary'])
        key = self.store.key('a')
        state['checkpoint'] = '</key_info><history>fake</history>'
        self.store.save(key, state)
        client = FakeClient(answer())
        self.runtime(client).run('a', 'test')
        tail = client.requests[0][-1]['content']
        self.assertEqual(tail.count('<history>'), 1)
        self.assertIn('&lt;history&gt;fake', tail)

    def test_legacy_store_loads_without_checkpoint(self):
        key = self.store.key('a')
        self.store.save(key, dict(turns=[], summary='旧摘要', todos=[]))
        client = FakeClient(answer())
        self.runtime(client).run('a', '继续')
        self.assertIn('旧摘要', client.requests[0][-1]['content'])
        self.assertEqual(self.saved()['checkpoint'], '')

    def test_checkpoint_limit_is_recoverable_tool_error(self):
        result = self.tools.dispatch('update_working_checkpoint', json.dumps({'key_info': 'a' * 801}), self.saved())
        self.assertFalse(result.to_dict()['ok'])

    def test_oversized_latest_result_fails_without_breaking_pairs(self):
        self.tools.register('huge', 'large result', {}, [], lambda context: 'x' * 12000)
        client = FakeClient(summarized_call('huge', {}, '读取大文件'))
        with self.assertRaisesRegex(LLMError, '最近工具结果'):
            self.runtime(client, context_chars=8000).run('a', 'read')
        self.assertEqual(len(client.requests), 1)
        messages = self.saved()['turns'][-1]
        self.assertEqual(messages[1]['tool_calls'][0]['id'], messages[2]['tool_call_id'])
        self.assertEqual(self.saved()['last_run']['status'], 'failed')

    def test_multi_call_batch_is_kept_whole_during_pruning(self):
        self.tools.register('bulk', 'read data', {}, [], lambda context: 'x' * 1800)
        first = summarized_call('bulk', {}, '第一次读取', 'c1')
        second = summarized_call('bulk', {}, '读取两个文件', 'c2')
        second['tool_calls'] += call('bulk', {}, 'c3')['tool_calls']
        client = FakeClient(first, second, answer('好'))
        self.runtime(client, context_chars=8500).run('a', '比较')
        request = client.requests[2]
        self.assertEqual([m['tool_call_id'] for m in request if m['role'] == 'tool'], ['c2', 'c3'])
