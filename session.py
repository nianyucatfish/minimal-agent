"""Session history, checkpoints and execution ownership."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid
from errors import AgentError, LLMError, RunCancelled, StorageError
from context import build_messages
from memory import extract_summary, remember

logger = logging.getLogger('agent')

_LOCAL_NAMESPACE = 'local'  # Preserve the existing local session file keys.


@dataclass
class RunContext:
    id: str
    cancelled: threading.Event

    def check(self):
        if self.cancelled.is_set():
            raise RunCancelled('执行已取消；已提交的操作仍保留')


class SessionStore:
    """Own durable transcripts and execution ownership.

    lock(key): serialize an entire run for one session; other sessions can run.
    _guard: protect lock/owner maps and make ownership-check + save atomic
    relative to cancellation. Never acquire a session lock while holding _guard.
    Both locks are process-local; they do not coordinate separate CLI/GUI processes.
    """
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._locks = {}
        self._active = {}

    def key(self, session):
        import hashlib
        return hashlib.sha256(json.dumps([_LOCAL_NAMESPACE, session]).encode()).hexdigest()

    def lock(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def open_session(self, session, mode='ensure'):
        """Ensure, create, or resume an identified session without replacing its content."""
        if not session.strip():
            raise ValueError('会话名称不能为空')
        if mode not in ('ensure', 'create', 'resume'):
            raise ValueError('未知会话打开方式')
        key = self.key(session)
        with self.lock(key):
            exists = (self.root / (key + '.json')).exists()
            if mode == 'create' and exists:
                raise ValueError('会话已存在，请用 /switch 切换')
            if mode == 'resume' and not exists:
                raise ValueError('会话不存在，请用 /new 创建')
            state = self.load(key)
            meta = state.get('_meta')
            if meta is not None and (not isinstance(meta, dict) or
                                     meta.get('user') != _LOCAL_NAMESPACE or meta.get('session') != session):
                raise StorageError('会话身份元数据不匹配')
            if meta is None:
                state['_meta'] = dict(user=_LOCAL_NAMESPACE, session=session, created_at=time.time())
                self.save(key, state)
            return state

    def list_sessions(self):
        """List identified local sessions; never expose conversation bodies."""
        sessions = []
        for path in self.root.glob('*.json'):
            try:
                state = self.load(path.stem)
                meta = state.get('_meta', {})
                if (not isinstance(meta, dict) or meta.get('user') != _LOCAL_NAMESPACE or
                    not isinstance(meta.get('session'), str) or
                    self.key(meta['session']) != path.stem):
                    continue
                sessions.append(dict(name=meta['session'], turns=len(state['turns']),
                                     updated_at=float(meta.get('updated_at', meta.get('created_at', 0)))))
            except (StorageError, ValueError, TypeError):
                logger.warning('跳过无法列出的会话文件 %s', path.name)
        return sorted(sessions, key=lambda item: item['updated_at'], reverse=True)

    def load(self, key):
        path = self.root / (key + '.json')
        if not path.exists():
            return dict(turns=[], summary='', checkpoint='', current_plan=None, todos=[])
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(state, dict) or not isinstance(state.get('turns'), list)
                or not isinstance(state.get('summary'), str)
                or ('todos' in state and not isinstance(state['todos'], list))):
                raise ValueError('会话结构不合法')
            state.setdefault('todos', [])
            state.setdefault('current_plan', None)
            plan = state['current_plan']
            if plan is not None:
                self._validate_plan(plan)
            state.setdefault('checkpoint', '')
            if not isinstance(state['checkpoint'], str):
                raise ValueError('checkpoint 必须为字符串')
            return state
        except (OSError, ValueError) as exc:
            raise StorageError('会话读取失败，请检查状态文件') from exc

    @staticmethod
    def _validate_plan(plan):
        if (not isinstance(plan, dict) or not isinstance(plan.get('id'), str) or not plan['id']
            or not isinstance(plan.get('title'), str) or not plan['title'].strip()
            or plan.get('status') not in ('active', 'completed', 'cancelled')
            or not isinstance(plan.get('steps'), list) or not 1 <= len(plan['steps']) <= 10):
            raise ValueError('计划结构不合法')
        ids, running, completed = set(), 0, 0
        for step in plan['steps']:
            if (not isinstance(step, dict) or not isinstance(step.get('id'), str) or not step['id']
                or step['id'] in ids or not isinstance(step.get('title'), str) or not step['title'].strip()
                or step.get('status') not in ('pending', 'in_progress', 'completed')):
                raise ValueError('计划步骤不合法')
            ids.add(step['id'])
            running += step['status'] == 'in_progress'
            completed += step['status'] == 'completed'
        if running > 1 or (plan['status'] != 'cancelled' and
                           (plan['status'] == 'completed') != (completed == len(plan['steps']))):
            raise ValueError('计划状态与步骤不一致')

    def save(self, key, state):
        if '_meta' in state:
            state['_meta']['updated_at'] = time.time()
        path = self.root / (key + '.json')
        tmp = self.root / (key + '.' + uuid.uuid4().hex + '.tmp')
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, path)
        except OSError as exc:
            raise StorageError('会话保存失败，不能确认本轮状态已持久化') from exc
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                logger.warning('无法清理临时文件 %s', tmp)

    def begin(self, key):
        with self._guard:
            context = RunContext(uuid.uuid4().hex, threading.Event())
            self._active[key] = context
            return context

    def cancel(self, key):
        with self._guard:
            context = self._active.pop(key, None)
            if context:
                context.cancelled.set()
            return context is not None

    def commit(self, key, context, state):
        # Identity check and write are one critical section relative to cancellation.
        with self._guard:
            context.check()
            if self._active.get(key) is not context:
                raise RunCancelled('旧执行结果已丢弃')
            self.save(key, state)

    def finish(self, key, context):
        with self._guard:
            if self._active.get(key) is context:
                self._active.pop(key)

    def trace(self, key, run, event, **data):
        row = dict(ts=time.time(), run_id=run, event=event, **data)
        try:
            with (self.root / (key + '.jsonl')).open('a', encoding='utf-8') as file:
                file.write(json.dumps(row, ensure_ascii=False) + '\n')
        except OSError:
            # Telemetry failure must not turn a committed business action into a retry.
            logger.exception('trace 写入失败 run_id=%s', run)

    @contextmanager
    def turn(self, key, text, context_chars, keep_rate=0.6):
        with self.lock(key):
            context = self.begin(key)
            self.trace(key, context.id, 'run_start')
            try:
                yield SessionTurn(self, key, context, text, context_chars, keep_rate)
            except RunCancelled:
                self._terminal(key, context, 'cancelled')
                self.trace(key, context.id, 'run_cancelled')
                raise
            except AgentError as exc:
                self.trace(key, context.id, 'run_error', code=exc.code, error=str(exc))
                if not isinstance(exc, StorageError):
                    self._terminal(key, context, 'failed', str(exc), exc.code)
                raise
            except Exception:
                logger.exception('Runtime 未知异常 run_id=%s', context.id)
                self.trace(key, context.id, 'run_error', code='INTERNAL_ERROR')
                self._terminal(key, context, 'failed', '执行出现内部错误', 'INTERNAL_ERROR')
                raise
            finally:
                self.finish(key, context)

    def _terminal(self, key, context, status, message='', code=''):
        # Load the last checkpoint, never persist an interrupted working snapshot.
        try:
            with self._guard:
                owner = self._active.get(key)
                cancelled_owner_released = status == 'cancelled' and owner is None
                if owner is not context and not cancelled_owner_released:
                    return
                saved = self.load(key)
                saved['last_run'] = dict(id=context.id, status=status)
                if code:
                    saved['last_run']['code'] = code
                if message and saved['turns']:
                    saved['turns'][-1].append(dict(role='assistant', content=message))
                self.save(key, saved)
        except StorageError:
            logger.exception('记录终止状态时发生存储错误 run_id=%s', context.id)


class SessionTurn:
    """Record and persist the messages produced by one run."""

    def __init__(self, store, key, context, text, context_chars, keep_rate=0.6):
        self.store, self.key, self.context = store, key, context
        self.context_chars = context_chars
        self.keep_rate = keep_rate
        self.state = store.load(key)
        if not self.state['summary']:
            for old_turn in self.state['turns']:
                for message in old_turn:
                    if message['role'] in ('user', 'assistant'):
                        remember(self.state, message['role'], message.get('content') or '')
        self.current_turn_index = len(self.state['turns'])
        self.current_messages = [dict(role='user', content=text)]
        self.state['turns'].append(self.current_messages)
        remember(self.state, 'User', text)
        self.state['last_run'] = dict(id=context.id, status='running')
        self.save_state()

    def save_state(self):
        """Persist the full session; unrelated to the working-memory checkpoint field."""
        self.store.commit(self.key, self.context, self.state)

    def trace(self, event, **data):
        self.store.trace(self.key, self.context.id, event, **data)

    def build_model_messages(self, tools=(), *, reminder=''):
        """Read-only request construction; compression never writes to storage."""
        self.context.check()
        return build_messages(
            self.state, self.context_chars, self.current_turn_index,
            tools=tools, keep_rate=self.keep_rate,
            notes_path=self.store.root.resolve() / 'memory.md', reminder=reminder,
        )

    def record_response(self, response):
        """Extract summary, append the response and persist tool placeholders.

        Normalizes response.content in place so the caller returns visible text.
        Each returned pair contains a call and its stored result placeholder.
        """
        self.context.check()
        extract_summary(response, self.state)
        calls = response.get('tool_calls') or []
        if not calls and not response['content']:
            raise LLMError('模型只返回了摘要，缺少面向用户的答案')

        self.current_messages.append(response)
        if not calls:
            return []

        not_executed = json.dumps(
            dict(ok=False, error=dict(code='NOT_EXECUTED', message='未确认执行，请查询状态')),
            ensure_ascii=False,
        )
        placeholders = [
            dict(role='tool', tool_call_id=call['id'], content=not_executed)
            for call in calls
        ]
        self.current_messages.extend(placeholders)
        self.save_state()
        return list(zip(calls, placeholders))

    def save_tool_result(self, message, result):
        """Replace a stored placeholder and persist tool state changes together."""
        message['content'] = json.dumps(result, ensure_ascii=False)
        self.save_state()

    def complete(self, steps):
        self.state['last_run']['status'] = 'completed'
        self.save_state()
        self.trace('run_end', steps=steps)
