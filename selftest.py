#!/usr/bin/env python3
"""Exercise every task the server advertises, WITHOUT paying.

We cannot pay (the wallet is empty), so we test the task functions directly by
importing the module. A task that crashes or returns nothing is a real defect —
not a marketing claim.
"""
import sys, json, traceback
sys.path.insert(0, '/home/redman8326/microsvc')
import hermes_server as hs

CASES = [
    ("summarize", "The quick brown fox jumps over the lazy dog. " * 20),
    ("extract", "Contact John Smith at john@example.com or +1-555-0100. Meet on 2026-10-19."),
    ("keywords", "Machine learning and neural networks power modern search engines."),
    ("sentiment", "I absolutely love this product, it is wonderful and fast."),
    ("translate", "Hello world, this is a test."),
    ("rewrite", "this is bad writing it is unclear and confusing"),
    ("proofread", "This sentance has a error in it and also is to long."),
    ("python_debug", "def add(a, b)\n    return a + b"),
    ("code_lint", "import os\nimport sys\n\nx = 1\nprint(x)"),
    ("code_refactor", "def f(x):\n    if x == True:\n        return True\n    else:\n        return False"),
    ("code_explain", "def fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)"),
    ("code_review", "def get_user(id):\n    return db.execute('SELECT * FROM users WHERE id = ' + id)"),
    ("json_minify", '{"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}'),
    ("json_query", '{"users": [{"name": "ann", "age": 30}, {"name": "bob", "age": 40}]}'),
    ("csv_to_json", "name,age\nann,30\nbob,40"),
    ("hash", "hello world"),
    ("base64", "hello world"),
    ("convert", "100 USD to EUR"),
    ("describe_image", "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_92x30dp.png"),
    ("probe_media", "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"),
]

ok = fail = 0
for task, inp in CASES:
    try:
        out = hs.do_task(task, inp, prompt="")
        if out is None or (isinstance(out, str) and not out.strip()):
            print(f"  {task:18s} EMPTY  <-- problem")
            fail += 1
        else:
            s = str(out).replace("\n", " ")[:70]
            print(f"  {task:18s} OK     {s}")
            ok += 1
    except Exception as e:
        print(f"  {task:18s} CRASH  {type(e).__name__}: {str(e)[:60]}")
        fail += 1

print()
print(f"working: {ok}/{len(CASES)}   failed: {fail}")
