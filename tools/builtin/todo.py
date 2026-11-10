"""User-facing to-do items, independent of the Agent's execution plan."""
from copy import deepcopy
import uuid
from errors import ToolError

SCHEMA = {
    'name': 'todo',
    'description': '管理用户待办，不是执行计划。add(title) 新增；list() 查询；complete(id) 完成；delete(id) 删除。不提供定时提醒。',
    'parameters': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['add', 'list', 'complete', 'delete']},
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 500},
            'id': {'type': 'string', 'minLength': 1},
        },
        'required': ['action'],
        'additionalProperties': False,
    },
}
FIELDS = {'add': {'title'}, 'list': set(), 'complete': {'id'}, 'delete': {'id'}}


def execute(context, action, **args):
    if action not in FIELDS or set(args) != FIELDS[action]:
        raise ToolError('动作参数不匹配：add(title)、list()、complete(id)、delete(id)')
    items = deepcopy(context.state.get('todos', []))
    if action == 'list':
        return items
    if action == 'add':
        title = args['title'].strip()
        if not title:
            raise ToolError('待办标题不能为空白')
        item = dict(id=uuid.uuid4().hex[:12], title=title, done=False)
        items.append(item)
    else:
        item = next((item for item in items if item['id'] == args['id']), None)
        if item is None:
            raise ToolError('待办 id 不存在，请先查询当前会话待办')
        if action == 'complete':
            item['done'] = True
        else:
            items.remove(item)
    context.state['todos'] = items
    return deepcopy(item)
