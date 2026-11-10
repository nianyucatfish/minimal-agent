"""Read local files using the working directory for relative paths."""
from collections import deque
from itertools import islice
from errors import ToolError

SCHEMA = {
    'name': 'file_read',
    'description': '读取本地UTF-8文件。相对路径基于工作目录，支持绝对路径。可按起始行、行数和关键词读取。',
    'parameters': {'type': 'object', 'properties': {
        'path': {'type': 'string', 'minLength': 1},
        'start': {'type': 'integer', 'minimum': 1},
        'count': {'type': 'integer', 'minimum': 1, 'maximum': 2000},
        'keyword': {'type': 'string'}, 'show_linenos': {'type': 'boolean'},
        'max_chars': {'type': 'integer', 'minimum': 100, 'maximum': 20000}},
        'required': ['path'], 'additionalProperties': False},
}


def execute(context, path, start=1, count=200, keyword=None, show_linenos=False, max_chars=6000):
    target = context.resolve_path(path)
    try:
        with target.open(encoding='utf-8', errors='replace') as file:
            lines = enumerate(file, 1)
            lines = islice(lines, start-1, None)
            before = deque(maxlen=count//3)
            selected = []
            if keyword:
                for row in lines:
                    if keyword.lower() in row[1].lower():
                        selected = list(before) + [row]
                        break
                    before.append(row)
                else:
                    return dict(execute(context, path, start, count, None, show_linenos, max_chars),
                                notice='关键词未找到，返回起始行附近内容')
            selected += list(islice(lines, count-len(selected)))
            more = next(lines, None) is not None
        text = ''.join(f'{i}|{line}' if show_linenos else line for i,line in selected)
        return dict(path=str(target), text=text[:max_chars], truncated=more or len(text)>max_chars,
                    start=selected[0][0] if selected else start, count=len(selected))
    except OSError as exc:
        raise ToolError('无法读取文件：' + str(exc)) from exc
