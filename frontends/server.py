"""Local Web frontend for Minimal Agent."""
import argparse
import json
import logging
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from browser.client import ensure_browser_bridge
from client import LLMClient
from errors import AgentError, RunCancelled, ToolError
from runtime import DEFAULT_MAX_STEPS, Runtime
from session import SessionStore
from tools import Tools

HERE = Path(__file__).resolve().parent
CORE = HERE.parent


class Application:
    def __init__(self, core, max_steps=DEFAULT_MAX_STEPS, *, config_path=None):
        if max_steps < 1:
            raise ValueError('max_steps 必须大于零')
        self.max_steps = max_steps
        self.config_path = config_path
        self.store = SessionStore(core / 'data')
        self.core = core
        self.lock = threading.Lock()
        self.jobs = {}
        self.questions = {}
        self.store.open_session('default')

    def session_title(self, name, state):
        title = state.get('_meta', {}).get('title')
        if title:
            return title
        generic = (name == 'default' or name.startswith(('chat-', 'session-', '新对话 ')))
        if not generic:
            return name
        first = next((m.get('content', '') for turn in state['turns'] for m in turn
                      if m['role'] == 'user' and m.get('content')), '')
        return self.make_title(first) if first else '新对话'

    @staticmethod
    def make_title(text):
        title = ' '.join(text.split())
        return title[:24] + ('…' if len(title) > 24 else '')

    def snapshot(self, name):
        names = self.store.list_sessions()
        if name not in {s['name'] for s in names}:
            raise ValueError('会话不存在')
        for item in names:
            item['title'] = self.session_title(item['name'], self.store.load(
                self.store.key(item['name'])))
        state = self.store.load(self.store.key(name))
        with self.lock:
            job = dict(self.jobs.get(name, {}))
            pending = self.questions.get(name)
            question = ({k: pending[k] for k in ('id', 'question', 'options')}
                        if pending and not pending['event'].is_set() else None)
        messages = []
        for turn_index, turn in enumerate(state['turns']):
            results = {m['tool_call_id']: m.get('content', '') for m in turn
                       if m['role'] == 'tool' and m.get('tool_call_id')}
            for msg in turn:
                if msg['role'] in ('user', 'assistant') and msg.get('content'):
                    messages.append(dict(role=msg['role'], content=msg['content'], turn=turn_index))
                for call in msg.get('tool_calls', []):
                    result = results.get(call['id'], '')
                    try:
                        parsed = json.loads(result)
                    except (ValueError, TypeError):
                        parsed = {}
                    pending = parsed.get('error', {}).get('code') == 'NOT_EXECUTED'
                    status = ('running' if job.get('running') else 'unknown') if pending or not result else (
                        'completed' if parsed.get('ok') else 'failed')
                    messages.append(dict(role='tool', turn=turn_index, id=f"{turn_index}:{call['id']}",
                                         name=call['function']['name'], status=status,
                                         arguments=call['function']['arguments'], result=result))
        for message in messages:
            message['finished'] = (message['turn'] < len(state['turns']) - 1 or
                                   (not job.get('running') and
                                    state.get('last_run', {}).get('status') != 'running'))
        return dict(sessions=names, title=self.session_title(name, state), messages=messages, plan=state.get('current_plan'),
                    todos=state.get('todos', []), job=job, question=question)

    def ask_user(self, name, question, options, cancel):
        pending = dict(id=secrets.token_hex(12), question=question, options=options,
                       event=threading.Event(), answer=None)
        with self.lock:
            if name in self.questions:
                raise ValueError('当前会话已有待回答的问题')
            self.questions[name] = pending
        try:
            while not pending['event'].wait(0.2):
                if cancel and cancel.is_set():
                    raise RunCancelled('用户提问已取消')
            if cancel and cancel.is_set():
                raise RunCancelled('用户提问已取消')
            return pending['answer']
        finally:
            with self.lock:
                if self.questions.get(name) is pending:
                    self.questions.pop(name, None)

    def answer_question(self, name, question_id, answer):
        if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 6000:
            raise ValueError('回答需为 1–6000 字')
        with self.lock:
            pending = self.questions.get(name)
            if not pending or pending['id'] != question_id or pending['event'].is_set():
                raise ValueError('问题已结束或已回答，请刷新')
            pending['answer'] = answer.strip()
            pending['event'].set()

    def manage_session(self, name, action, title=None):
        with self.lock:
            if self.jobs.get(name, {}).get('running'):
                raise ValueError('会话正在执行，请停止或等待完成后再操作')
            key = self.store.key(name)
            with self.store.lock(key):
                path = self.store.root / (key + '.json')
                if not path.exists():
                    raise ValueError('会话不存在')
                state = self.store.load(key)
                if state.get('_meta', {}).get('session') != name:
                    raise ValueError('会话不存在')
                if action == 'rename':
                    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
                        raise ValueError('标题需为 1–100 字')
                    state['_meta']['title'] = title.strip()
                    self.store.save(key, state)
                else:
                    trash = self.store.root / '.trash'
                    trash.mkdir(exist_ok=True)
                    path.replace(trash / (key + '-' + secrets.token_hex(4) + '.json'))
                    self.jobs.pop(name, None)
            remaining = self.store.list_sessions()
            if not remaining:
                next_name = 'chat-' + secrets.token_hex(12)
                self.store.open_session(next_name, mode='create')
            else:
                next_name = remaining[0]['name']
            return next_name

    def send(self, name, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 6000:
            raise ValueError('请输入 1–6000 字的消息')
        self.snapshot(name)
        with self.lock:
            if not (self.store.root / (self.store.key(name) + '.json')).exists():
                raise ValueError('会话不存在')
            if self.jobs.get(name, {}).get('running'):
                raise ValueError('这个会话仍在处理中')
            self.jobs[name] = dict(running=True, error='')
        def work():
            error = ''
            try:
                key = self.store.key(name)
                with self.store.lock(key):
                    state = self.store.load(key)
                    if not state['turns'] and not state.get('_meta', {}).get('title'):
                        state.setdefault('_meta', {})['title'] = self.make_title(text)
                        self.store.save(key, state)
                runtime = Runtime(LLMClient(self.config_path), self.store,
                                  Tools(str(self.core / 'temp'), store=self.store, ask_user=
                                        lambda question, options, cancel: self.ask_user(
                                            name, question, options, cancel)), max_steps=self.max_steps)
                runtime.run(name, text)
            except (AgentError, ValueError, OSError) as exc:
                error = str(exc)
            except Exception:
                logging.exception('GUI run failed')
                error = '运行失败，请查看服务端日志'
            finally:
                with self.lock:
                    self.jobs[name] = dict(running=False, error=error)
        threading.Thread(target=work, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='本地 JSON API 配置文件路径')
    parser.add_argument('--with-browser', action='store_true', help='同时启动或复用浏览器桥接服务')
    parser.add_argument('--port', type=int, default=8899)
    parser.add_argument('--max-steps', type=int, default=DEFAULT_MAX_STEPS)
    args = parser.parse_args()
    app = Application(CORE, args.max_steps, config_path=args.config)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data, content_type='application/json; charset=utf-8'):
            body = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith('application/json') else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/':
                page = (HERE / 'index.html').read_text().replace('__TOKEN__', token)
                return self.reply(200, page.encode(), 'text/html; charset=utf-8')
            if url.path in ('/vendor/marked.js', '/vendor/purify.js'):
                return self.reply(200, (HERE / url.path.lstrip('/')).read_bytes(),
                                  'text/javascript; charset=utf-8')
            if self.headers.get('X-GUI-Token') != token:
                return self.reply(403, {'error': '请从本地 GUI 页面访问'})
            try:
                if url.path != '/api/state':
                    return self.reply(404, {'error': '不存在的接口'})
                self.reply(200, app.snapshot(parse_qs(url.query).get('session', ['default'])[0]))
            except (AgentError, ValueError, OSError) as exc:
                self.reply(400, {'error': str(exc)})

        def do_POST(self):
            if self.headers.get('X-GUI-Token') != token:
                return self.reply(403, {'error': '请从本地 GUI 页面操作'})
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 32000:
                    raise ValueError('请求过大或为空')
                body = json.loads(self.rfile.read(length))
                name = body.get('session')
                if not isinstance(name, str) or not name.strip() or len(name) > 100:
                    raise ValueError('会话名称需为 1–100 字')
                if self.path == '/api/new':
                    app.store.open_session(name, mode='create')
                elif self.path in ('/api/rename', '/api/delete'):
                    next_name = app.manage_session(name, self.path.rsplit('/', 1)[1], body.get('title'))
                    return self.reply(200, {'ok': True, 'next_session': next_name})
                elif self.path == '/api/answer':
                    app.answer_question(name, body.get('question_id'), body.get('answer'))
                elif self.path == '/api/send':
                    app.send(name, body.get('text'))
                elif self.path == '/api/cancel':
                    app.store.cancel(app.store.key(name))
                else:
                    return self.reply(404, {'error': '不存在的接口'})
                self.reply(200, {'ok': True})
            except (AgentError, ValueError, OSError, TypeError, AttributeError) as exc:
                self.reply(400, {'error': str(exc)})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    try:
        if args.with_browser:
            ensure_browser_bridge()
        print(f'打开 http://127.0.0.1:{args.port} （Ctrl+C 退出）', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ToolError) as exc:
        parser.exit(1, f'{exc}\n')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
