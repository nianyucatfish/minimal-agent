"""Manage one session-scoped execution plan, without a plan archive."""
from copy import deepcopy
import uuid
from errors import ToolError

SCHEMA = {
    'name': 'plan',
    'description': ('管理当前执行计划，不是用户待办。create(title,steps)、get()、update(plan_id,step_id,status)、cancel(plan_id)。'
                    '未完成计划不能覆盖；全部步骤完成后可创建下一计划。更新/取消必须使用当前 plan_id。'),
    'parameters': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['create', 'get', 'update', 'cancel']},
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 120},
            'steps': {'type': 'array', 'minItems': 1, 'maxItems': 10,
                      'items': {'type': 'string', 'minLength': 1, 'maxLength': 120}},
            'plan_id': {'type': 'string', 'minLength': 1},
            'step_id': {'type': 'string', 'minLength': 1},
            'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']},
        },
        'required': ['action'],
        'additionalProperties': False,
    },
}
FIELDS = {'create': {'title', 'steps'}, 'get': set(),
          'update': {'plan_id', 'step_id', 'status'}, 'cancel': {'plan_id'}}


def execute(context, action, **args):
    if action not in FIELDS or set(args) != FIELDS[action]:
        raise ToolError('动作参数不匹配：create(title, steps)、get()、update(plan_id, step_id, status)、cancel(plan_id)')
    plan = deepcopy(context.state.get('current_plan'))
    if action == 'get':
        return {'plan': plan}
    if action == 'create':
        if plan and plan['status'] == 'active':
            raise ToolError('当前计划未完成；继续执行，或根据用户意图明确取消后再创建')
        title, steps = args['title'].strip(), [step.strip() for step in args['steps']]
        if not title or not all(steps):
            raise ToolError('计划和步骤标题不能仅包含空白')
        plan = dict(id='plan_' + uuid.uuid4().hex[:12], title=title, status='active',
                    steps=[dict(id='step_' + uuid.uuid4().hex[:12], title=step, status='pending')
                           for step in steps])
    else:
        if not plan or args['plan_id'] != plan['id']:
            raise ToolError('plan_id 已过期或当前没有计划，请重新查询')
        if action == 'cancel':
            if plan['status'] == 'completed':
                raise ToolError('已完成计划不能取消，可以创建新计划')
            plan['status'] = 'cancelled'
        else:
            step = next((s for s in plan['steps'] if s['id'] == args['step_id']), None)
            if step is None:
                raise ToolError('step_id 不属于当前计划')
            if plan['status'] == 'cancelled':
                raise ToolError('计划已取消，不能更新')
            if step['status'] == 'completed' and args['status'] != 'completed':
                raise ToolError('已完成步骤不能回退，需要重做时创建新计划')
            if args['status'] == 'in_progress' and any(
                    s['id'] != step['id'] and s['status'] == 'in_progress' for s in plan['steps']):
                raise ToolError('已有其他步骤正在执行，请先处理该步骤')
            step['status'] = args['status']
            plan['status'] = 'completed' if all(s['status'] == 'completed' for s in plan['steps']) else 'active'
    # Validate first, then replace the snapshot once; persistence is owned by SessionTurn.
    context.state['current_plan'] = plan
    return {'plan': deepcopy(plan)}
