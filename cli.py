"""DevMaster AI developer agent (command line). Stdlib only, so it also works in Termux.

Examples:
  export GROQ_API_KEY=...            # or OPENAI_API_KEY / ANTHROPIC_API_KEY / OPENROUTER_API_KEY
  python -m app.modules.ai.cli --providers
  python -m app.modules.ai.cli --provider groq --list-models
  python -m app.modules.ai.cli -p ./myproj --provider groq --model <id> --task bugfix "login crashes on empty password"
  python -m app.modules.ai.cli -p ./myproj --revert 20260101-120000-ab12cd34
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import prompts
from .agent import Agent
from .gitutil import git_context
from .pricing import Pricing
from .providers import LLMError, build_registry
from .store import Store
from .testrunner import DockerTestRunner, LocalTestRunner
from .workspace import ConflictError, revert
from ..runs.languages import docker_available


def main(argv=None, registry=None, runner=None, input_fn=input, out=print) -> int:
    ap = argparse.ArgumentParser(prog="devmaster-agent")
    ap.add_argument("request", nargs="*", help="what you want, in plain language")
    ap.add_argument("-p", "--project", default=".")
    ap.add_argument("--task", choices=sorted(prompts.TASKS), default="edit")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--providers", action="store_true", help="list configured providers")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--tests", choices=["auto", "docker", "local-unsafe", "off"], default="auto")
    ap.add_argument("--prices", help="JSON file: {model: [usd_per_M_in, usd_per_M_out]}")
    ap.add_argument("--config", help="providers JSON config")
    ap.add_argument("--fix-rounds", type=int, default=2)
    ap.add_argument("--token-budget", type=int, default=400_000)
    ap.add_argument("--resume", help="conversation id")
    ap.add_argument("--revert", metavar="BACKUP_ID")
    a = ap.parse_args(argv)
    root = Path(a.project).resolve()

    if a.revert:
        revert(root, a.revert)
        out(f"Reverted backup {a.revert}.")
        return 0
    reg = registry if registry is not None else build_registry(config_path=a.config)
    if a.providers:
        out("Usable providers (API key found): " + (", ".join(sorted(reg)) or "none. Set e.g. GROQ_API_KEY"))
        return 0
    if not a.provider or a.provider not in reg:
        out("Choose --provider from: " + (", ".join(sorted(reg)) or "none (set an API key env var)"))
        return 2
    provider = reg[a.provider]
    if a.list_models:
        try:
            out("\n".join(provider.list_models()))
        except (AttributeError, LLMError) as e:
            out(f"Cannot list models for {a.provider}: {e}")
        return 0
    if not a.model:
        out("Pass --model <id> (use --list-models to see what your account offers).")
        return 2
    if not a.request:
        out("Tell me what to do, e.g.:  ... --task bugfix \"fix the login crash\"")
        return 2

    if runner is None and a.tests != "off":
        if a.tests == "local-unsafe":
            out("WARNING: tests will run on THIS machine with no isolation.")
            runner = LocalTestRunner()
        elif a.tests == "docker" or docker_available():
            runner = DockerTestRunner()
        else:
            out("Docker not found: tests will not be run (use --tests local-unsafe on your own machine to run them).")

    (root / ".devmaster").mkdir(exist_ok=True)
    store = Store(str(root / ".devmaster" / "history.sqlite"))
    agent = Agent(provider, a.model, root, test_runner=runner, pricing=Pricing.load(a.prices), store=store,
                  conversation_id=a.resume, max_fix_rounds=a.fix_rounds, token_budget=a.token_budget,
                  on_event=lambda s, d="": out(f"[{s}] {d}"))
    request = " ".join(a.request)
    extra = git_context(root) if a.task == "git_summary" else ""
    out(f"Provider: {a.provider} | Model: {a.model} | Task: {a.task} | Conversation: {agent.conv}")
    prop = agent.run(a.task, request, extra)

    if prop.questions:
        out("\nThe AI needs answers before it can continue:\n" + "\n".join(f" - {q}" for q in prop.questions))
    if prop.plan.get("steps"):
        out("\nPLAN:\n" + "\n".join(f" {i}. {s}" for i, s in enumerate(prop.plan["steps"], 1)))
    for n in prop.notes:
        out(f"NOTE: {n}")
    if prop.report:
        out("\n" + prop.report)
    if prop.test_result and prop.test_result.ran:
        out(f"\nTESTS ({prop.test_result.command}): {'PASSED' if prop.test_result.passed else 'FAILED'} after {prop.rounds + 1} run(s)")
    out("\nUSAGE:")
    for u in prop.usage:
        c = f"${u.cost_usd:.4f}" if u.cost_usd is not None else "price unknown"
        out(f"  {u.purpose:<10} {u.model}  in={u.tokens_in} out={u.tokens_out}  {c}")
    total = f"${prop.cost_usd:.4f}" if prop.cost_usd is not None else "unknown (add --prices to estimate)"
    out(f"  TOTAL tokens={prop.tokens}  estimated cost={total}")

    if not prop.changeset:
        return 0
    out("\nPROPOSED CHANGES (nothing written yet):\n" + prop.changeset.diff_text())
    out(f"{len(prop.changeset.changes)} file(s): " + ", ".join(f"{c.action} {c.path}" for c in prop.changeset.changes))
    if input_fn("\nApply these changes? Type 'yes' to apply: ").strip().lower() != "yes":
        out("Not applied. Your files are untouched.")
        return 0
    try:
        bid = agent.approve_and_apply(prop, prop.changeset.id)
    except ConflictError as e:
        out(f"NOT applied: {e}")
        return 1
    out(f"Applied. Undo with:  --revert {bid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
