import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

os.environ["FORGE_DB"] = ":memory:"

from forge import evals, improve, release, router, runtime, store  # noqa: E402

BAD_APP = """
FIELDS = []
calls = []
def validate_input(p):
    if "boom" in p: raise KeyError("boom")
    if not p.get("x"): raise ValueError("Need x.")
    return p
def compute(p, ctx):
    calls.append(1)
    return {"answer": "a"}
def validate_output(p, o):
    raise ValueError("never good enough")
"""

PAID = """
[budget]
daily_usd = 1.0
timeout_s = 5
[endpoints.c]
base = "http://unused.invalid/v1"
key_env = "FORGE_TEST_KEY"
[models.paid]
endpoint = "c"
model = "m"
usd_in = 1.0
usd_out = 1.0
[tasks]
t = ["paid"]
"""


class ForgeTest(unittest.TestCase):
    def setUp(self):
        store._local.__dict__.clear()  # fresh in-memory database per test
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.post = router._post
        self.addCleanup(lambda: setattr(router, "_post", self.post))
        local = self.tmp / "local.toml"  # a fixed pair of models, so tests never depend on this computer's setup
        local.write_text('[endpoints.ollama]\nbase = "http://unused.invalid/v1"\n'
                         '[models.main]\nendpoint = "ollama"\nmodel = "a"\n'
                         '[models.big]\nendpoint = "ollama"\nmodel = "b"\n', "utf-8")
        os.environ["FORGE_LOCAL_CONFIG"] = str(local)
        self.addCleanup(os.environ.pop, "FORGE_LOCAL_CONFIG")

    def root(self, routing=None, app=BAD_APP):
        for d in ("config", "prompts", "apps/t"):
            (self.tmp / d).mkdir(parents=True, exist_ok=True)
        (self.tmp / "config/routing.toml").write_text(routing or (store.ROOT / "config/routing.toml").read_text("utf-8"), "utf-8")
        (self.tmp / "prompts/p.md").write_text("Doc: {{document}} Q: {{question}}", "utf-8")
        (self.tmp / "apps/t/app.py").write_text(app, "utf-8")
        return self.tmp

    def test_router_falls_back_to_next_model(self):
        seen = []

        def post(url, body, key, timeout):
            seen.append(body["model"])
            if len(seen) == 1:
                raise TimeoutError("down")
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

        router._post = post
        self.assertEqual(router.call("summarize", [{"role": "user", "content": "x"}]), "hi")
        self.assertEqual(len(seen), 2)
        self.assertEqual([r["ok"] for r in store.q("select ok from calls order by id")], [0, 1])

    def test_missing_models_give_a_clear_message(self):
        os.environ["FORGE_LOCAL_CONFIG"] = str(self.tmp / "none.toml")
        with self.assertRaisesRegex(router.RouterError, "forge.cli setup"):
            router.call("summarize", [{"role": "user", "content": "x"}])

    def test_local_file_adds_models_without_touching_shared_settings(self):
        cfg = router.config()
        self.assertEqual(cfg["models"]["main"]["model"], "a")
        self.assertEqual(cfg["tasks"]["summarize"], ["main", "big"])
        self.assertIn("daily_usd", cfg["budget"])

    def test_pick_prefers_long_context_then_size_within_gpu_memory(self):
        from forge import models
        m = lambda name, gb, ctx=0, think=False: {"model": name, "size": gb * 1e9, "ctx": ctx, "thinking": think,  # noqa: E731
                                                   "endpoint": "ollama", "base": "x", "key_env": None}
        found = [m("tiny", 2.7, think=True), m("big12", 7.6), m("long9", 6.6, 65536, True), m("plain9", 6.6, 0, True),
                 m("huge", 40)]
        roles = models.pick(found, vram=11)
        self.assertEqual({r: v["model"] for r, v in roles.items()},
                         {"main": "long9", "think": "long9", "fast": "tiny", "big": "big12"})
        text = models.render(roles)
        import tomllib
        cfg = tomllib.loads(text)
        self.assertEqual(cfg["models"]["think"]["extra"], {"reasoning_effort": "medium"})
        self.assertNotIn("extra", cfg["models"]["big"])  # never send reasoning options to a model without them

    def test_router_stops_when_every_model_fails(self):
        router._post = lambda *a: (_ for _ in ()).throw(TimeoutError("down"))
        with self.assertRaises(router.RouterError):
            router.call("summarize", [{"role": "user", "content": "x"}])

    def test_router_enforces_daily_budget_on_paid_models(self):
        root = self.root(PAID)
        os.environ["FORGE_TEST_KEY"] = "k"
        router._post = lambda *a: self.fail("a paid model was called over budget")
        store.x("insert into calls(ts,usd,ok) values(?,?,1)", time.time(), 2.0)
        with self.assertRaisesRegex(router.RouterError, "budget"):
            router.call("t", [{"role": "user", "content": "x"}], root=root)

    def test_render_does_not_expand_slots_inside_values(self):
        text = runtime.render(self.root(), "p", document="{{question}}", question="SECRET")
        self.assertEqual(text, "Doc: {{question}} Q: SECRET")

    def test_runtime_retries_are_bounded_and_failure_is_safe(self):
        r = runtime.handle("t", {"x": "1"}, root=self.root(), version="stable")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], runtime.SAFE_ERROR)
        trace = store.q("select * from traces")[0]
        self.assertTrue(trace["fingerprint"].startswith("OutputInvalid:t:"))

    def test_input_errors_are_shown_and_not_treated_as_faults(self):
        r = runtime.handle("t", {}, root=self.root(), version="stable")
        self.assertEqual(r["error"], "Need x.")
        self.assertIsNone(store.q("select fingerprint from traces")[0]["fingerprint"])

    def test_app_crash_is_fingerprinted_by_type_and_location(self):
        runtime.handle("t", {"boom": 1}, root=self.root(), version="stable")
        self.assertEqual(store.q("select fingerprint from traces")[0]["fingerprint"], "KeyError:app.py:validate_input")

    def test_detect_makes_one_issue_per_fingerprint_and_closes_transient(self):
        for i in range(5):
            store.x("insert into traces(id,ts,app,version,ok,error,fingerprint,ms) values(?,?,?,?,0,'e',?,1)",
                    f"a{i}", time.time(), "t", "stable", "KeyError:app.py:compute")
        store.x("insert into traces(id,ts,app,version,ok,error,fingerprint,ms) values('b',?,'t','stable',0,'e',?,1)",
                time.time(), "RouterError:app.py:compute")
        store.x("insert into traces(id,ts,app,version,ok,error,fingerprint,ms) values('c',?,'t','eval',0,'e','X:y',1)", time.time())
        improve.detect()
        improve.detect()  # a second pass must not double count
        rows = {r["fingerprint"]: r for r in store.q("select * from issues")}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["KeyError:app.py:compute"]["count"], 5)
        self.assertEqual(rows["RouterError:app.py:compute"]["status"], "closed")
        self.assertEqual([r["fingerprint"] for r in improve.triage(5)], ["KeyError:app.py:compute"])

    def test_rejected_issue_stays_blocked_until_occurrences_double(self):
        improve.upsert("fp", "error", "t", "title", "d", 5)
        store.x("update issues set status='rejected', seen_count=count")
        improve.upsert("fp", "error", "t", "title", "d", 1)
        self.assertEqual(store.q("select status from issues")[0]["status"], "rejected")
        improve.upsert("fp", "error", "t", "title", "d", 4)
        self.assertEqual(store.q("select status from issues")[0]["status"], "open")

    def test_triage_skips_apps_with_a_change_in_flight(self):
        improve.upsert("fp", "error", "t", "title", "d")
        store.x("insert into proposals(app,status) values('t','awaiting')")
        self.assertEqual(improve.triage(5), [])

    def test_allow_list_blocks_tests_evals_config_and_forge(self):
        ok = improve.allowed
        self.assertEqual(ok("code", "docqa", ["apps/docqa/app.py", "apps/docqa/tests/test_issue_7.py"], 7), "")
        self.assertEqual(ok("prompt", "docqa", ["prompts/docqa_answer.md"], 7), "")
        for channel, path in [("code", "apps/docqa/tests/test_app.py"), ("code", "apps/docqa/evals/golden.jsonl"),
                              ("code", "forge/improve.py"), ("code", "config/routing.toml"),
                              ("prompt", "prompts/forge_judge.md"), ("prompt", "prompts/../forge/x.md"),
                              ("routing", "apps/docqa/app.py")]:
            self.assertTrue(ok(channel, "docqa", [path], 7), path)

    def test_agent_paths_are_confined_to_its_own_app(self):
        p = {"app": "notes"}
        for good in ("apps/notes/app.py", "apps/notes/tests/test_x.py", "prompts/notes_ask.md"):
            self.assertEqual(improve.agent_path(p, good), good)
        for bad in ("forge/store.py", "apps/docqa/app.py", "config/routing.toml", "apps/notes/../docqa/app.py",
                    "prompts/forge_fix.md", ".mcp.json", "apps/notes_two/app.py", "tests/test_forge.py"):
            with self.assertRaises(ValueError, msg=bad):
                improve.agent_path(p, bad)

    def test_mock_mode_needs_no_model(self):
        os.environ["FORGE_MOCK"] = "1"
        self.addCleanup(os.environ.pop, "FORGE_MOCK")
        router._post = lambda *a: self.fail("a model was called in mock mode")
        self.assertTrue(router.call("summarize", [{"role": "user", "content": "hi"}]).startswith("MOCK REPLY"))

    def test_mcp_lists_tools_and_turns_failures_into_tool_errors(self):
        from forge import mcp
        self.assertIsNone(mcp.handle({"method": "notifications/initialized"}))
        self.assertEqual(mcp.handle({"id": 1, "method": "initialize", "params": {}})["result"]["serverInfo"]["name"], "forge")
        names = [t["name"] for t in mcp.handle({"id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("submit_change", names)
        self.assertFalse({"approve", "release", "merge"} & set(names))  # an agent can never approve its own work
        bad = mcp.handle({"id": 3, "method": "tools/call", "params": {
            "name": "write_file", "arguments": {"change_id": 999, "path": "apps/x/app.py", "content": ""}}})
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("error", mcp.handle({"id": 4, "method": "nope"}))

    def test_examples_become_fixed_golden_cases(self):
        from forge import builder
        cases = builder.parse_examples("2+3*4 -> 14\n\n  sqrt(16) => 4 \nf(x)->y -> z\n10 → ten")
        self.assertEqual([(c["input"]["input"], c["answer"]) for c in cases],
                         [("2+3*4", "14"), ("sqrt(16)", "4"), ("f(x)->y", "z"), ("10", "ten")])
        self.assertEqual(builder.parse_examples(""), [])
        with self.assertRaisesRegex(ValueError, "arrow"):
            builder.parse_examples("no arrow here")
        evals_dir = self.tmp / "apps" / "x" / "evals"
        evals_dir.mkdir(parents=True)
        (evals_dir / "golden.jsonl").write_text(
            '{"id": "you-1", "input": {"input": "2+3*4"}, "answer": "99"}\n{"id": "mine", "input": {}}\n', "utf-8")
        builder.restore_examples(self.tmp, "x", cases[:1])  # the builder edited the owner's answer: put it back
        lines = (evals_dir / "golden.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(lines, ['{"id": "you-1", "input": {"input": "2+3*4"}, "answer": "14"}', '{"id": "mine", "input": {}}'])

    def test_app_names_come_from_the_idea_and_never_clash(self):
        from forge import builder
        self.assertEqual(builder.name_for("Make me a scientific calculator app"), "scientific_calculator")
        self.assertEqual(builder.name_for("A tool to convert units"), "convert_units")
        self.assertEqual(builder.name_for("Count the words in a piece of text"), "count_words")
        self.assertEqual(builder.name_for("In a sentence, reverse every word"), "sentence_reverse_every")
        self.assertEqual(builder.name_for("Convert currencies between euros and dollars"), "convert_currencies")
        self.assertEqual(builder.name_for("Convert temperatures between C and F"), "convert_temperatures_2")  # taken
        self.assertTrue(builder.name_for("Slugify things").startswith("slugify_things"))
        with self.assertRaisesRegex(ValueError, "already exists"):
            builder.name_for("anything", "slugify")
        with self.assertRaises(ValueError):
            builder.name_for("anything", "Bad Name!")

    def test_dashboard_pages_render(self):
        from forge import dashboard
        for html in (dashboard.build_home(), dashboard.apps_page(), dashboard.build_home({"idea": "<script>"}, "oops")):
            self.assertIn("<!doctype html>", html)
            self.assertNotIn("<script>", html)  # everything typed by a person is escaped

    def test_parse_reads_cause_and_files_and_rejects_free_text(self):
        cause, files = improve.parse("CAUSE: a bug\nFILE: apps/x/app.py\n<<<\nprint(1)\n>>>\n")
        self.assertEqual((cause, files), ("a bug", {"apps/x/app.py": "print(1)\n"}))
        with self.assertRaises(ValueError):
            improve.parse("Sure! Here is my fix.")

    def test_write_cannot_escape_the_worktree(self):
        with self.assertRaises(ValueError):
            improve.write(self.tmp, "../outside.txt", "x")

    def test_compare_gate(self):
        base = {"success": 0.8, "p95_ms": 1000, "usd_per_req": 0.0}
        same = dict(base)
        self.assertEqual(evals.compare(base, same, False, 5000), [])
        self.assertTrue(evals.compare(base, same, True, 5000))  # equal is not an improvement
        self.assertEqual(evals.compare(base, {**base, "success": 0.9}, True, 5000), [])
        self.assertTrue(evals.compare(base, {**base, "success": 0.7}, False, 5000))
        self.assertTrue(evals.compare(base, {**base, "p95_ms": 9000}, False, 5000))

    def test_failing_trial_is_discarded_and_remembered(self):
        self.addCleanup(setattr, release, "cleanup", release.cleanup)
        release.cleanup = lambda pid: None  # no real git branches in this test
        store.x("insert into issues(id,fingerprint,kind,app,title,count,status) values(1,'fp','error','t','t',3,'proposed')")
        store.x("insert into proposals(id,issue_id,app,channel,branch,status,summary) values(1,1,'t','code','proposal/1','canary','s')")
        store.kv_set("canary", {"proposal": 1, "path": str(self.tmp), "pct": 50, "need": 6, "since": 0})
        for i in range(3):
            store.x("insert into traces(id,ts,app,version,ok,fingerprint,ms) values(?,?,'t','canary:p1',0,'E:x',1)", f"c{i}", time.time())
        release.check()
        self.assertEqual(store.q("select status from proposals")[0]["status"], "rolled_back")
        self.assertEqual(store.q("select status from issues")[0]["status"], "hold")
        self.assertIsNone(store.kv_get("canary"))
        self.assertEqual(len(store.recall("rolled_back")), 1)


if __name__ == "__main__":
    unittest.main()
