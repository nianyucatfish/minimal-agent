"""Shared temp semantics and ordinary local read/write permissions."""
import json
from pathlib import Path
import tempfile
import unittest
from tools import Tools


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = Tools(self.root/'temp', modules=('file_read','file_write','code_run'))

    def call(self, tool, **args):
        return self.tools.dispatch(tool, json.dumps(args), {})

    def test_default_is_temp(self):
        self.assertEqual(Tools().workdir, Path('temp').resolve())

    def test_write_read_relative_and_empty(self):
        self.assertTrue(self.call('file_write', path='a/b.txt', content='hello').to_dict()['ok'])
        self.assertEqual(self.call('file_read',path='a/b.txt').data['text'], 'hello')
        self.call('file_write',path='a/b.txt',content='')
        self.assertEqual(self.call('file_read',path='a/b.txt').data['text'], '')

    def test_append_prepend(self):
        self.call('file_write',path='file.txt',content='B')
        self.call('file_write',path='file.txt',content='C',mode='append')
        self.call('file_write',path='file.txt',content='A',mode='prepend')
        self.assertEqual(self.call('file_read',path='file.txt').data['text'], 'ABC')

    def test_absolute_and_parent_paths(self):
        outside = self.root/'outside.txt'
        self.call('file_write',path=str(outside),content='outside')
        self.assertEqual(self.call('file_read',path='../outside.txt').data['text'], 'outside')
        self.assertEqual(self.call('file_read',path=str(outside)).data['text'], 'outside')

    def test_code_and_file_tools_use_same_base(self):
        self.call('code_run',script="from pathlib import Path; Path('result.txt').write_text('480')")
        self.assertEqual(self.call('file_read',path='result.txt').data['text'], '480')
        self.call('file_write',path='input.txt',content='value')
        self.assertEqual(self.call('code_run',script="from pathlib import Path; print(Path('input.txt').read_text())").data['stdout'],'value\n')

    def test_line_window_keyword_and_truncation(self):
        self.call('file_write',path='lines.txt',content=''.join(f'line{i}\n' for i in range(1,21)))
        result = self.call('file_read',path='lines.txt',start=3,count=2,show_linenos=True).data
        self.assertEqual(result['text'],'3|line3\n4|line4\n')
        self.assertTrue(result['truncated'])
        self.assertIn('line15',self.call('file_read',path='lines.txt',keyword='line15',count=3).data['text'])
        self.assertIn('notice',self.call('file_read',path='lines.txt',keyword='absent').data)

    def test_missing_file_and_write_error(self):
        self.assertFalse(self.call('file_read',path='missing.txt').to_dict()['ok'])
        self.call('file_write',path='a',content='file')
        self.assertFalse(self.call('file_write',path='a/b',content='x').to_dict()['ok'])
