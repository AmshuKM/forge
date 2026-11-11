import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("docqa_app", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class FakeCtx:
    def __init__(self, reply):
        self.reply, self.seen = reply, []

    def llm(self, task, prompt, **values):
        self.seen.append((task, prompt))
        return self.reply


class DocQaTest(unittest.TestCase):
    def test_rejects_empty_and_oversized_input(self):
        with self.assertRaises(ValueError):
            app.validate_input({"document": "  "})
        with self.assertRaises(ValueError):
            app.validate_input({"document": "x" * (app.MAX_DOC + 1)})
        with self.assertRaises(ValueError):
            app.validate_input({"document": "ok", "question": "q" * 501})

    def test_question_picks_answer_task_and_empty_picks_summary(self):
        ctx = FakeCtx(" hello ")
        self.assertEqual(app.compute({"document": "d", "question": "q"}, ctx), {"answer": "hello"})
        app.compute({"document": "d", "question": ""}, ctx)
        self.assertEqual(ctx.seen, [("answer", "docqa_answer"), ("summarize", "docqa_summarize")])

    def test_output_validation(self):
        doc = {"document": "x" * 2000, "question": ""}
        app.validate_output(doc, {"answer": "short"})
        for bad in ("", "y" * 4001, "y" * 2000):
            with self.assertRaises(ValueError):
                app.validate_output(doc, {"answer": bad})


if __name__ == "__main__":
    unittest.main()
