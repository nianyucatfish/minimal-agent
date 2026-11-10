import io
import json
import logging
import os
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from client import LLMClient
from session import SessionStore
from errors import LLMError, RunCancelled, StorageError


def response(message):
    return io.BytesIO(json.dumps({'choices': [{'message': message, 'finish_reason': 'stop'}]}).encode())


class ClientTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, LLM_API_KEY='test', LLM_BASE_URL='https://example.test/v1', LLM_MODEL='test')
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)/'config.json'
        path.write_text('{}')
        self.client = LLMClient(path)

    def test_http_auth_error_preserves_cause_and_no_retry(self):
        error = urllib.error.HTTPError('url', 401, 'Unauthorized', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaises(LLMError) as caught:
                self.client.chat([], [])
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(request.call_count, 1)

    def test_retry_then_success(self):
        error = urllib.error.HTTPError('url', 429, 'Rate limited', {'Retry-After': '0'}, None)
        with patch('urllib.request.urlopen', side_effect=[error, response({'content': 'ok'})]) as request:
            with self.assertLogs('agent', logging.WARNING):
                result = self.client.chat([], [])
        self.assertEqual(result['content'], 'ok')
        self.assertEqual(request.call_count, 2)

    def test_cancel_during_backoff(self):
        cancel = threading.Event()
        def fail(*args, **kwargs):
            cancel.set()
            raise urllib.error.URLError('network down')
        with patch('urllib.request.urlopen', side_effect=fail) as request:
            with self.assertLogs('agent', logging.WARNING):
                with self.assertRaises(RunCancelled):
                    self.client.chat([], [], cancel=cancel)
        self.assertEqual(request.call_count, 1)

    def test_malformed_protocol(self):
        for message in [[], {'content': []}, {'content': '', 'tool_calls': ['bad']},
                        {'content': '', 'tool_calls': [{'id': 'x', 'function': None}]}]:
            with patch('urllib.request.urlopen', return_value=response(message)):
                with self.assertRaises(LLMError):
                    self.client.chat([], [])

    def test_file_error_is_translated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(root)
            (Path(root) / 'x.json').write_text('broken')
            with self.assertRaises(StorageError) as caught:
                store.load('x')
            self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_config_file_and_environment_precedence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.json'
            path.write_text(json.dumps(dict(api_key='file-key', base_url='https://example.test/v1/', model='file-model')))
            with patch.dict(os.environ, {}, clear=True):
                client = LLMClient(path)
                self.assertEqual(client.key, 'file-key')
                self.assertEqual(client.base, 'https://example.test/v1')
                self.assertEqual(client.model, 'file-model')
                with patch.dict(os.environ, LLM_MODEL='override'):
                    self.assertEqual(LLMClient(path).model, 'override')

    def test_bad_config_is_safe_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            path = Path(root) / 'config.json'
            for content in ['broken', '[]', '{"api_key":"secret","base_url":123,"model":"m"}',
                            '{"api_key":"secret","base_url":"https://example.test","model":"m","request_options":{"messages":[]}}']:
                path.write_text(content)
                with self.assertRaises(LLMError) as caught:
                    LLMClient(path)
                self.assertNotIn('secret', str(caught.exception))

    def test_direct_mode_bypasses_system_proxy(self):
        path=Path(self.tmp.name)/'direct.json'
        path.write_text(json.dumps(dict(api_key='test',base_url='https://example.test',model='test',proxy_mode='direct',timeout=9)))
        with patch.dict(os.environ,{},clear=True):
            client=LLMClient(path)
        with patch.object(client.http,'open',return_value=response({'content':'ok'})) as direct, \
             patch('urllib.request.urlopen',side_effect=AssertionError('must not use system proxy')):
            self.assertEqual(client.chat([],[])['content'],'ok')
        self.assertEqual(direct.call_args.kwargs['timeout'],9)

    def test_timeout_log_includes_reason_without_secret(self):
        error=urllib.error.URLError(TimeoutError('sensitive token test'))
        with patch('urllib.request.urlopen',side_effect=[error,response({'content':'ok'})]), \
             patch('time.sleep'),self.assertLogs('agent',level='WARNING') as logs:
            self.client.chat([],[])
        text='\n'.join(logs.output)
        self.assertIn('超时',text)
        self.assertIn('2/3',text)
        self.assertIn('system',text)
        self.assertNotIn('sensitive token',text)

    def test_network_failure_preserves_cause_and_details(self):
        error=urllib.error.URLError(ConnectionRefusedError(61,'refused'))
        with patch('urllib.request.urlopen',side_effect=error) as request,patch('time.sleep'), \
             self.assertLogs('agent',level='WARNING'):
            with self.assertRaises(LLMError) as caught:
                self.client.chat([],[])
        self.assertEqual(request.call_count,3)
        self.assertIs(caught.exception.__cause__,error)
        self.assertIn('连接被拒绝',str(caught.exception))
        self.assertIn('system',str(caught.exception))

    def test_tls_verification_failure_not_retried(self):
        import ssl
        error=urllib.error.URLError(ssl.SSLCertVerificationError('certificate invalid'))
        with patch('urllib.request.urlopen',side_effect=error) as request:
            with self.assertRaisesRegex(LLMError,'证书'):
                self.client.chat([],[])
        self.assertEqual(request.call_count,1)

    def test_bad_network_config(self):
        path=Path(self.tmp.name)/'bad-network.json'
        for options in [dict(proxy_mode='unknown'),dict(timeout=0),dict(timeout=True)]:
            path.write_text(json.dumps(dict(api_key='test',base_url='https://example.test',model='test',**options)))
            with self.assertRaises(LLMError):
                LLMClient(path)
