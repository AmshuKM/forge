"""Document summariser and Q&A.

Every app provides: FIELDS, validate_input, compute, validate_output.
"""

FIELDS = [
    ("document", "Document", "textarea"),
    ("question", "Question (leave empty for a summary)", "text"),
]
MAX_DOC = 20_000  # stays under context_chars so nothing is cut off silently


def validate_input(payload):
    document = str(payload.get("document", "")).strip()
    question = str(payload.get("question", "")).strip()
    if not document:
        raise ValueError("Please paste a document.")
    if len(document) > MAX_DOC:
        raise ValueError("That document is too long (limit 20,000 characters).")
    if len(question) > 500:
        raise ValueError("That question is too long (limit 500 characters).")
    return {"document": document, "question": question}


def compute(payload, ctx):
    if payload["question"]:
        text = ctx.llm("answer", "docqa_answer", document=payload["document"], question=payload["question"])
    else:
        text = ctx.llm("summarize", "docqa_summarize", document=payload["document"])
    return {"answer": text.strip()}


def validate_output(payload, output):
    answer = output.get("answer", "")
    if not answer:
        raise ValueError("the answer was empty")
    if len(answer) > 4000:
        raise ValueError("the answer was longer than 4000 characters")
    if not payload["question"] and len(payload["document"]) > 1000 and len(answer) >= len(payload["document"]):
        raise ValueError("the summary was not shorter than the document")

