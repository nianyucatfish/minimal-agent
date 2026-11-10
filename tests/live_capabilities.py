"""Explicit real-model code/browser smoke test against a user-selected public tab."""
import argparse
import json
from pathlib import Path
import tempfile
import time
from client import LLMClient
from errors import AgentError
from runtime import Runtime
from session import SessionStore
from tools import Tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tab-id', type=int, required=True)
    parser.add_argument('--expected-url', required=True)
    args = parser.parse_args()
    client = LLMClient()
    seen = []
    original = client.chat
    def recorded(messages, tools, **kwargs):
        response = original(messages, tools, **kwargs)
        seen.extend(c['function']['name'] for c in response.get('tool_calls', []))
        return response
    client.chat = recorded
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as root:
        store = SessionStore(Path(root)/'data')
        registry = Tools(Path(root)/'work')
        runtime = Runtime(client, store, registry)
        prompt = (f'做一次明确授权的本地代码执行与浏览器只读连通测试，不需要创建计划：'
                  f'1. 用code_run运行Python计算(128+32)*3，写入当前工作目录的result.txt并打印；'
                  f'2. 用file_read读取result.txt，再用file_write写note.txt，内容为verified；'
                  f'3. 用web_scan读取tab_id={args.tab_id}页面，text_only=true,max_chars=600；'
                  f'4. 用web_execute_js对同一个tab_id读取document.title和location.href，'
                  f'expected_url={args.expected_url}，使用return返回对象。'
                  '不要点击、导航、读取Cookie/密码或其他标签页。最后报告实际计算结果和页面标题。')
        result = runtime.run('capabilities', prompt)
        state = store.load(store.key('capabilities'))
        output = registry.workdir/'result.txt'
        checks = dict(all_tools_called=all(n in seen for n in ('code_run','file_read','file_write','web_scan','web_execute_js')),
                      file_written=output.exists() and output.read_text().strip()=='480',
                      note_written=(registry.workdir/'note.txt').read_text()=='verified',
                      run_completed=state['last_run']['status']=='completed',
                      answer_contains_result='480' in result)
        report = dict(model=client.model, tools=seen, checks=checks, answer=result,
                      elapsed_seconds=round(time.monotonic()-start,2), public_url=args.expected_url)
    Path('reports').mkdir(exist_ok=True)
    Path('reports/live_capabilities.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except AgentError as exc:
        print(f'Live capability test failed [{exc.code}]: {exc}')
        raise SystemExit(1)
