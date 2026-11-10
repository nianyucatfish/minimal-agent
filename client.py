"""Model transport, protocol validation and bounded retry."""
import json
import logging
import math
import os
from pathlib import Path
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
from errors import LLMError, RunCancelled

logger = logging.getLogger('agent')


class LLMClient:
    """OpenAI-compatible Chat Completions; session history belongs to SessionStore."""
    def __init__(self, config_path=None):
        path = Path(config_path) if config_path else Path(__file__).with_name('api_config.local.json')
        config = {}
        if config_path or path.exists():
            try:
                config = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(config, dict):
                    raise ValueError('配置必须为对象')
            except (OSError, ValueError) as exc:
                raise LLMError('API 配置文件无法读取或 JSON 格式错误') from exc
        self.key = os.environ.get('LLM_API_KEY', config.get('api_key', ''))
        self.base = os.environ.get('LLM_BASE_URL', config.get('base_url', ''))
        self.model = os.environ.get('LLM_MODEL', config.get('model', ''))
        if not all(isinstance(v, str) and v.strip() for v in (self.key, self.base, self.model)):
            raise LLMError('请填写本地 API 配置，或设置 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL')
        self.base = self.base.rstrip('/')
        self.proxy_mode = os.environ.get('LLM_PROXY_MODE', config.get('proxy_mode', 'system'))
        if self.proxy_mode not in ('system', 'direct'):
            raise LLMError('proxy_mode 必须为 system 或 direct')
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({})) if self.proxy_mode == 'direct' else None
        self.timeout = config.get('timeout', 30)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 300:
            raise LLMError('timeout 必须为1到300秒')
        deepseek = 'deepseek' in self.model.lower()
        self.context_chars = config.get('context_chars', 240000 if deepseek else 105000)
        self.trim_keep_rate = config.get('trim_keep_rate', 0.3 if deepseek else 0.6)
        if type(self.context_chars) is not int or self.context_chars < 4000:
            raise LLMError('context_chars 必须为至少4000的整数（字符预算）')
        if (type(self.trim_keep_rate) not in (int, float) or
            not math.isfinite(self.trim_keep_rate) or not 0 < self.trim_keep_rate < 1):
            raise LLMError('trim_keep_rate 必须大于0且小于1')
        self.options = config.get('request_options', {})
        if (not isinstance(self.options, dict) or
            set(self.options) - {'thinking', 'temperature', 'max_tokens'}):
            raise LLMError('request_options 仅允许 thinking、temperature、max_tokens')

    @staticmethod
    def _network_reason(error):
        cause = getattr(error, 'reason', error)
        if isinstance(cause, ssl.SSLCertVerificationError):
            return 'TLS证书验证失败'
        if isinstance(cause, (TimeoutError, socket.timeout)):
            return '连接或读取超时'
        if isinstance(cause, socket.gaierror):
            return 'DNS解析失败'
        if isinstance(cause, ConnectionRefusedError):
            return '连接被拒绝'
        if isinstance(cause, ConnectionResetError):
            return '连接被重置'
        return '网络错误（' + type(cause).__name__ + '）'

    def chat(self, messages, tools, *, cancel=None):
        payload = json.dumps(dict(model=self.model, messages=messages,
                                  tools=tools, tool_choice='auto', stream=False, **self.options)).encode()
        req = urllib.request.Request(self.base + '/chat/completions', data=payload,
              headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        for attempt in range(3):
            if cancel and cancel.is_set():
                raise RunCancelled("模型请求已取消")
            retry_after = None
            reason = ""
            try:
                open_request = self.http.open if self.http is not None else urllib.request.urlopen
                with open_request(req, timeout=self.timeout) as response:
                    data = json.load(response)
                choice = data['choices'][0]
                if choice.get('finish_reason') in ('length', 'content_filter'):
                    raise LLMError('模型输出被截断或过滤，请缩小任务后重试')
                raw = choice['message']
                if not isinstance(raw, dict):
                    raise LLMError('模型消息不是对象')
                result = {'role': 'assistant', 'content': raw.get('content') or ''}
                calls = raw.get('tool_calls') or []
                if not isinstance(result['content'], str) or not isinstance(calls, list):
                    raise LLMError('模型正文或工具列表类型错误')
                ids = set()
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                        raise LLMError('模型工具调用不是合法对象')
                    if (not isinstance(call.get('id'), str) or not call['id'] or
                        call['id'] in ids or call.get('type') != 'function' or
                        not isinstance(call.get('function', {}).get('name'), str) or
                        not isinstance(call['function'].get('arguments'), str)):
                        raise LLMError('模型返回不合法的工具调用协议')
                    ids.add(call['id'])
                if calls:
                    result['tool_calls'] = calls
                if not calls and not result['content'].strip():
                    raise LLMError('模型返回空回复')
                # Parse provider reasoning metadata, but do not persist/display hidden reasoning.
                self.has_reasoning = bool(raw.get('reasoning_content') or raw.get('reasoning'))
                return result
            except urllib.error.HTTPError as exc:
                reason = f'HTTP {exc.code}'
                try:
                    retry_after = float(exc.headers.get('Retry-After', ''))
                except (TypeError, ValueError, AttributeError):
                    pass
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise LLMError(f'模型请求失败：{reason}（网络模式：{self.proxy_mode}）') from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                reason = self._network_reason(exc)
                cause = getattr(exc, 'reason', exc)
                if isinstance(cause, ssl.SSLCertVerificationError) or attempt == 2:
                    raise LLMError(f'模型请求失败：{reason}（网络模式：{self.proxy_mode}，超时设置：{self.timeout}秒）') from exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise LLMError('无法解析 LLM 响应') from exc
            delay = retry_after if retry_after is not None else random.uniform(0, 2 ** attempt)
            if not math.isfinite(delay) or delay > 30:
                raise LLMError('服务商要求等待过久，请稍后重试')
            delay = max(0, delay)
            logger.warning('模型请求失败：%s；网络模式=%s；%.2f秒后开始第%s/3次尝试',
                           reason, self.proxy_mode, delay, attempt + 2)
            if cancel:
                if cancel.wait(delay):
                    raise RunCancelled('退避等待已取消')
            else:
                time.sleep(delay)
