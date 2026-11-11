import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("app", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class SlugifyTest(unittest.TestCase):
    def slug(self, text):
        payload = app.validate_input({"text": text})
        out = app.compute(payload, None)
        app.validate_output(payload, out)
        return out["answer"]

    def test_slugs(self):
        self.assertEqual(self.slug("Hello, World!"), "hello-world")
        self.assertEqual(self.slug("  Café au lait  "), "cafe-au-lait")
        self.assertEqual(self.slug("a--b__c"), "a-b-c")

    def test_rejects_unusable_input(self):
        for bad in ("", "   ", "!!!", "x" * 201):
            with self.assertRaises(ValueError):
                app.validate_input({"text": bad})


if __name__ == "__main__":
    unittest.main()
