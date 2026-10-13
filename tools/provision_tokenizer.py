"""Explicit one-time network setup, never invoked by compilation."""
import argparse
import urllib.request
from cch.core import tokenizer_asset,digest
from cch.cli import atomic_write
import os
import tempfile

p=argparse.ArgumentParser()
p.add_argument('--encoding',choices=['cl100k_base','o200k_base'],default='cl100k_base')
a=p.parse_args()
url,path,expected=tokenizer_asset(a.encoding)
if path.is_file() and digest(path.read_bytes())==expected:
    print('Tokenizer cache is ready.')
else:
    with urllib.request.urlopen(url,timeout=30) as response:
        data=response.read()
    if digest(data)!=expected:
        raise SystemExit('Tokenizer hash mismatch; no cache written.')
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    print('Tokenizer cache is ready.')
