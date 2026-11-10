"""The orchestration loop knows only client, tools and the public session API."""
import time
from errors import LLMError, RunCancelled


DEFAULT_MAX_STEPS = 40
STRATEGY_REVIEW_STEP = 20
STRATEGY_REVIEW = (
    f'已执行 {STRATEGY_REVIEW_STEP} 步。对照用户目标检查实际进展和剩余工作。'
    '如果连续尝试没有带来新信息，停止重复操作，改用其他方法；必要时向用户说明阻塞。'
)


class Runtime:
    def __init__(self, client, store, tools, max_steps=DEFAULT_MAX_STEPS, context_chars=None):
        if context_chars is None:
            context_chars = getattr(client, 'context_chars', 105000)
        if max_steps < 1 or context_chars < 4000:
            raise ValueError('max_steps >= 1，context_chars >= 4000')
        self.client, self.store, self.tools = client, store, tools
        self.max_steps, self.context_chars = max_steps, context_chars

    def cancel(self, session):
        return self.store.cancel(self.store.key(session))

    def run(self, session, text):
        if not text.strip() or len(text) > self.context_chars // 4:
            raise ValueError('输入为空或过长')
        self.store.open_session(session)
        key = self.store.key(session)
        try:
            with self.store.turn(key, text, self.context_chars,
                                 keep_rate=getattr(self.client, 'trim_keep_rate', 0.6)) as turn:
                for step in range(self.max_steps):
                    schemas = self.tools.schemas
                    reminder = STRATEGY_REVIEW if step == STRATEGY_REVIEW_STEP else ''
                    messages = turn.build_model_messages(schemas, reminder=reminder)
                    response = self.client.chat(messages, schemas, cancel=turn.context.cancelled)
                    tool_calls_with_results = turn.record_response(response)
                    if not tool_calls_with_results:
                        turn.complete(steps=step + 1)
                        return response['content']
                    for call, result_message in tool_calls_with_results:
                        turn.context.check()
                        function = call['function']
                        started = time.monotonic()
                        outcome = self.tools.dispatch(
                            function['name'], function['arguments'], turn.state,
                            turn.context.cancelled)
                        result = outcome.to_dict()
                        turn.save_tool_result(result_message, result)
                        turn.trace('tool', name=function['name'], arguments=function['arguments'], result=result,
                                   elapsed_ms=round((time.monotonic() - started) * 1000))
                raise LLMError('已达到最大工具循环次数；已提交的操作已保存')
        except RunCancelled as exc:
            return str(exc)
