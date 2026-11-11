"""Turn a title into a URL slug. Uses no AI model."""
import re
import unicodedata

FIELDS = [("text", "Title to turn into a slug", "text")]


def _plain(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def validate_input(payload):
    text = str(payload.get("text", "")).strip()
    if not text:
        raise ValueError("Please enter a title.")
    if len(text) > 200:
        raise ValueError("That title is too long (limit 200 characters).")
    if not re.search(r"[a-z0-9]", _plain(text)):
        raise ValueError("The title needs at least one letter or digit.")
    return {"text": text}


def compute(payload, ctx):
    return {"answer": re.sub(r"[^a-z0-9]+", "-", _plain(payload["text"])).strip("-")}


def validate_output(payload, output):
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", output.get("answer", "")):
        raise ValueError("the slug was empty or malformed")
