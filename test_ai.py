import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

from app.modules.ai import agent as A, cli, providers as P, workspace as W
from app.modules.ai.pricing import Pricing
from app.modules.ai.store import Store
from app.modules.ai.testrunner import DockerTestRunner, LocalTestRunner, TestResult, detect_test_command


def blocks(*items):
    return "\n".join(f"<<<FILE {a} {p}>>>\n{c}\n<<<END>>>" for a, p, c in items)


class FakeProvider:
    name = "fake"
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def complete(self, messages, model, max_tokens=0, temperature=0):
        self.calls.append(messages)
        return P.LLMResponse(self.replies.pop(0), 100, 50, model, "fake")


class FakeRunner:
    def __init__(self, results): self.results, self.seen = list(results), []
    def run(self, files):
        self.seen.append(dict(files)); return self.results.pop(0)


class TmpProject(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        self.root = Path(self.d)
        (self.root / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (self.root / "README.md").write_text("# demo\n")

    def read(self, rel): return (self.root / rel).read_text()


class Workspace(TmpProject):
    def test_snapshot_skips_secrets_binaries_and_build_dirs(self):
        (self.root / ".env").write_text("KEY=secret")
        (self.root / "server.pem").write_text("x")
        (self.root / "node_modules").mkdir(); (self.root / "node_modules/x.js").write_text("x")
        (self.root / ".git").mkdir(); (self.root / ".git/config").write_text("x")
        (self.root / "bin.dat").write_bytes(b"\x00\x01")
        (self.root / "link").symlink_to(self.root / "app.py")
        self.assertEqual(sorted(W.snapshot(self.root)), ["README.md", "app.py"])

    def test_safe_rel_and_protected(self):
        for bad in ("../x", "/etc/passwd", "a/../../b", "C:\\x", "", "a\x00b"):
            with self.assertRaises(W.PathError): W.safe_rel(bad)
        self.assertEqual(W.safe_rel("./src\\a.py"), "src/a.py")
        for p in (".env", "x/.env.local", "id_rsa", "k.pem", ".git/hooks/x"):
            self.assertTrue(W.is_protected(p), p)

    def test_parse_blocks_and_rejections(self):
        text = blocks(("modify", "app.py", "x = 1"), ("create", "../evil.py", "x"), ("create", ".env", "S=1"))
        proposed = {"app.py": "old\n"}
        rej = W.apply_blocks(proposed, W.parse_file_blocks(text))
        self.assertEqual(proposed, {"app.py": "x = 1\n"})
        self.assertEqual(len(rej), 2)

    def test_changeset_diff_and_id_stable(self):
        o = {"a": "1\n", "b": "2\n"}; n = {"a": "1\n", "b": "3\n", "c": "new\n"}
        cs = W.diff_changeset(o, n)
        self.assertEqual([(c.action, c.path) for c in cs.changes], [("modify", "b"), ("create", "c")])
        self.assertIn("-2", cs.diff_text()); self.assertEqual(cs.id, W.diff_changeset(o, n).id)

    def test_apply_requires_matching_approval_and_writes_nothing_otherwise(self):
        cs = W.diff_changeset(W.snapshot(self.root), {**W.snapshot(self.root), "app.py": "new\n"})
        with self.assertRaises(W.ApprovalError): W.apply_changes(self.root, cs, "wrong")
        self.assertIn("a - b", self.read("app.py"))

    def test_apply_backup_and_revert(self):
        snap = W.snapshot(self.root)
        new = {**snap, "app.py": "def add(a, b):\n    return a + b\n", "t/new.py": "x\n"}; new.pop("README.md")
        cs = W.diff_changeset(snap, new)
        bid = W.apply_changes(self.root, cs, cs.id)
        self.assertIn("a + b", self.read("app.py")); self.assertTrue((self.root / "t/new.py").exists())
        self.assertFalse((self.root / "README.md").exists())
        W.revert(self.root, bid)
        self.assertIn("a - b", self.read("app.py")); self.assertTrue((self.root / "README.md").exists())
        self.assertFalse((self.root / "t/new.py").exists())

    def test_conflict_when_user_edited_file_after_proposal(self):
        snap = W.snapshot(self.root)
        cs = W.diff_changeset(snap, {**snap, "app.py": "ai version\n", "README.md": "ai readme\n"})
        (self.root / "app.py").write_text("user edit made meanwhile\n")
        with self.assertRaises(W.ConflictError) as cm: W.apply_changes(self.root, cs, cs.id)
        self.assertEqual(cm.exception.conflicts, ["app.py"])
        self.assertEqual(self.read("app.py"), "user edit made meanwhile\n")
        self.assertEqual(self.read("README.md"), "# demo\n")  # all-or-nothing

    def test_create_refuses_to_overwrite_existing(self):
        cs = W.ChangeSet((W.FileChange("app.py", "create", "x\n", None),))
        with self.assertRaises(W.ConflictError): W.apply_changes(self.root, cs, cs.id)

    def test_symlink_escape_blocked(self):
        outside = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, outside, True)
        (self.root / "sub").symlink_to(outside)
        cs = W.ChangeSet((W.FileChange("sub/x.py", "create", "x\n", None),))
        with self.assertRaises(W.PathError): W.apply_changes(self.root, cs, cs.id)
        self.assertEqual(os.listdir(outside), [])


FIX = blocks(("modify", "app.py", "def add(a, b):\n    return a + b"))
PLAN = json.dumps({"understanding": "fix add", "steps": ["fix operator"], "files_to_modify": ["app.py"], "files_to_create": []})


class AgentFlow(TmpProject):
    def agent(self, replies, results=None, **kw):
        self.p = FakeProvider(replies)
        self.r = FakeRunner(results) if results is not None else None
        return A.Agent(self.p, "m1", self.root, test_runner=self.r, pricing=Pricing({"m1": [1.0, 2.0]}), **kw)

    def test_plan_generate_fail_fix_pass_then_approved_apply(self):
        bad = blocks(("modify", "app.py", "def add(a, b):\n    return a * b"))
        ag = self.agent([PLAN, bad, FIX], [TestResult(True, False, "FAIL: 2*2 != 4", "t"), TestResult(True, True, "OK", "t")])
        events = []; ag.emit = lambda s, d="": events.append(s)
        prop = ag.run("bugfix", "add() is wrong")
        self.assertEqual(events, ["inspect", "plan", "generate", "test", "fix", "test", "review"])
        self.assertTrue(prop.test_result.passed); self.assertEqual(prop.rounds, 1)
        self.assertEqual(self.read("app.py"), "def add(a, b):\n    return a - b\n")   # NOT written yet
        self.assertIn("a * b", self.r.seen[0]["app.py"]); self.assertIn("a + b", self.r.seen[1]["app.py"])
        self.assertIn("FAIL: 2*2", self.p.calls[2][-1]["content"])                    # error analysis fed back
        with self.assertRaises(W.ApprovalError): ag.approve_and_apply(prop, "nope")
        self.assertIn("a - b", self.read("app.py"))
        ag.approve_and_apply(prop, prop.changeset.id)
        self.assertIn("a + b", self.read("app.py"))

    def test_usage_tokens_and_cost(self):
        ag = self.agent([PLAN, FIX], [TestResult(True, True, "ok", "t")])
        prop = ag.run("bugfix", "x")
        self.assertEqual((prop.tokens, len(prop.usage)), (300, 2))
        self.assertAlmostEqual(prop.cost_usd, 2 * (100 * 1 + 50 * 2) / 1e6)
        self.assertEqual(ag.store.usage_summary(ag.conv)["calls"], 2)

    def test_unknown_price_is_reported_as_unknown(self):
        ag = self.agent([PLAN, FIX]); ag.pricing = Pricing()
        self.assertIsNone(ag.run("bugfix", "x").cost_usd)

    def test_stops_after_max_fix_rounds_and_flags_failure(self):
        fails = [TestResult(True, False, "boom", "t")] * 5
        ag = self.agent([PLAN, FIX, FIX, FIX], fails, max_fix_rounds=2)
        prop = ag.run("bugfix", "x")
        self.assertEqual(len(self.r.seen), 3)
        self.assertTrue(any("still FAIL" in n for n in prop.notes))

    def test_no_runner_is_stated_honestly(self):
        prop = self.agent([PLAN, FIX]).run("bugfix", "x")
        self.assertTrue(any("NOT run" in n for n in prop.notes))

    def test_report_tasks_never_produce_changes(self):
        for task in ("explain", "security", "architecture", "git_summary"):
            prop = self.agent(["The report."]).run(task, "please")
            self.assertEqual(prop.report, "The report."); self.assertFalse(prop.changeset)

    def test_blocking_questions_stop_before_generation(self):
        q = json.dumps({"understanding": "?", "blocking_questions": ["Which database?"], "steps": []})
        prop = self.agent([q]).run("api", "make an api")
        self.assertEqual(prop.questions, ["Which database?"]); self.assertEqual(len(self.p.calls), 1)

    def test_unsafe_ai_output_is_dropped(self):
        evil = blocks(("create", "../../etc/x", "x"), ("create", ".env", "A=1"), ("create", "ok.py", "print(1)"))
        prop = self.agent([PLAN, evil]).run("generate", "x")
        self.assertEqual([c.path for c in prop.changeset.changes], ["ok.py"])
        self.assertTrue(any("Ignored" in n for n in prop.notes))

    def test_secrets_never_sent_to_model(self):
        (self.root / ".env").write_text("API_KEY=super-secret-123")
        self.agent([PLAN, FIX]).run("bugfix", "x")
        self.assertFalse(any("super-secret-123" in m["content"] for call in self.p.calls for m in call))

    def test_token_budget_stops_before_calling(self):
        ag = self.agent([PLAN], token_budget=10); prop = ag.run("bugfix", "x")
        self.assertEqual(self.p.calls, []); self.assertTrue(any("budget" in n for n in prop.notes))

    def test_provider_error_becomes_note(self):
        class Boom(FakeProvider):
            def complete(self, *a, **k): raise P.LLMError("401: bad key")
        ag = A.Agent(Boom([]), "m", self.root)
        self.assertIn("401", ag.run("bugfix", "x").notes[0])

    def test_conversation_history_is_kept_and_reused(self):
        ag = self.agent([PLAN, FIX, PLAN, FIX]); ag.run("bugfix", "first"); ag.run("edit", "second")
        self.assertTrue(any("first" in m["content"] for m in self.p.calls[2]))

    def test_context_prioritises_relevant_files_within_budget(self):
        files = {"a.py": "x" * 500, "login.py": "def login(): pass", "z.py": "y" * 500}
        ctx, inc, omitted = A.build_context(files, "fix login", 300)
        self.assertEqual(inc, ["login.py"]); self.assertEqual(len(omitted), 2)


class Providers(unittest.TestCase):
    def test_openai_compatible_parse_and_strip_think(self):
        post = mock.Mock(return_value=(200, {}, {"choices": [{"message": {"content": "<think>hmm</think>hi"}}],
                                                  "usage": {"prompt_tokens": 7, "completion_tokens": 3}}))
        r = P.OpenAICompatible("groq", "https://x/v1/", "KEY", post=post).complete([{"role": "user", "content": "q"}], "m")
        self.assertEqual((r.text, r.tokens_in, r.tokens_out), ("hi", 7, 3))
        url, headers, payload = post.call_args[0]
        self.assertEqual(url, "https://x/v1/chat/completions"); self.assertEqual(headers["Authorization"], "Bearer KEY")
        self.assertIn("max_tokens", payload)

    def test_openai_variant_uses_max_completion_tokens_and_no_temperature(self):
        post = mock.Mock(return_value=(200, {}, {"choices": [{"message": {"content": "x"}}]}))
        P.OpenAICompatible("openai", "https://x", "K", "max_completion_tokens", False, post=post).complete([], "m")
        payload = post.call_args[0][2]
        self.assertIn("max_completion_tokens", payload); self.assertNotIn("temperature", payload)

    def test_anthropic_separates_system_and_parses(self):
        post = mock.Mock(return_value=(200, {}, {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 4, "output_tokens": 2}}))
        r = P.Anthropic("K", post=post).complete([{"role": "system", "content": "S"}, {"role": "user", "content": "q"}], "c")
        payload = post.call_args[0][2]
        self.assertEqual(payload["system"], "S"); self.assertEqual(payload["messages"], [{"role": "user", "content": "q"}])
        self.assertEqual((r.text, r.tokens_in, r.tokens_out), ("ok", 4, 2))
        self.assertEqual(post.call_args[0][1]["x-api-key"], "K")

    def test_retry_on_429_then_success_and_error_message(self):
        post = mock.Mock(side_effect=[(429, {"Retry-After": "1"}, {}), (200, {}, {"choices": [{"message": {"content": "ok"}}]})])
        sleeps = []
        P.OpenAICompatible("g", "https://x", "K", post=post, sleep=sleeps.append).complete([], "m")
        self.assertEqual((post.call_count, sleeps), (2, [1.0]))
        bad = mock.Mock(return_value=(401, {}, {"error": {"message": "invalid key"}}))
        with self.assertRaisesRegex(P.LLMError, "401: invalid key"):
            P.OpenAICompatible("g", "https://x", "K", post=bad).complete([], "m")

    def test_registry_only_has_providers_with_keys_and_never_leaks_them(self):
        reg = P.build_registry(env={"GROQ_API_KEY": "gsk_SECRET"})
        self.assertEqual(sorted(reg), ["groq", "ollama"])
        self.assertNotIn("gsk_SECRET", repr(reg["groq"]))

    def test_custom_provider_from_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"providers": {"mine": {"kind": "openai", "base_url": "http://h/v1", "key_env": "MINE"}}}, f)
        self.addCleanup(os.unlink, f.name)
        self.assertIn("mine", P.build_registry(env={"MINE": "k"}, config_path=f.name))
        self.assertNotIn("mine", P.build_registry(env={}, config_path=f.name))


class StoreAndPricing(unittest.TestCase):
    def test_history_trim_and_usage(self):
        s = Store(); c = s.new_conversation()
        for i in range(5): s.add_message(c, "user", f"m{i}" * 50)
        self.assertLess(len(s.history(c, 300)), 5); self.assertEqual(s.history(c, 10**6)[-1]["content"], "m4" * 50)
        s.add_usage(c, "p", "m", "plan", 10, 5, 0.5); s.add_usage(c, "p", "m", "gen", 1, 1, None)
        self.assertIsNone(s.usage_summary(c)["cost_usd"])

    def test_pricing(self):
        p = Pricing({"m": [3, 15], "p/m2": [1, 1]})
        self.assertAlmostEqual(p.cost("m", 1_000_000, 1_000_000), 18.0)
        self.assertEqual(p.cost("m2", 1_000_000, 0, "p"), 1.0); self.assertIsNone(p.cost("zzz", 1, 1))


class Runners(unittest.TestCase):
    PY = {"calc.py": "def add(a,b): return a+b\n",
          "test_calc.py": "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_a(self): self.assertEqual(add(1,2), 3)\nif __name__=='__main__': unittest.main()\n"}

    def test_detect(self):
        self.assertEqual(detect_test_command(self.PY)[3], "python unittest")
        self.assertEqual(detect_test_command({"a.test.js": ""})[3], "node --test")
        self.assertEqual(detect_test_command({"go.mod": "", "a_test.go": ""})[3], "go test")
        self.assertIsNone(detect_test_command({"main.py": ""}))

    def test_local_runner_really_runs_pass_and_fail(self):
        r = LocalTestRunner().run(self.PY); self.assertTrue(r.ran and r.passed, r.output)
        bad = dict(self.PY, **{"calc.py": "def add(a,b): return a-b\n"})
        r = LocalTestRunner().run(bad); self.assertTrue(r.ran); self.assertFalse(r.passed)
        self.assertFalse(LocalTestRunner().run({"main.py": "x"}).ran)

    def test_docker_runner_builds_hardened_command_and_does_not_need_docker_here(self):
        with mock.patch("app.modules.ai.testrunner.subprocess.run", return_value=mock.Mock(returncode=0, stdout="OK", stderr="")) as run:
            r = DockerTestRunner().run(self.PY)
        argv = run.call_args[0][0]
        self.assertTrue(r.passed)
        for f in ("--network", "--read-only", "--cap-drop"): self.assertIn(f, argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")

    def test_docker_missing_is_reported_not_crashed(self):
        with mock.patch("app.modules.ai.testrunner.subprocess.run", side_effect=FileNotFoundError("docker")):
            r = DockerTestRunner().run(self.PY)
        self.assertFalse(r.ran); self.assertIn("Docker not available", r.reason)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class Git(TmpProject):
    def test_git_context_shows_diff(self):
        from app.modules.ai.gitutil import git_context
        run = lambda *a: subprocess.run(["git", *a], cwd=self.root, capture_output=True, check=True)
        run("init", "-q"); run("-c", "user.name=t", "-c", "user.email=t@t", "add", "."); run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n")
        ctx = git_context(self.root)
        self.assertIn("init", ctx); self.assertIn("+    return a + b", ctx)


class CLI(TmpProject):
    def run_cli(self, args, replies, answer="yes", runner=None):
        out = []
        reg = {"fake": FakeProvider(replies)}
        code = cli.main(["-p", self.d, "--provider", "fake", "--model", "m1", "--tests", "off", *args],
                        registry=reg, runner=runner, input_fn=lambda _: answer, out=out.append)
        return code, "\n".join(out)

    def test_end_to_end_apply_on_yes(self):
        code, out = self.run_cli(["--task", "bugfix", "fix add"], [PLAN, FIX])
        self.assertEqual(code, 0); self.assertIn("PROPOSED CHANGES", out); self.assertIn("estimated cost", out)
        self.assertIn("a + b", self.read("app.py")); self.assertIn("--revert", out)

    def test_nothing_written_unless_yes(self):
        _, out = self.run_cli(["--task", "bugfix", "fix add"], [PLAN, FIX], answer="y")
        self.assertIn("a - b", self.read("app.py")); self.assertIn("untouched", out)

    def test_requires_model_and_provider(self):
        out = []
        self.assertEqual(cli.main(["-p", self.d, "--provider", "fake", "x"], registry={"fake": FakeProvider([])}, out=out.append), 2)
        self.assertEqual(cli.main(["-p", self.d, "--provider", "nope", "--model", "m", "x"], registry={}, out=out.append), 2)


if __name__ == "__main__":
    unittest.main()
