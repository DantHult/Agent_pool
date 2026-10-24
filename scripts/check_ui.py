"""Check static UI identifiers and JavaScript syntax without installing packages.

Uses Node when available, otherwise macOS JavaScriptCore. This is not browser QA.
"""
import ctypes
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


class Identifiers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        self.values.extend(value for name, value in attrs if name == 'id')


def check_javascript_core(source):
    jsc = ctypes.CDLL('/System/Library/Frameworks/JavaScriptCore.framework/JavaScriptCore')
    pointer = ctypes.c_void_p
    jsc.JSGlobalContextCreate.argtypes = [pointer]
    jsc.JSGlobalContextCreate.restype = pointer
    jsc.JSStringCreateWithUTF8CString.argtypes = [ctypes.c_char_p]
    jsc.JSStringCreateWithUTF8CString.restype = pointer
    jsc.JSCheckScriptSyntax.argtypes = [pointer, pointer, pointer, ctypes.c_int, ctypes.POINTER(pointer)]
    jsc.JSCheckScriptSyntax.restype = ctypes.c_bool
    jsc.JSStringRelease.argtypes = [pointer]
    jsc.JSGlobalContextRelease.argtypes = [pointer]
    context = jsc.JSGlobalContextCreate(None)
    script = jsc.JSStringCreateWithUTF8CString(source)
    exception = pointer()
    try:
        return jsc.JSCheckScriptSyntax(context, script, None, 1, ctypes.byref(exception))
    finally:
        jsc.JSStringRelease(script)
        jsc.JSGlobalContextRelease(context)


def main():
    script = ROOT / 'static/app.js'
    markup = Identifiers()
    markup.feed((ROOT / 'static/index.html').read_text())
    if len(markup.values) != len(set(markup.values)):
        raise SystemExit('FAIL: duplicate static HTML IDs')
    referenced = set(re.findall(r"\$\('([^']+)'\)", script.read_text()))
    # fileContent is created by the artifact viewer rather than static HTML.
    missing = referenced - set(markup.values) - {'fileContent'}
    if missing:
        raise SystemExit('FAIL: missing HTML IDs: ' + ', '.join(sorted(missing)))
    print('PASS: HTML/JavaScript ID consistency')
    node = shutil.which('node')
    if node:
        subprocess.run([node, '--check', str(script)], check=True)
        print('PASS: JavaScript syntax (Node)')
    elif sys.platform == 'darwin':
        if not check_javascript_core(script.read_bytes()):
            raise SystemExit('FAIL: JavaScript syntax (JavaScriptCore)')
        print('PASS: JavaScript syntax (JavaScriptCore)')
    else:
        print('SKIP: JavaScript syntax; Node or macOS JavaScriptCore is required')


if __name__ == '__main__':
    main()
