"""Scripted model and protocol builders shared by offline tests."""
import json


def call(name, args, id='c1'):
    return dict(role='assistant', content='', tool_calls=[dict(id=id, type='function',
                function=dict(name=name, arguments=json.dumps(args)))])


def answer(text='完成'):
    return dict(role='assistant', content=text)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, messages, tools, *, cancel=None):
        self.requests.append(json.loads(json.dumps(messages)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
