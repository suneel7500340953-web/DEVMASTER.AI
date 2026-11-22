import os, subprocess, sys, tempfile, unittest
from unittest import mock

from app.modules.runs import languages as L
from app.modules.runs import sandbox as S

REQUIRED = {"python", "javascript", "typescript", "html", "css", "java", "c", "cpp", "csharp",
            "go", "rust", "php", "sql", "kotlin", "dart", "json"}


class Registry(unittest.TestCase):
    def test_all_required_languages_present(self):
        self.assertEqual(REQUIRED, set(L.LANGUAGES))

    def test_extensions_unique_and_lookup(self):
        exts = [e for l in L.LANGUAGES.values() for e in l.extensions]
        self.assertEqual(len(exts), len(set(exts)))
        self.assertEqual(L.language_for_filename("a/b/main.rs").id, "rust")
        self.assertEqual(L.language_for_filename("X.CPP").id, "cpp")
        self.assertIsNone(L.language_for_filename("README"))
        self.assertIsNone(L.language_for_filename("a.unknown"))

    def test_executable_languages_have_image_command_and_install_help(self):
        for l in L.LANGUAGES.values():
            if l.execution == "run":
                self.assertTrue(l.image and l.run and l.requires and l.install, l.id)

    def test_html_and_css_are_preview_only(self):
        for i in ("html", "css"):
            self.assertEqual(L.LANGUAGES[i].execution, "preview")
            with self.assertRaises(ValueError):
                S.build_command(L.LANGUAGES[i], "index." + i)

    def test_compiled_languages_compile_first(self):
        for i in ("c", "cpp", "rust", "kotlin"):
            comp, run = S.build_command(L.LANGUAGES[i], "src/main" + L.LANGUAGES[i].extensions[0])
            self.assertIsNotNone(comp, i)
        for i in ("python", "javascript", "go", "php", "dart"):
            comp, _ = S.build_command(L.LANGUAGES[i], "main" + L.LANGUAGES[i].extensions[0])
            self.assertIsNone(comp, i)


class Detection(unittest.TestCase):
    def test_missing_runtime_returns_install_instructions(self):
        st = L.detect_runtime(L.LANGUAGES["rust"], which=lambda b: None)
        self.assertFalse(st.available)
        self.assertEqual(st.missing, ("rustc",))
        self.assertIn("rustup", st.install)

    def test_partial_toolchain_is_unavailable(self):  # java needs javac AND java
        st = L.detect_runtime(L.LANGUAGES["java"], which=lambda b: "/x/java" if b == "java" else None)
        self.assertFalse(st.available)
        self.assertEqual(st.missing, ("javac",))

    def test_alternatives_and_version(self):
        fake = mock.Mock(return_value=mock.Mock(stdout="Python 3.12.1\n", stderr=""))
        st = L.detect_runtime(L.LANGUAGES["python"], which=lambda b: "/u/python" if b == "python" else None, runner=fake)
        self.assertTrue(st.available)
        self.assertEqual(st.version, "Python 3.12.1")

    def test_preview_always_available(self):
        self.assertTrue(L.detect_runtime(L.LANGUAGES["html"], which=lambda b: None).available)


class Sandbox(unittest.TestCase):
    def argv(self, **kw):
        return S.docker_run_argv(L.LANGUAGES["python"], "main.py", "/srv/ws/p1",
                                 container_name="dm-run-test", **kw)

    def test_hardening_flags(self):
        a = self.argv()
        for flag in ("--read-only", "--cap-drop", "--pids-limit", "--memory", "--cpus", "--user", "--tmpfs"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--network") + 1], "none")
        self.assertIn("no-new-privileges", a)
        self.assertEqual(a[a.index("--user") + 1], "65534:65534")
        self.assertTrue(any("readonly" in x and "target=/work" in x for x in a))

    def test_never_privileged_or_docker_socket(self):
        joined = " ".join(self.argv())
        self.assertNotIn("--privileged", joined)
        self.assertNotIn("docker.sock", joined)

    def test_network_is_opt_in(self):
        self.assertNotIn("--network", self.argv(network=True))

    def test_compile_and_run_chain(self):
        a = S.docker_run_argv(L.LANGUAGES["c"], "m.c", "/srv/ws", container_name="dm-x")
        self.assertEqual(a[-3:-1], ["sh", "-c"])
        self.assertIn("gcc -O2 -Wall m.c -o /tmp/m && /tmp/m", a[-1])

    def test_rejects_unsafe_filenames(self):
        for bad in ("", "../x.py", "/etc/passwd", "-rf.py", "a b.py", "a;rm -rf.py", "a$(id).py", "x/../y.py", "a\n.py"):
            with self.assertRaises(ValueError, msg=bad):
                S.validate_filename(bad)

    def test_rejects_bad_workspace_and_name(self):
        with self.assertRaises(ValueError):
            S.docker_run_argv(L.LANGUAGES["python"], "a.py", "relative/path", container_name="ok")
        with self.assertRaises(ValueError):
            S.docker_run_argv(L.LANGUAGES["python"], "a.py", "/a,b", container_name="ok")
        with self.assertRaises(ValueError):
            S.docker_run_argv(L.LANGUAGES["python"], "a.py", "/a", container_name="bad name;")

    def test_timeout_kills_container(self):
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(argv, 1)
            return mock.Mock()
        with mock.patch.object(S.subprocess, "run", fake_run):
            r = S.run_sandboxed(L.LANGUAGES["python"], "a.py", "/srv/ws", timeout_s=1)
        self.assertTrue(r.timed_out)
        self.assertEqual(calls[-1][:2], ["docker", "kill"])
        self.assertEqual(calls[-1][2], r.container)

    def test_output_is_truncated(self):
        big = "x" * (S.MAX_OUTPUT + 10)
        with mock.patch.object(S.subprocess, "run", return_value=mock.Mock(stdout=big, stderr="", returncode=0)):
            r = S.run_sandboxed(L.LANGUAGES["python"], "a.py", "/srv/ws")
        self.assertTrue(r.truncated)
        self.assertEqual(len(r.stdout), S.MAX_OUTPUT)


class RealLocalExecution(unittest.TestCase):
    """Runs the SQL and JSON commands for real with the local Python (no Docker needed)."""
    def _run(self, lang_id, filename, content):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, filename), "w") as f:
                f.write(content)
            _, run = S.build_command(L.LANGUAGES[lang_id], filename)
            run = [sys.executable if run[0] == "python" else run[0], *run[1:]]
            return subprocess.run(run, cwd=d, capture_output=True, text=True)

    def test_sql_select(self):
        r = self._run("sql", "q.sql", "CREATE TABLE t(a,b);\nINSERT INTO t VALUES (1,'x'),(2,'y');\nSELECT * FROM t;\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["a | b", "1 | x", "2 | y"])

    def test_sql_error_is_reported(self):
        r = self._run("sql", "q.sql", "SELECT * FROM missing;\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SQL error", r.stderr)

    def test_json_valid_and_invalid(self):
        self.assertEqual(self._run("json", "a.json", '{"a": 1}').returncode, 0)
        self.assertNotEqual(self._run("json", "a.json", "{bad").returncode, 0)


if __name__ == "__main__":
    unittest.main()
