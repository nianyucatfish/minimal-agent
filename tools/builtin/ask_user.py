"""Human input supplied by the host interface, never guessed by the model."""
from errors import ToolError

SCHEMA = {
    'name': 'ask_user',
    'description': '需要澄清关键需求、选择方案时向用户提问并等待回答。支持建议选项，用户也可自由输入。不要用于不影响执行的琐碎问题。',
    'parameters': {'type': 'object', 'properties': {
        'question': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'options': {'type': 'array', 'maxItems': 5,
                    'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
    }, 'required': ['question'], 'additionalProperties': False},
}


def execute(context, question, options=None):
    if not question.strip() or any(not item.strip() for item in options or []):
        raise ToolError('问题和选项不能为空白')
    if context.ask_user is None:
        raise ToolError('当前运行界面不支持用户提问；请在回复中直接询问用户')
    answer = context.ask_user(question, options or [], context.cancel)
    if not isinstance(answer, str) or not answer.strip():
        raise ToolError('未收到有效回答')
    return {'answer': answer.strip()}
