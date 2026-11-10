"""Explicit real-API acceptance for user to-dos and execution plans; uses isolated synthetic sessions."""
import json
from pathlib import Path
import tempfile
import time
from client import LLMClient
from runtime import Runtime
from session import SessionStore
from tools import Tools
from errors import AgentError


def main():
    client = LLMClient()
    sizes, tails, seen_tools, requests = [], [], [], []
    original = client.chat
    def recorded(messages, tools, **kwargs):
        requests.append(messages)
        sizes.append(len(json.dumps(dict(messages=messages, tools=tools), ensure_ascii=False)))
        tails.append(all(messages[-1]['content'].count(tag) == 1 for tag in ('<history>', '<plan>')))
        result = original(messages, tools, **kwargs)
        seen_tools.extend(call['function']['name'] for call in result.get('tool_calls', []))
        return result
    client.chat = recorded
    checks, answers = {}, []
    started = time.monotonic()
    budget = 9000
    with tempfile.TemporaryDirectory() as root:
        data = Path(root) / 'data'
        store = SessionStore(data)
        tools = Tools(Path(__file__).resolve().parents[1] / 'docs', modules=('calculator','file_read','todo','plan','checkpoint'))
        runtime = Runtime(client, store, tools, context_chars=budget)
        def run(prompt, session='a'):
            result = runtime.run(session, prompt)
            answers.append(result)
            print(json.dumps({'turn': len(answers), 'answer': result}, ensure_ascii=False), flush=True)
            return store.load(store.key(session))
        state = run('我叫测试小鱼。禁止修改数据库，请写入 checkpoint。用 plan 创建“检查项目”计划，恰好两步：“计算表达式”“读取示例资料”。本轮仅创建计划，不开始执行。')
        plan = state['current_plan']
        checks['plan_created_pending'] = bool(plan) and plan['status']=='active' and all(s['status']=='pending' for s in plan['steps'])
        if not checks['plan_created_pending']:
            raise RuntimeError('模型未创建预期计划')
        first_id = plan['id']
        state = run('给我新增一条用户待办“明天提交报告”，保持未完成，不要创建或更新执行计划。')
        checks['user_todo_added_without_plan_change'] = len(state['todos'])==1 and not state['todos'][0]['done'] and state['current_plan']['id']==first_id

        store = SessionStore(data)  # Reconnect with a fresh owner; do not share in-memory state.
        runtime = Runtime(client, store, tools, context_chars=budget)
        state = run('续接上次计划。先用 plan.get 查询；执行第一步，用 calculator 计算 (128+32)*3，验证后把第一步更新为 completed。第二步保持 pending，本轮不要执行。')
        plan = state['current_plan']
        checks['reconnect_and_first_step'] = plan['id']==first_id and [s['status'] for s in plan['steps']]==['completed','pending']
        state = run('继续第二步：读取 intro.txt，概括内容，成功后用 plan 将第二步标为 completed。')
        checks['plan_auto_completed'] = state['current_plan']['status']=='completed'
        checks['plan_completion_does_not_complete_todo'] = len(state['todos'])==1 and not state['todos'][0]['done']
        state = run('我叫什么？先前计算结果是多少？数据库方面有什么限制？当前计划完成了吗？请简短回答。')
        checks['followup_recalled_name_result_constraint'] = all(s in answers[-1] for s in ['测试小鱼','480','数据库'])
        state = run('请用 todo 查询用户待办，再把“明天提交报告”标记完成。不要改变执行计划。')
        checks['user_todo_completed_independently'] = state['todos'][0]['done'] and state['current_plan']['id']==first_id and state['current_plan']['status']=='completed'
        state = run('另一个新任务：请创建“下一阶段”计划，只有一步“等待我提供新要求”。本轮只创建，不执行该步骤。新任务仍禁止修改数据库，清理旧进度但保留有效约束。')
        checks['completed_plan_replaced'] = state['current_plan']['id'] != first_id and state['current_plan']['status']=='active'
        checks['no_plan_history'] = 'plan_history' not in state
        checks['tail_uses_new_plan'] = first_id not in requests[-1][-1]['content'].split('<plan>')[1]
        checks['both_tools_called'] = all(t in seen_tools for t in ('todo', 'plan'))
        checks['calculator_called'] = 'calculator' in seen_tools
        checks['file_read_called'] = 'file_read' in seen_tools
        checks['checkpoint_called_and_saved'] = 'update_working_checkpoint' in seen_tools and bool(state['checkpoint'])
        checks['summary_generated'] = 'Agent:' in state['summary']
        checks['summary_hidden'] = all('<summary>' not in item for item in answers)
        checks['tail_singleton'] = all(tails)
        checks['memory_not_in_chat'] = '【工作记忆】' not in json.dumps(state['turns'])
        checks['old_turns_pruned'] = len(state['turns']) < 7
        isolated = run('分别用 todo.list 和 plan.get 查询当前会话用户待办和执行计划，不新增。', session='b')
        checks['session_isolation'] = isolated['current_plan'] is None and not isolated['todos'] and not isolated['checkpoint']
        checks['budget_respected'] = max(sizes) <= budget
        report = dict(scenario='todo-and-plan-v1', model=client.model, base_url=client.base,
                      model_calls=len(sizes), context_chars=budget,
                      elapsed_seconds=round(time.monotonic()-started, 2), checks=checks, answers=answers)
    output = Path(__file__).resolve().parents[1] / 'reports' / 'live_test.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': checks, 'model_calls': len(sizes)}, ensure_ascii=False), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except AgentError as exc:
        print(f'Live test failed [{exc.code}]: {exc}')
        raise SystemExit(1)
