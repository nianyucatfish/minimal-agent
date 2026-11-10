"""Standalone loopback HTTP/WebSocket relay for a compatible Chrome extension."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid


class BrowserRelay:
    def __init__(self):
        self.lock = threading.RLock()
        self.tabs = {}
        self.pending = {}

    def connected_tabs(self):
        with self.lock:
            return [dict(info, id=tab_id, type='ext_ws') for tab_id, (_, info) in self.tabs.items()]

    def receive(self, client, message):
        with self.lock:
            kind = message.get('type')
            if kind in ('ext_ready', 'tabs_update'):
                self.tabs = {key:value for key,value in self.tabs.items() if value[0] is not client}
                for tab in message.get('tabs', []):
                    if str(tab.get('id', '')).isdigit() and str(tab.get('url', '')).startswith(('http://','https://')):
                        self.tabs[str(tab['id'])] = (client, dict(url=tab['url'], title=tab.get('title','')))
            elif kind in ('result', 'error'):
                pending = self.pending.get(message.get('id'))
                if pending and pending['client'] is client:
                    pending['result'] = ({'data':message.get('result')} if kind=='result'
                                         else {'error':str(message.get('error','Browser error'))})
                    pending['done'].set()

    def disconnect(self, client):
        with self.lock:
            self.tabs = {key:value for key,value in self.tabs.items() if value[0] is not client}
            for pending in self.pending.values():
                if pending['client'] is client:
                    pending['result'] = {'error':'Browser extension disconnected; outcome may be unknown'}
                    pending['done'].set()

    def execute(self, tab_id, code, timeout):
        with self.lock:
            if str(tab_id) not in self.tabs:
                return {'error':'Selected tab is unavailable; no fallback is performed'}
            client, _ = self.tabs[str(tab_id)]
            request_id = uuid.uuid4().hex
            pending = dict(client=client, done=threading.Event(), result=None)
            self.pending[request_id] = pending
            try:
                client.send_message(json.dumps(dict(id=request_id, tabId=int(tab_id), code=code)))
            except Exception:
                self.pending.pop(request_id, None)
                return {'error':'Failed to send browser request'}
        try:
            if not pending['done'].wait(timeout):
                return {'error':'Browser request timed out; outcome may be unknown'}
            return pending['result']
        finally:
            with self.lock:
                self.pending.pop(request_id, None)


def http_handler(relay):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/link':
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 100000:
                    raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(length))
                if data.get('cmd') == 'get_all_sessions':
                    result = relay.connected_tabs()
                elif data.get('cmd') == 'execute_js':
                    timeout = float(data.get('timeout',15))
                    if not 0 < timeout <= 30 or not isinstance(data.get('code'), str):
                        raise ValueError('Invalid execute request')
                    result = relay.execute(data.get('sessionId'), data['code'], timeout)
                else:
                    raise ValueError('Unknown bridge command')
                body = json.dumps({'r':result}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, TypeError, AttributeError) as exc:
                self.send_error(400, str(exc))

        def log_message(self, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ws-port', type=int, default=18765)
    parser.add_argument('--http-port', type=int, default=18766)
    args = parser.parse_args()
    try:
        from simple_websocket_server import WebSocket, WebSocketServer
    except ImportError:
        parser.error('Install requirements-browser.txt to run the standalone relay')
    relay = BrowserRelay()
    class Connection(WebSocket):
        def handle(self):
            try:
                relay.receive(self, json.loads(self.data))
            except (ValueError, TypeError, AttributeError):
                self.close()
        def connected(self):
            pass
        def handle_close(self):
            relay.disconnect(self)
    http = ThreadingHTTPServer(('127.0.0.1',args.http_port),http_handler(relay))
    http.daemon_threads = True
    try:
        websocket = WebSocketServer('127.0.0.1',args.ws_port,Connection)
    except Exception:
        http.server_close()
        raise
    threading.Thread(target=http.serve_forever, daemon=True).start()
    print(f'Browser relay: ws://127.0.0.1:{args.ws_port}, http://127.0.0.1:{args.http_port}/link',flush=True)
    try:
        websocket.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        websocket.close()
        http.shutdown()
        http.server_close()


if __name__ == '__main__':
    main()
