"""CLI entry point and dependency composition."""
import argparse
import logging
from client import LLMClient
from errors import AgentError
from cli import SessionCommands, chat_loop
from runtime import DEFAULT_MAX_STEPS, Runtime
from session import SessionStore
from tools import Tools


def main():
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='本地 JSON API 配置文件路径')
    parser.add_argument('--session', default='default')
    parser.add_argument('--data', default='data')
    parser.add_argument('--workdir', '--workspace', '--docs', dest='workdir', default='temp')
    parser.add_argument('--browser-url', default='http://127.0.0.1:18766/link')
    parser.add_argument('--max-steps', type=int, default=DEFAULT_MAX_STEPS)
    args = parser.parse_args()
    try:
        store = SessionStore(args.data)
        commands = SessionCommands(store, args.session)
        def ask_user(question, options, cancel):
            print('Agent 提问：' + question)
            for index, option in enumerate(options, 1):
                print(f'  {index}. {option}')
            answer = input('你的回答（可输入选项序号或文字）> ').strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]
            return answer
        def runtime_factory():
            return Runtime(LLMClient(args.config), store, Tools(args.workdir, args.browser_url, ask_user=ask_user, store=store), args.max_steps)
        chat_loop(commands, runtime_factory)
    except (AgentError, ValueError, OSError) as exc:
        print('启动失败：', exc)


if __name__ == '__main__':
    main()
