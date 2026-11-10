"""Context pruning must not delete the user's persisted conversation."""
from copy import deepcopy
import unittest
from context import build_messages


class TranscriptTests(unittest.TestCase):
    def test_old_turns_are_pruned_only_from_request(self):
        old = [{'role': 'user', 'content': 'old question ' * 600},
               {'role': 'assistant', 'content': 'old reply ' * 600}]
        current = [{'role': 'user', 'content': 'follow up'}]
        state = dict(turns=[old, current], summary='facts', checkpoint='', current_plan=None)
        before = deepcopy(state)
        request = build_messages(state, 4000, len(state['turns']) - 1, 'system', [])
        self.assertEqual(state, before)
        self.assertFalse(any(m.get('content') == old[0]['content'] for m in request))
        self.assertTrue(any(m.get('content') == 'follow up' for m in request))

    def test_tool_batch_pruning_keeps_full_transcript(self):
        def batch(call_id):
            return [dict(role='assistant', content='', tool_calls=[dict(id=call_id, type='function',
                    function=dict(name='search', arguments='{}'))]),
                    dict(role='tool', tool_call_id=call_id, content='result ' * 500)]
        current = [dict(role='user', content='task')] + batch('first') + batch('last')
        # The latest result fits; the first batch forces pruning.
        current[-1]['content'] = 'short result'
        state = dict(turns=[current], summary='facts', checkpoint='', current_plan=None)
        before = deepcopy(state)
        request = build_messages(state, 3000, len(state['turns']) - 1, 'system', [])
        self.assertEqual(state, before)
        ids = [m['tool_call_id'] for m in request if m['role'] == 'tool']
        self.assertEqual(ids, ['last'])

    def test_no_pruning_between_target_and_trigger(self):
        current = [dict(role='user', content='x' * 2800)]
        state = dict(turns=[current], summary='', checkpoint='', current_plan=None)
        request = build_messages(state, 4000, len(state['turns']) - 1, 'system', [], keep_rate=0.3)
        self.assertTrue(any(m.get('content') == current[0]['content'] for m in request))

    def test_reload_retains_full_history_after_compaction(self):
        import tempfile
        from session import SessionStore
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            state = store.open_session('chat')
            old = [dict(role='user', content='old ' * 2000)]
            current = [dict(role='user', content='new')]
            state['turns'] = [old, current]
            build_messages(state, 4000, len(state['turns']) - 1, 'system', [])
            store.save(store.key('chat'), state)
            restored = SessionStore(directory).load(store.key('chat'))
            self.assertEqual(restored['turns'], [old, current])
            next_request = build_messages(restored, 4000, len(restored['turns']) - 1, 'system', [])
            self.assertFalse(any(m.get('content') == old[0]['content'] for m in next_request))

    def test_building_model_messages_never_saves_state(self):
        import tempfile
        from unittest.mock import patch
        from session import SessionStore
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            store.open_session('s')
            with store.turn(store.key('s'), 'hello', 4000) as turn:
                with patch.object(store, 'commit', side_effect=AssertionError('unexpected save')):
                    request = turn.build_model_messages()
                self.assertTrue(any(m.get('content') == 'hello' for m in request))
                turn.complete(steps=1)

    def test_compaction_protects_turn_by_index(self):
        old = [dict(role='user', content='old ' * 2000)]
        current = [dict(role='user', content='keep this input')]
        state = dict(turns=[old, current], summary='', checkpoint='', current_plan=None)
        request = build_messages(state, 4000, 1, 'system', [])
        self.assertTrue(any(m.get('content') == 'keep this input' for m in request))
        self.assertEqual(len(state['turns']), 2)
