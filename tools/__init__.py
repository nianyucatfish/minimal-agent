"""Explicit built-in composition; add trusted tool modules here or call registry.load()."""
from functools import partial

from .registry import ToolRegistry, ToolContext, StepOutcome

BUILTINS = ('calculator', 'file_read', 'file_write', 'todo', 'plan', 'checkpoint', 'code_run', 'web_scan', 'web_execute_js', 'ask_user')


class Tools(ToolRegistry):
    def __init__(self, workdir="temp", browser_url="http://127.0.0.1:18766/link", modules=None, ask_user=None, *, store=None):
        super().__init__(workdir, browser_url, ask_user)
        for name in BUILTINS if modules is None else modules:
            self.load(f'{__name__}.builtin.{name}')
        if modules is None and store is not None:
            from .builtin import recall_history
            self.register_tool(recall_history.SCHEMA, partial(recall_history.execute, store=store))
