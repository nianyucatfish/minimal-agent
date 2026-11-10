"""Test the real local HTTP protocol against a stub browser bridge, not a user's browser."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from browser.client import BrowserBridge
from errors import RunCancelled, ToolError
from tools import Tools


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                request = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                cls.requests.append(request)
                if request['cmd'] == 'get_all_sessions':
                    result = cls.tabs
                elif cls.response is not None:
                    result = cls.response
                else:
                    result = {'data': {'result': {'type': 'object', 'value': {'title': 'Sample', 'url': 'https://example.test', 'content':'hello'}}}}
                body = json.dumps({'r': result}).encode()
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/link'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        type(self).tabs = [dict(id='7', type='ext_ws', url='https://example.test', title='Sample'),
                           dict(id='8', type='ws', url='https://other.test')]
        type(self).requests = []
        type(self).response = None
        self.tools = Tools('.', browser_url=self.url, modules=('web_scan','web_execute_js'))

    def dispatch(self, tool, **args):
        return self.tools.dispatch(tool, json.dumps(args), {})

    def test_tabs_and_no_extension(self):
        result = self.dispatch('web_scan', tabs_only=True).data
        self.assertEqual([t['tab_id'] for t in result['tabs']], [7])
        type(self).tabs = []
        scan = self.dispatch('web_scan', tabs_only=True).data
        self.assertFalse(scan['extension_connected'])
        self.assertIn('browser/extension/', scan['notice'])
        execution = self.dispatch('web_execute_js', tab_id=7, script='return 1')
        self.assertEqual(execution.code, 'TOOL_ERROR')
        self.assertIn('browser/extension/', execution.error)

    def test_scan_requires_explicit_tab(self):
        self.assertFalse(self.dispatch('web_scan').to_dict()['ok'])
        self.assertEqual(self.requests, [])

    def test_stale_tab_not_executed(self):
        self.assertFalse(self.dispatch('web_execute_js', tab_id=99, script='return 1').to_dict()['ok'])
        self.assertTrue(all(r['cmd'] == 'get_all_sessions' for r in self.requests))

    def test_target_pinned_in_both_protocol_layers(self):
        result = self.dispatch('web_execute_js', tab_id=7, script='return 42', expected_url='https://example.test')
        self.assertTrue(result.to_dict()['ok'])
        request = self.requests[-1]
        self.assertEqual(request['sessionId'], '7')
        command = json.loads(request['code'])
        self.assertEqual(command['tabId'], 7)
        self.assertEqual(command['method'], 'Runtime.evaluate')
        self.assertIn('PAGE_CHANGED', command['params']['expression'])
        self.assertIn('return 42', command['params']['expression'])

    def test_scan_uses_bounded_snapshot_and_strips_values(self):
        result = self.dispatch('web_scan', tab_id=7, max_chars=800, text_only=True)
        self.assertEqual(result.data['page']['title'], 'Sample')
        expression = json.loads(self.requests[-1]['code'])['params']['expression']
        self.assertIn("removeAttribute('value')", expression)
        self.assertIn('slice(0,800)', expression)

    def test_page_exception_and_ambiguous_timeout(self):
        type(self).response = {'data': {'result': {}, 'exceptionDetails': {'text':'PAGE_CHANGED'}}}
        self.assertFalse(self.dispatch('web_execute_js', tab_id=7, script='return 1').to_dict()['ok'])
        type(self).response = {'result':'no response, ACK received'}
        result = self.dispatch('web_execute_js', tab_id=7, script='return 1')
        self.assertFalse(result.to_dict()['ok'])
        self.assertIn('不自动重试', result.error)

    def test_bridge_error_is_not_silently_returned_as_success(self):
        type(self).response = {'error':'tab closed'}
        self.assertFalse(self.dispatch('web_execute_js', tab_id=7, script='return 1').to_dict()['ok'])

    def test_large_result_preview(self):
        type(self).response = {'data': {'result': {'value':'x'*2000}}}
        result = self.dispatch('web_execute_js', tab_id=7, script='return 1', max_chars=500).data
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['preview']), 500)

    def test_only_loopback_bridge(self):
        with self.assertRaises(ToolError):
            BrowserBridge('https://external.test/link')

    def test_precancelled_browser_operation(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(RunCancelled):
            with BrowserBridge(self.url).selected(7, cancel):
                self.fail('should not execute')
        self.assertEqual(self.requests, [])

    def test_connection_failure_is_tool_error(self):
        bridge = BrowserBridge(self.url)
        from unittest.mock import patch
        import urllib.error
        with patch.object(bridge.http, 'open', side_effect=urllib.error.URLError('offline')):
            with self.assertRaises(ToolError):
                bridge.tabs()
