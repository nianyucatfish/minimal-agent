"""Local browser bridge client with on-demand startup for the default endpoint."""
import atexit
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from errors import RunCancelled, ToolError

DEFAULT_BRIDGE_URL = 'http://127.0.0.1:18766/link'
CORE = Path(__file__).resolve().parents[1]
NO_BROWSER_TABS = (
    '浏览器桥接没有可操作的标签页。请在 Chrome 启用项目的 browser/extension/ 扩展，'
    '并打开一个普通 HTTP(S) 页面，再重新扫描。'
)
_start_lock = threading.Lock()
_guard = threading.Lock()
_tab_locks = {}


def _stop_relay(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def ensure_browser_bridge():
    """Start the default relay if no compatible service is listening."""
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        DEFAULT_BRIDGE_URL, data=b'{"cmd":"get_all_sessions"}',
        headers={'Content-Type': 'application/json'},
    )

    def ready():
        try:
            with http.open(request, timeout=0.3) as response:
                return isinstance(json.loads(response.read(2_000_000)).get('r'), list)
        except (OSError, ValueError, AttributeError):
            return False

    with _start_lock:
        if ready():
            return False

        python = CORE / '.venv-browser' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        try:
            process = subprocess.Popen(
                [str(python) if python.is_file() else sys.executable, '-m', 'browser.service'],
                cwd=CORE,
            )
        except OSError as exc:
            raise ToolError('无法启动浏览器桥接，请检查 Python 虚拟环境') from exc
        try:
            for _ in range(30):
                if ready():
                    if process.poll() is None:
                        atexit.register(_stop_relay, process)
                        return True
                    return False
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            raise ToolError(
                '浏览器桥接启动失败。请按 README 的「浏览器搜索」安装 requirements-browser.txt 中的依赖，'
                '并检查 18765 和 18766 端口。'
            )
        except BaseException:
            _stop_relay(process)
            raise


class BrowserBridge:
    def __init__(self, url=DEFAULT_BRIDGE_URL):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password):
            raise ToolError('浏览器桥接地址必须是无认证的本机 HTTP 地址')
        self.url = url
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, payload, timeout=20):
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
        try:
            with self.http.open(request, timeout=timeout + 5) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ToolError('浏览器返回过大，请缩小查询范围')
            result = json.loads(body)['r']
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolError('浏览器桥接不可用或超时；请检查服务，操作结果可能未知，不要盲目重试写操作') from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise ToolError('浏览器桥接响应格式错误') from exc
        if isinstance(result, dict) and result.get('error'):
            raise ToolError('浏览器执行失败：' + str(result['error'])[:400])
        return result

    def tabs(self):
        command = {'cmd': 'get_all_sessions'}
        try:
            result = self._request(command)
        except ToolError as exc:
            cause = exc.__cause__
            if self.url != DEFAULT_BRIDGE_URL or not isinstance(getattr(cause, 'reason', cause), ConnectionRefusedError):
                raise
            started = ensure_browser_bridge()
            result = self._request(command)
            if started and result == []:
                # Give the extension's reconnect timer a chance to publish its tabs.
                for _ in range(12):
                    time.sleep(0.25)
                    result = self._request(command)
                    if result:
                        break
        if not isinstance(result, list):
            raise ToolError('浏览器标签页列表格式错误')
        return [dict(tab_id=int(t['id']), title=t.get('title', ''), url=t['url'])
                for t in result if isinstance(t, dict) and t.get('type') == 'ext_ws'
                and str(t.get('id', '')).isdigit() and str(t.get('url', '')).startswith(('http://', 'https://'))]

    @contextmanager
    def selected(self, tab_id, cancel=None):
        with _guard:
            lock = _tab_locks.setdefault((self.url, tab_id), threading.Lock())
        while not lock.acquire(timeout=0.1):
            if cancel and cancel.is_set():
                raise RunCancelled('等待浏览器标签页时已取消')
        try:
            if cancel and cancel.is_set():
                raise RunCancelled('浏览器操作已取消')
            tabs = self.tabs()
            if not tabs:
                raise ToolError(NO_BROWSER_TABS)
            if not any(t['tab_id'] == tab_id for t in tabs):
                raise ToolError('指定标签页未连接，请先 web_scan(tabs_only=true) 查询，不自动切换其他页')
            yield
        finally:
            lock.release()

    def evaluate(self, tab_id, script, timeout=15, expected_url=None):
        guard = '' if expected_url is None else (
            'if (location.href !== ' + json.dumps(expected_url) + ') throw new Error("PAGE_CHANGED");\n')
        # Pin the CDP target inside the command as well: an outer session fallback cannot
        # silently redirect execution to a different tab when the selected tab disappears.
        command = dict(cmd='cdp', tabId=tab_id, method='Runtime.evaluate', params=dict(
            expression='(async () => {\n' + guard + script + '\n})()',
            awaitPromise=True, returnByValue=True))
        result = self._request(dict(cmd='execute_js', sessionId=str(tab_id), code=json.dumps(command),
                                    timeout=str(timeout)), timeout=timeout)
        if not isinstance(result, dict) or 'data' not in result:
            raise ToolError('浏览器未返回最终结果，页面可能正在导航；请重新扫描确认，不自动重试')
        cdp = result['data']
        if not isinstance(cdp, dict) or not isinstance(cdp.get('result'), dict):
            raise ToolError('浏览器 CDP 返回格式错误')
        if cdp.get('exceptionDetails'):
            detail = cdp['exceptionDetails']
            message = detail.get('exception', {}).get('description') or detail.get('text', 'JavaScript error')
            raise ToolError('页面脚本错误：' + str(message)[:400])
        value = cdp['result']
        return value.get('value', value.get('unserializableValue'))
