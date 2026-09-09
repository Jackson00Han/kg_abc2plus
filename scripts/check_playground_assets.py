#!/usr/bin/env python3
"""Syntax-check each inline script separately and all first-party UI modules."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]/'src/graphrag_prod/playground/static'

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__();self.active=False;self.scripts=[]
    def handle_starttag(self,tag,attrs):
        if tag=='script' and not dict(attrs).get('src'):
            self.active=True;self.scripts.append('')
    def handle_data(self,data):
        if self.active:self.scripts[-1]+=data
    def handle_endtag(self,tag):
        if tag=='script':self.active=False

if __name__=='__main__':
    count=0
    with tempfile.TemporaryDirectory(prefix='graphrag-script-check-') as folder:
        for page in ROOT.rglob('*.html'):
            parser=Scripts();parser.feed(page.read_text())
            for source in parser.scripts:
                path=Path(folder)/f'script-{count}.js';path.write_text(source)
                subprocess.run(['node','--check',str(path)],check=True);count+=1
        for path in ROOT.rglob('*.mjs'):
            if 'vendor' not in path.parts:
                subprocess.run(['node','--check',str(path)],check=True);count+=1
    print(f'JavaScript syntax passed: {count} inline scripts and first-party modules')
