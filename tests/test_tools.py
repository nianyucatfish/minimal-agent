"""Extension contract, parameter types and trusted-module loading."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from errors import ToolError
from tools import ToolRegistry, Tools
from runtime import Runtime
from session import SessionStore
from tests.helpers import FakeClient, call, answer


class ToolExtensionTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry('.')

    def module(self):
        schema = dict(name='multiply', description='Multiply integers', parameters=dict(
            type='object', properties=dict(left=dict(type='integer'), right=dict(type='integer')),
            required=['left', 'right'], additionalProperties=False))
        def execute(context, left, right):
            return left * right
        return SimpleNamespace(SCHEMA=schema, execute=execute)

    def test_new_module_runs_without_runtime_changes(self):
        self.registry.load(self.module())
        client = FakeClient(call('multiply', dict(left=6, right=7)), answer('42'))
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(client, SessionStore(root), self.registry)
            self.assertEqual(runtime.run('a', 'multiply'), '42')
        result = next(m for m in client.requests[1] if m['role']=='tool')
        self.assertEqual(json.loads(result['content'])['data'], 42)

    def test_nested_parameters(self):
        self.registry.register('nested', 'typed parameters', {
            'enabled': {'type': 'boolean'},
            'weights': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 1, 'maxItems': 3},
            'options': {'type': 'object', 'properties': {'count': {'type': 'integer', 'minimum': 1}},
                        'required': ['count'], 'additionalProperties': False}},
            ['enabled', 'weights', 'options'], lambda context, **args: args)
        valid = dict(enabled=True, weights=[1, 0.5], options=dict(count=2))
        self.assertTrue(self.registry.dispatch('nested', json.dumps(valid), {}).to_dict()['ok'])
        cases = [dict(enabled=1), dict(weights=[]), dict(weights=[1,2,3,4]),
                 dict(weights=['1']), dict(options=dict(count=True)),
                 dict(options=dict(count=0)), dict(options=dict(count=1, extra=2))]
        for override in cases:
            with self.subTest(override=override):
                value = dict(valid, **override)
                outcome = self.registry.dispatch('nested', json.dumps(value), {})
                self.assertEqual(outcome.code, 'INVALID_ARGUMENTS')

    def test_duplicate_module_fails(self):
        self.registry.load(self.module())
        with self.assertRaisesRegex(ValueError, '重复'):
            self.registry.load(self.module())

    def test_signature_checked_at_startup(self):
        module = self.module()
        module.execute = lambda context, wrong: 0
        with self.assertRaisesRegex(ValueError, '签名'):
            self.registry.load(module)
        module = self.module()
        module.SCHEMA['parameters']['required'] = ['left']
        with self.assertRaisesRegex(ValueError, '签名'):
            self.registry.load(module)

    def test_schema_is_not_silently_ignored(self):
        for keyword, value in [('pattern', 'x'), ('maxLength', -1), ('minLength', 2.5)]:
            with self.subTest(keyword=keyword):
                with self.assertRaises(ValueError):
                    self.registry.register('bad', 'bad', {'x':dict(type='string', **{keyword:value})}, ['x'], lambda context,x:x)
        with self.assertRaises(ValueError):
            self.registry.register('bad', 'bad', {'x':{'type':'array'}}, ['x'], lambda context,x:x)

    def test_schema_cannot_be_mutated_from_caller(self):
        module = self.module()
        self.registry.load(module)
        module.SCHEMA['description'] = 'changed'
        exported = self.registry.schemas
        exported[0]['function']['parameters']['properties']['left']['type'] = 'string'
        self.assertEqual(self.registry.schemas[0]['function']['description'], 'Multiply integers')
        self.assertEqual(self.registry.dispatch('multiply', '{"left":6,"right":7}', {}).data, 42)

    def test_context_uses_current_state(self):
        module = self.module()
        module.execute = lambda context, left, right: context.state['label']
        self.registry.load(module)
        self.assertEqual(self.registry.dispatch('multiply', '{"left":1,"right":1}', {'label':'A'}).data, 'A')
        self.assertEqual(self.registry.dispatch('multiply', '{"left":1,"right":1}', {'label':'B'}).data, 'B')

    def test_domain_errors_return_but_programming_errors_propagate(self):
        def expected(context, left, right):
            raise ToolError('known')
        module = self.module()
        module.execute = expected
        self.registry.load(module)
        self.assertEqual(self.registry.dispatch('multiply', '{"left":1,"right":1}', {}).code, 'TOOL_ERROR')
        self.registry.register('broken', 'broken', {}, [], lambda context: (_ for _ in ()).throw(TypeError('bug')))
        with self.assertRaisesRegex(TypeError, 'bug'):
            self.registry.dispatch('broken', '{}', {})

    def test_defaults_load_and_docs_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'sample.txt').write_text('hello')
            tools = Tools(root)
            self.assertEqual(len(tools.schemas), 10)
            self.assertEqual(tools.dispatch('file_read', '{"path":"sample.txt"}', {}).data['text'], 'hello')
            self.assertFalse(tools.dispatch('file_read', '{"path":"../outside"}', {}).to_dict()['ok'])

    def test_numbers_do_not_accept_bool_or_nonfinite(self):
        self.registry.register('number', 'number', {'x':{'type':'number'}}, ['x'], lambda context,x:x)
        for raw in ['{"x":true}', '{"x":NaN}', '{"x":Infinity}']:
            self.assertFalse(self.registry.dispatch('number', raw, {}).to_dict()['ok'])

    def test_all_registration_paths_receive_same_context_contract(self):
        from tools import ToolContext
        for entry in ('register', 'register_tool', 'load'):
            with self.subTest(entry=entry):
                registry = ToolRegistry('.')
                module = self.module()
                seen = []
                def execute(context, left, right):
                    seen.append(context)
                    context.state['result'] = left * right
                    return context.state['result']
                if entry == 'register':
                    spec = module.SCHEMA['parameters']
                    registry.register('multiply', 'Multiply', spec['properties'], spec['required'], execute)
                elif entry == 'register_tool':
                    registry.register_tool(module.SCHEMA, execute)
                else:
                    module.execute = execute
                    registry.load(module)
                state = {}
                self.assertEqual(registry.dispatch('multiply', '{"left":6,"right":7}', state).data, 42)
                self.assertIsInstance(seen[0], ToolContext)
                self.assertIs(seen[0].state, state)
                self.assertEqual(seen[0].workdir, Path('.').resolve())
                self.assertEqual(state['result'], 42)
