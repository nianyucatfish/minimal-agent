"""Common registration, schema validation and dispatch; independent of concrete tools."""
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from inspect import signature
import json
from pathlib import Path
import re
from errors import ToolError
from .schema import check_schema, validate


@dataclass
class StepOutcome:
    data: object = None
    error: str = ''
    code: str = ''

    def to_dict(self):
        if self.code:
            return dict(ok=False, error=dict(code=self.code, message=self.error))
        return dict(ok=True, data=self.data)


@dataclass(frozen=True)
class ToolContext:
    workdir: Path
    state: dict
    browser_url: str = "http://127.0.0.1:18766/link"
    cancel: object = None
    ask_user: object = None

    def resolve_path(self, path='.'):
        target = Path(path).expanduser()
        return (target if target.is_absolute() else self.workdir / target).resolve()


class ToolRegistry:
    def __init__(self, workdir="temp", browser_url="http://127.0.0.1:18766/link", ask_user=None):
        self.workdir = Path(workdir).resolve()
        self.registry = {}
        self.ask_user = ask_user
        self.browser_url = browser_url

    def register(self, name, description, properties, required, execute):
        """Register a function execute(context, **arguments), including test/embedded tools."""
        self.register_tool(dict(name=name, description=description, parameters=dict(
            type='object', properties=properties, required=required, additionalProperties=False)), execute)

    def register_tool(self, schema, execute):
        name, spec = schema.get('name'), schema.get('parameters')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name):
            raise ValueError('工具名不合法')
        if name in self.registry:
            raise ValueError(f'工具重复注册：{name}')
        if not isinstance(schema.get('description'), str) or not schema['description'].strip():
            raise ValueError('工具缺少描述')
        check_schema(spec)
        if spec['type'] != 'object':
            raise ValueError('工具参数必须是 object')
        self._check_signature(execute, ToolContext(self.workdir, {}), spec)
        self.registry[name] = (dict(type='function', function=deepcopy(schema)), execute)

    def load(self, module):
        """Explicitly load a trusted Python module exposing SCHEMA and execute(context, ...)."""
        module = import_module(module) if isinstance(module, str) else module
        self.register_tool(module.SCHEMA, module.execute)

    @staticmethod
    def _check_signature(execute, context, spec):
        try:
            # All declared fields must be accepted; optional fields must actually be optional.
            signature(execute).bind(context, **dict.fromkeys(spec.get('properties', {})))
            signature(execute).bind(context, **dict.fromkeys(spec.get('required', [])))
        except TypeError as exc:
            raise ValueError('工具函数签名与 Schema 不一致') from exc

    @property
    def schemas(self):
        return [deepcopy(schema) for schema, _ in self.registry.values()]

    def dispatch(self, name, raw, state, cancel=None):
        if name not in self.registry:
            return StepOutcome(error='未知工具', code='UNKNOWN_TOOL')
        schema, execute = self.registry[name]
        try:
            args = json.loads(raw)
            validate(args, schema['function']['parameters'])
        except (ValueError, TypeError, RecursionError) as exc:
            return StepOutcome(error=str(exc), code='INVALID_ARGUMENTS')
        try:
            context = ToolContext(workdir=self.workdir, state=state,
                                  browser_url=self.browser_url, cancel=cancel,
                                  ask_user=self.ask_user)
            return StepOutcome(data=execute(context, **args))
        except ToolError as exc:
            return StepOutcome(error=str(exc), code=exc.code)
        # Unexpected execution errors propagate; do not disguise bugs as parameter errors.
