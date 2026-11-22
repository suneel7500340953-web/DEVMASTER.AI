# apps/api: runs module (language registry + sandbox)

Run the tests (Python 3.10+, no extra packages, no Docker needed):

    cd apps/api && python -m unittest discover -s tests -t . -v

`runs/languages.py` = what each language needs; `runs/sandbox.py` = hardened `docker run` builder.
`run_sandboxed()` needs Docker and is only covered by mocked tests so far.

# apps/api: ai module (AI developer agent)

Works from a terminal (also Termux), stdlib only, no `pip install` needed.

    export GROQ_API_KEY=...        # or OPENAI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY (Ollama needs no key)
    python -m app.modules.ai.cli --providers
    python -m app.modules.ai.cli --provider groq --list-models
    python -m app.modules.ai.cli -p ./myproject --provider groq --model <id> --task bugfix "login crashes on empty password"

Tasks: generate, project, edit, bugfix, refactor, tests, docs, api, schema, ui, optimize (these propose file changes)
and explain, security, architecture, git_summary (these return a report only).

Flow: inspect -> plan (asks questions if unclear) -> generate -> run tests on the PROPOSED files -> fix (max 2 rounds)
-> show diff + token usage + cost -> you type `yes` -> applied with backup. Undo: `--revert <backup id>`.

- Tests: `--tests auto|docker|local-unsafe|off`. `docker` is isolated. `local-unsafe` runs on your own machine without isolation.
- Cost: add `--prices prices.json` with `{"model-id": [usd_per_1M_input, usd_per_1M_output]}`. Without it the cost shows as unknown.
- `.env`, keys, `.git`, binaries and `node_modules` are never read or sent to the AI, and the AI cannot change them.

Run all tests: `python -m unittest discover -s tests -t . -v`
