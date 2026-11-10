"""Explicit local file writes with the same path rules as file_read and code_run."""
from errors import ToolError

SCHEMA = {
    'name': 'file_write',
    'description': '写入本地UTF-8文件，支持overwrite/append/prepend。相对路径基于工作目录，支持绝对路径。',
    'parameters': {'type': 'object', 'properties': {
        'path': {'type': 'string', 'minLength': 1},
        'content': {'type': 'string', 'maxLength': 1000000},
        'mode': {'type': 'string', 'enum': ['overwrite','append','prepend']}},
        'required': ['path','content'], 'additionalProperties': False},
}


def execute(context, path, content, mode='overwrite'):
    target = context.resolve_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == 'prepend':
            old = target.read_text(encoding='utf-8') if target.exists() else ''
            target.write_text(content+old, encoding='utf-8')
        else:
            with target.open('a' if mode == 'append' else 'w', encoding='utf-8') as file:
                file.write(content)
        return dict(path=str(target), mode=mode, written_chars=len(content))
    except (OSError, UnicodeError) as exc:
        raise ToolError('无法写入文件：' + str(exc)) from exc
