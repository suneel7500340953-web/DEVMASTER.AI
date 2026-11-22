"""The agent loop: understand -> inspect -> plan -> generate -> test (sandbox) -> fix -> propose.
It NEVER writes to the project. It returns a Proposal; the caller applies it with approve_and_apply()."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import prompts
from .pricing import Pricing, estimate_tokens
from .providers import LLMError
from .store import Store
from .testrunner import TestResult
from .workspace import (ChangeSet, apply_blocks, apply_changes, diff_changeset, parse_file_blocks,
                        snapshot)

KEY_FILES = ("readme", "package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml")


@dataclass
class Usage:
    purpose: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None


@dataclass
class Proposal:
    task: str
    request: str
    plan: dict = field(default_factory=dict)
    changeset: ChangeSet = field(default_factory=ChangeSet)
    test_result: TestResult | None = None
    report: str | None = None
    questions: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    usage: list = field(default_factory=list)
    rounds: int = 0

    @property
    def tokens(self): return sum(u.tokens_in + u.tokens_out for u in self.usage)

    @property
    def cost_usd(self):
        costs = [u.cost_usd for u in self.usage]
        return sum(costs) if costs and None not in costs else None


class BudgetExceeded(RuntimeError):
    pass


def extract_json(text: str) -> dict | None:
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


def build_context(files: dict, request: str, budget: int, focus=()) -> tuple[str, list, list]:
    """Project tree + the most relevant files that fit in `budget` characters."""
    words = set(re.findall(r"[a-z_]{3,}", request.lower()))

    def score(path):
        s = 1000 if path in focus else 0
        low = path.lower()
        s += sum(5 for w in words if w in low)
        s += 3 if any(k in low for k in KEY_FILES) else 0
        body = files[path][:5000].lower()
        return s + sum(min(body.count(w), 5) for w in words)

    tree = "\n".join(sorted(files)) or "(empty project)"
    out, used, inc, omitted = [f"FILE TREE:\n{tree}\n"], len(tree), [], []
    for p in sorted(files, key=lambda x: (-score(x), x)):
        block = f"\n### FILE {p}\n```\n{files[p]}\n```\n"
        if used + len(block) > budget:
            omitted.append(p)
            continue
        out.append(block); used += len(block); inc.append(p)
    return "".join(out), inc, omitted


class Agent:
    def __init__(self, provider, model, project_root, *, test_runner=None, pricing=None, store=None,
                 conversation_id=None, max_fix_rounds=2, context_chars=60_000, max_tokens=4096,
                 token_budget=400_000, on_event=None):
        self.provider, self.model, self.root = provider, model, project_root
        self.runner, self.pricing = test_runner, pricing or Pricing()
        self.store = store or Store()
        self.conv = conversation_id or self.store.new_conversation()
        self.max_fix_rounds, self.context_chars = max_fix_rounds, context_chars
        self.max_tokens, self.token_budget = max_tokens, token_budget
        self.emit = on_event or (lambda stage, detail="": None)

    # ---- one LLM call, with accounting ----
    def _call(self, prop: Proposal, purpose: str, messages: list) -> str:
        if prop.tokens + sum(estimate_tokens(m["content"]) for m in messages) > self.token_budget:
            raise BudgetExceeded(f"Token budget of {self.token_budget} would be exceeded. Stopped before calling the model.")
        r = self.provider.complete(messages, self.model, max_tokens=self.max_tokens)
        cost = self.pricing.cost(r.model, r.tokens_in, r.tokens_out, r.provider)
        prop.usage.append(Usage(purpose, r.provider, r.model, r.tokens_in, r.tokens_out, cost))
        self.store.add_usage(self.conv, r.provider, r.model, purpose, r.tokens_in, r.tokens_out, cost)
        return r.text

    def _messages(self, task, user_content):
        sys = prompts.SYSTEM + task.instruction + ("\n\n" + prompts.FORMAT if task.mode == "changes" else "")
        return [{"role": "system", "content": sys}, *self.store.history(self.conv), {"role": "user", "content": user_content}]

    # ---- the workflow ----
    def run(self, task_key: str, request: str, extra: str = "") -> Proposal:
        task = prompts.TASKS[task_key]
        prop = Proposal(task_key, request)
        try:
            return self._run(task, request, extra, prop)
        except BudgetExceeded as e:
            prop.notes.append(str(e))
            return prop
        except LLMError as e:
            prop.notes.append(f"AI provider error: {e}")
            return prop

    def _run(self, task, request, extra, prop):
        self.emit("inspect", "reading project (secrets/binaries/build dirs are skipped)")
        original = snapshot(self.root)
        ctx, included, omitted = build_context(original, request, self.context_chars)
        if omitted:
            prop.notes.append(f"{len(omitted)} file(s) did not fit in the context and were not shown to the AI.")
        head = f"TASK: {task.key}\nREQUEST: {request}\n"
        if extra:
            head += f"\nEXTRA CONTEXT:\n{extra}\n"

        if task.mode == "report":
            self.emit("analyze", task.key)
            prop.report = self._call(prop, task.key, self._messages(task, f"{head}\nPROJECT:\n{ctx}"))
            self._remember(prop)
            return prop

        self.emit("plan", "understanding requirements and planning")
        text = self._call(prop, "plan", self._messages(task, f"{head}\nPROJECT:\n{ctx}\n\n{prompts.PLAN}"))
        plan = extract_json(text) or {"understanding": text[:400], "steps": [], "files_to_modify": [], "files_to_create": []}
        prop.plan = plan
        if plan.get("blocking_questions"):
            prop.questions = list(plan["blocking_questions"])
            self.emit("questions", "the AI needs more information")
            self._remember(prop)
            return prop

        focus = [f for f in plan.get("files_to_modify", []) if isinstance(f, str)]
        ctx, _, _ = build_context(original, request, self.context_chars, focus)
        self.emit("generate", "writing code changes")
        gen = self._call(prop, "generate", self._messages(
            task, f"{head}\nPLAN:\n{json.dumps(plan)}\n\nPROJECT:\n{ctx}\n\nNow output the file blocks."))
        proposed = dict(original)
        rejected = apply_blocks(proposed, parse_file_blocks(gen))
        if rejected:
            prop.notes.append("Ignored unsafe/protected paths: " + ", ".join(rejected))
        if proposed == original:
            prop.notes.append("The AI returned no file changes.")

        for rnd in range(self.max_fix_rounds + 1):
            prop.rounds = rnd
            if not self.runner or proposed == original:
                break
            self.emit("test", f"running tests in sandbox (round {rnd + 1})")
            res = self.runner.run(proposed)
            prop.test_result = res
            if not res.ran or res.passed or rnd == self.max_fix_rounds:
                break
            self.emit("fix", "tests failed, asking the AI to fix")
            changed = {p: proposed[p] for p in proposed if original.get(p) != proposed[p]}
            files_txt = "".join(f"\n### FILE {p}\n```\n{c}\n```\n" for p, c in changed.items())
            fix = self._call(prop, f"fix{rnd + 1}", self._messages(
                task, f"{head}\nThe tests FAILED.\nTEST OUTPUT:\n{res.output}\n\nCURRENT PROPOSED FILES:{files_txt}\n\n"
                      "Fix the problem. Output ONLY the file blocks that must change."))
            rejected = apply_blocks(proposed, parse_file_blocks(fix))
            if rejected:
                prop.notes.append("Ignored unsafe/protected paths: " + ", ".join(rejected))
        if not self.runner:
            prop.notes.append("Tests were NOT run (no test runner configured). Review the code carefully.")
        elif prop.test_result and not prop.test_result.ran:
            prop.notes.append("Tests were NOT run: " + prop.test_result.reason)
        elif prop.test_result and prop.test_result.passed is False:
            prop.notes.append("Tests still FAIL after the fix attempts. Review before applying.")

        prop.changeset = diff_changeset(original, proposed)
        self.emit("review", f"{len(prop.changeset.changes)} file change(s) ready for your approval")
        self._remember(prop)
        return prop

    def _remember(self, prop: Proposal):
        self.store.add_message(self.conv, "user", f"[{prop.task}] {prop.request}")
        if prop.report:
            summary = prop.report[:1500]
        else:
            files = ", ".join(c.path for c in prop.changeset.changes) or "no file changes"
            summary = f"Plan: {prop.plan.get('understanding', '')[:400]}\nProposed changes: {files}"
        self.store.add_message(self.conv, "assistant", summary)

    # ---- approval gate ----
    def approve_and_apply(self, prop: Proposal, approval_id: str) -> str:
        """Writes to disk ONLY if approval_id equals the proposal's change-set id (see ChangeSet.id)."""
        return apply_changes(self.root, prop.changeset, approval_id)
