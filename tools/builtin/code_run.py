"""Bounded foreground execution on the local machine, not a security sandbox."""
import codecs
import os
import signal
import subprocess
import sys
import threading
import time
from errors import ToolError

SCHEMA = {
    'name': 'code_run',
    'description': ('在默认temp或指定cwd执行Python或shell，返回stdout/stderr和退出码；非安全沙箱。'
                    '支持写文件、运行脚本；超时或取消会终止进程组。仅前台执行，不用于后台常驻服务。'),
    'parameters': {'type': 'object', 'properties': {
        'script': {'type': 'string', 'minLength': 1, 'maxLength': 30000},
        'type': {'type': 'string', 'enum': ['python', 'shell', 'bash']},
        'cwd': {'type': 'string'}, 'timeout': {'type': 'number', 'minimum': 0.1, 'maximum': 600},
        'max_output_chars': {'type': 'integer', 'minimum': 100, 'maximum': 12000}},
        'required': ['script'], 'additionalProperties': False},
}


def _kill_group(process):
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass


def execute(context, script, type='python', cwd='.', timeout=60, max_output_chars=6000):
    target = context.resolve_path(cwd)
    if context.cancel and context.cancel.is_set():
        return dict(status='cancelled', exit_code=None, stdout='', truncated=False, cwd=str(target))
    command = [sys.executable, '-u', '-c', script] if type == 'python' else ['bash', '-c', script]
    process = None
    try:
        target.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(command, cwd=target, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=os.name == 'posix')
        chunks, size, failures = [], [0], []
        def drain():
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            try:
                while True:
                    block = process.stdout.read1(4096)
                    if not block:
                        break
                    text = decoder.decode(block)
                    remaining = max(0, max_output_chars - size[0])
                    if remaining:
                        chunks.append(text[:remaining])
                    size[0] += len(text)
                text = decoder.decode(b'', final=True)
                chunks.append(text[:max(0, max_output_chars-size[0])])
                size[0] += len(text)
            except OSError as exc:
                failures.append(str(exc))
            finally:
                process.stdout.close()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        start, status = time.monotonic(), None
        while process.poll() is None or reader.is_alive():
            if context.cancel and context.cancel.is_set():
                status = 'cancelled'
            elif time.monotonic() - start >= timeout:
                status = 'timeout'
            if status:
                _kill_group(process)
                break
            time.sleep(0.03)
        process.wait(timeout=2)
        reader.join(timeout=2)
        return dict(status=status or ('success' if process.returncode == 0 and not failures else 'error'),
                    exit_code=process.returncode, stdout=''.join(chunks), truncated=size[0] > max_output_chars,
                    cwd=str(target), elapsed_seconds=round(time.monotonic()-start, 3))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError('代码执行器启动或收尾失败：' + str(exc)) from exc
    finally:
        if process is not None:
            _kill_group(process)
