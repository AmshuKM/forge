You build one small app for Forge. Follow the output format exactly.

The owner asked for this app, named {{name}}:
<request>
{{idea}}
</request>

How Forge apps work:
<guide>
{{guide}}
</guide>

{{examples}}

Files so far:
{{files}}

{{failure}}

Write these three files: apps/{{name}}/app.py, apps/{{name}}/tests/test_app.py and apps/{{name}}/evals/golden.jsonl
(at least 5 cases). Use only the Python standard library. Never use eval or exec.

Output format, with nothing before or after it:
CAUSE: one sentence describing the app you built
FILE: path/of/file
<<<
the complete content of that file
>>>
Repeat the FILE block for each file.
