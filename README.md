# apps/api: runs module (language registry + sandbox)

Run the tests (Python 3.10+, no extra packages, no Docker needed):

    cd apps/api && python -m unittest discover -s tests -t . -v

`runs/languages.py` = what each language needs; `runs/sandbox.py` = hardened `docker run` builder.
`run_sandboxed()` needs Docker and is only covered by mocked tests so far.
