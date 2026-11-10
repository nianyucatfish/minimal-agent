"""Render the prompt template and fit model messages to the request budget."""
from copy import deepcopy
from html import escape
import json
from pathlib import Path

from errors import LLMError
from memory import remember

prompt_template = Path(__file__).with_name('system_prompt.txt').read_text(encoding='utf-8')
tail_heading = '【工作记忆】'
system_section, tail_body = prompt_template.split(f'\n{tail_heading}\n', 1)
SYSTEM_TEMPLATE = system_section.strip()
WORKING_MEMORY_TEMPLATE = tail_heading + '\n' + tail_body.strip()
INLINE_NOTES_LIMIT = 1200


def read_short_notes(notes_path):
    if notes_path is None or not notes_path.is_file():
        return ''
    try:
        with notes_path.open(encoding='utf-8') as file:
            notes = file.read(INLINE_NOTES_LIMIT + 1)
    except (OSError, UnicodeError):
        return ''
    return notes.strip() if len(notes) <= INLINE_NOTES_LIMIT else ''


def long_term_memory_guidance(notes_path, tools, notes):
    if notes_path is None:
        return []
    tool_names = {schema['function']['name'] for schema in tools}
    if 'file_read' not in tool_names:
        return []

    path = json.dumps(str(notes_path), ensure_ascii=False)
    guidance = []
    if notes:
        guidance.append('工作记忆中的长期笔记包含用户偏好和约定。先检查其中是否有匹配当前消息的约定，再决定如何响应。')
    elif notes_path.is_file():
        guidance.append(f'长期记忆在 {path}。理解本轮用户意图前先读取，检查是否有相关偏好或触发约定。')
    if 'file_write' in tool_names:
        guidance.append(
            f'用户明确要求长期记住稳定偏好或经验时，可用 file_write 写入 {path}。'
            '文件存在时先读取再更新；不保存密钥或临时任务状态。'
        )
    return guidance


def compact(state, context_chars, *, current_turn_index=None, size=None, keep_rate=0.6):
    """Drop whole turns/complete tool batches, retaining current input and latest batch."""
    if size is None:
        size = lambda: len(json.dumps(state['turns'], ensure_ascii=False)) + len(state['summary'])
    if size() <= context_chars:
        return
    target = int(context_chars * keep_rate)
    while state['turns'] and size() > target:
        if current_turn_index is None or current_turn_index > 0:
            old = state['turns'].pop(0)
            if current_turn_index is not None:
                current_turn_index -= 1
            # Migration fallback for sessions created before incremental summaries existed.
            if not state['summary']:
                for message in old:
                    if message['role'] in ('user', 'assistant'):
                        remember(state, message['role'], message.get('content') or '')
            continue
        current_messages = state['turns'][0]
        starts = [i for i, message in enumerate(current_messages)
                  if message['role'] == 'assistant' and message.get('tool_calls')]
        if len(starts) < 2:
            break
        del current_messages[1:starts[-1]]


def build_messages(state, context_chars, current_turn_index, system=None, tools=(),
                   keep_rate=0.6, *, notes_path=None, reminder=''):
    """Build a disposable model request; never mutate the stored transcript."""
    if not 0 <= current_turn_index < len(state['turns']):
        raise ValueError('当前轮次索引超出会话范围')
    notes = read_short_notes(notes_path) if system is None else ''
    if system is None:
        memory_guidance = long_term_memory_guidance(notes_path, tools, notes)
        if memory_guidance:
            system = SYSTEM_TEMPLATE.replace('{long_term_memory_guidance}', '\n'.join(memory_guidance))
        else:
            system = SYSTEM_TEMPLATE.removesuffix('【长期记忆导航】\n{long_term_memory_guidance}').rstrip()

    reminder_messages = [dict(role='user', content=f'【执行提醒】\n{reminder}')] if reminder else []
    state = deepcopy(state)
    # The compact note is state, not a message: never append it to stored turns.
    def assemble():
        history = escape(state['summary'])
        checkpoint = escape(state.get('checkpoint', ''))
        plan = escape(json.dumps(state.get('current_plan'), ensure_ascii=False, separators=(',', ':')))
        working_memory = WORKING_MEMORY_TEMPLATE.format(
            history=history, checkpoint=checkpoint, plan=plan,
            long_term_notes=f'\n<long_term_notes>{escape(notes)}</long_term_notes>' if notes else '',
        )

        return [
            dict(role='system', content=system),
            *(message for turn in state['turns'] for message in turn),
            *reminder_messages,
            dict(role='user', content=working_memory),
        ]

    def request_size(messages):
        return len(json.dumps(dict(messages=messages, tools=tools), ensure_ascii=False))

    compact(state, context_chars, current_turn_index=current_turn_index, size=lambda: request_size(assemble()), keep_rate=keep_rate)
    messages = assemble()
    if request_size(messages) > context_chars:
        raise LLMError('本轮上下文预算耗尽；最近工具结果仍过大，请拆分任务')
    return messages
