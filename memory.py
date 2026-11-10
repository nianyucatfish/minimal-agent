"""Maintain the rolling factual summary for a session."""
import re

SUMMARY = re.compile(r'<summary>(.*?)</summary>', re.DOTALL)


def remember(state, role, text):
    note = ' '.join(text.split())[:240]
    if note:
        lines = state['summary'].splitlines() + [f'{role}: {note}']
        while len('\n'.join(lines)) > 1600:
            lines.pop(0)
        state['summary'] = '\n'.join(lines)


def extract_summary(response, state):
    """Keep a public factual note, not hidden reasoning; remove it from chat output."""
    content = response.get('content') or ''
    notes = SUMMARY.findall(content)
    response['content'] = SUMMARY.sub('', content).strip()
    note = '；'.join(notes) if notes else response['content']
    remember(state, 'Agent' if notes else 'Agent摘录', note)
