"""Relay lifecycle, late results and selected-target guarantees without Chrome."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from browser.client import BrowserBridge
from browser.service import BrowserRelay, http_handler


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.relay = BrowserRelay()
        relay = self.relay
        class Client:
            def __init__(self):
                self.sent = []
            def send_message(self, text):
                message = json.loads(text)
                self.sent.append(message)
                relay.receive(self, dict(type='result',id=message['id'],result={'result':{'value':42}}))
        self.client = Client()
        self.relay.receive(self.client,dict(type='ext_ready',tabs=[dict(id=7,url='https://example.test',title='Sample')]))

    def test_exact_target_and_request_cleanup(self):
        self.assertEqual(self.relay.execute('7','code',1)['data']['result']['value'],42)
        self.assertEqual(self.client.sent[0]['tabId'],7)
        self.assertEqual(self.relay.pending,{})
        self.assertIn('error',self.relay.execute('99','code',1))
        self.assertEqual(len(self.client.sent),1)

    def test_timeout_ignores_late_result(self):
        self.client.send_message = lambda text: self.client.sent.append(json.loads(text))
        self.assertIn('error',self.relay.execute('7','code',0.01))
        self.relay.receive(self.client,dict(type='result',id=self.client.sent[0]['id'],result=42))
        self.assertEqual(self.relay.pending,{})

    def test_disconnect_wakes_waiter(self):
        self.client.send_message = lambda text: self.relay.disconnect(self.client)
        self.assertIn('disconnected',self.relay.execute('7','code',1)['error'])
        self.assertEqual(self.relay.connected_tabs(),[])

    def test_http_client_works_with_standalone_relay(self):
        server = ThreadingHTTPServer(('127.0.0.1',0),http_handler(self.relay))
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            bridge = BrowserBridge(f'http://127.0.0.1:{server.server_port}/link')
            self.assertEqual(bridge.tabs()[0]['tab_id'],7)
            with bridge.selected(7):
                self.assertEqual(bridge.evaluate(7,'return 42'),42)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
