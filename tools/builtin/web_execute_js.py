"""Run a script in an explicitly selected browser tab."""
import json
from browser.client import BrowserBridge

SCHEMA = {
    'name': 'web_execute_js',
    'description': ('在指定tab_id执行页面JavaScript函数体，使用return返回结果，支持await。先web_scan观察，执行后再扫描确认。'
                    'expected_url可防止页面已变化。导航与读取分两次调用；不读取密码、Cookie或其他认证信息。'),
    'parameters': {'type': 'object', 'properties': {
        'tab_id': {'type': 'integer', 'minimum': 1},
        'script': {'type': 'string', 'minLength': 1, 'maxLength': 20000},
        'expected_url': {'type': 'string'},
        'timeout': {'type': 'number', 'minimum': 1, 'maximum': 30},
        'max_chars': {'type': 'integer', 'minimum': 500, 'maximum': 10000}},
        'required': ['tab_id', 'script'], 'additionalProperties': False},
}


def execute(context, tab_id, script, expected_url=None, timeout=15, max_chars=6000):
    bridge = BrowserBridge(context.browser_url)
    with bridge.selected(tab_id, context.cancel):
        result = bridge.evaluate(tab_id, script, timeout, expected_url)
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > max_chars:
        return dict(tab_id=tab_id, preview=text[:max_chars], truncated=True)
    return dict(tab_id=tab_id, result=result, truncated=False)
