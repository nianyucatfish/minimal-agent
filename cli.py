"""Local slash commands and terminal conversation; no model calls for commands."""
from dataclasses import dataclass
import logging
import shlex
import uuid
from errors import AgentError

logger = logging.getLogger('agent')
HELP = '''/new [名称]      新建并切换会话；省略名称自动生成
/switch 名称      切换到已有会话，恢复历史、todo 和 plan
/sessions         列出当前用户的会话（* 表示当前）
/session          显示当前用户与会话
/help             显示帮助
/exit             退出；也支持 /quit
以 // 开头可以把 / 开头的文字作为普通消息发送。'''


@dataclass
class CommandResult:
    message: str
    exit: bool = False


class SessionCommands:
    def __init__(self, store, session='default'):
        store.open_session(session)
        self.store, self.session = store, session

    def execute(self, text):
        if not text.startswith('/') or text.startswith('//'):
            return None
        parts = shlex.split(text)
        command, args = parts[0], parts[1:]
        if command == '/new' and len(args) <= 1:
            name = args[0] if args else 'session-' + uuid.uuid4().hex[:8]
            self.store.open_session(name, mode='create')
            self.session = name
            return CommandResult(f'已新建并切换到会话：{name}')
        if command == '/switch' and len(args) == 1:
            self.store.open_session(args[0], mode='resume')
            self.session = args[0]
            return CommandResult(f'已切换到会话：{self.session}')
        if args:
            raise ValueError('命令参数不正确，输入 /help 查看用法；含空格名称请使用引号')
        if command == '/sessions':
            rows = self.store.list_sessions()
            return CommandResult('\n'.join(
                f'{"*" if row["name"] == self.session else " "} {row["name"]}  ({row["turns"]}轮)' for row in rows)
                or '暂无会话')
        if command == '/session':
            return CommandResult(f'当前会话：{self.session}')
        if command == '/help':
            return CommandResult(HELP)
        if command in ('/exit', '/quit'):
            return CommandResult('已退出，已保存的会话下次可续接。', exit=True)
        raise ValueError('未知命令或参数缺失，输入 /help 查看用法')


def chat_loop(commands, runtime_factory, read=input, write=print):
    """Initialize the model lazily so session commands also work without API config."""
    runtime = None
    write(f'会话 {commands.session}；输入 /help 查看命令')
    while True:
        try:
            text = read(f'你[{commands.session}]> ').strip()
            if not text:
                continue
            result = commands.execute(text)
            if result is not None:
                write(result.message)
                if result.exit:
                    return
                continue
            if text.startswith('//'):
                text = text[1:]
            if runtime is None:
                runtime = runtime_factory()
            write('Agent> ' + runtime.run(commands.session, text))
        except AgentError as exc:
            write(f'执行失败 [{exc.code}]: {exc}')
        except ValueError as exc:
            write('错误：' + str(exc))
        except (EOFError, KeyboardInterrupt):
            write('已退出，已保存的会话下次可续接。')
            return
        except Exception:
            logger.exception('CLI 未知异常')
            write('内部错误，详情见错误日志')
