import unittest
import importlib.util
from pathlib import Path


class TestIssue2(unittest.TestCase):
    def setUp(self):
        # Load app module
        spec = importlib.util.spec_from_file_location('app', Path(__file__).parents[1] / 'app.py')
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)

    def test_single_line_document(self):
        class FakeCtx:
            def llm(self, task, prompt, **values):
                return "Mocked answer"

        ctx = FakeCtx()
        payload = {
            "document": "This is a single line document.",
            "question": ""
        }
        # This should not raise IndexError
        result = self.app.compute(payload, ctx)
        self.assertIn("answer", result)

    def test_multi_line_document(self):
        class FakeCtx:
            def llm(self, task, prompt, **values):
                return "Mocked answer"

        ctx = FakeCtx()
        payload = {
            "document": "Line 1\nLine 2",
            "question": ""
        }
        result = self.app.compute(payload, ctx)
        self.assertIn("answer", result)


if __name__ == '__main__':
    unittest.main()
