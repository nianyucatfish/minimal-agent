"""Find prior questions in the local session archive."""
from errors import StorageError, ToolError

SCHEMA = {
    'name': 'recall_history',
    'description': ('只读检索本地历史会话。用户提到以前问过的问题、过去的结论，或需要找回上下文里缺失的旧细节时使用。'
                    '用空格分隔关键词或短语，未命中可换词。返回旧问题、回答片段、会话、更新时间和日志路径。'),
    'parameters': {
        'type': 'object',
        'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
        'required': ['query'],
        'additionalProperties': False,
    },
}


def execute(context, query, *, store):
    terms = set(query.casefold().split())
    if not terms:
        raise ToolError('请输入历史检索关键词')
    hits = []
    for session in store.list_sessions():
        key = store.key(session['name'])
        try:
            state = store.load(key)
        except StorageError:
            continue
        for index, turn in enumerate(state['turns'], start=1):
            if not isinstance(turn, list) or not turn or not isinstance(turn[0], dict):
                continue
            question = turn[0].get('content')
            if turn[0].get('role') != 'user' or not isinstance(question, str):
                continue
            last = turn[-1]
            if not isinstance(last, dict) or last.get('role') != 'assistant' or last.get('tool_calls'):
                continue
            answer = last.get('content')
            if not isinstance(answer, str) or not answer.strip():
                continue
            question_text, answer_text = question.casefold(), answer.casefold()
            question_matches = sum(term in question_text for term in terms)
            answer_matches = sum(term in answer_text for term in terms)
            if question_matches or answer_matches:
                rank = (question_matches, answer_matches, session['updated_at'], index)
                hits.append((rank, dict(
                    question=question[:400], answer_excerpt=answer[:800],
                    session=session['name'], session_updated_at=session['updated_at'],
                    turn=index, log_path=str((store.root / (key + '.json')).resolve()),
                )))
    hits.sort(key=lambda hit: hit[0], reverse=True)
    return {'matches': [item for _, item in hits[:5]]}
