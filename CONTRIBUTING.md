# Contributing to FreeFlow

Thank you for helping improve FreeFlow.

## Before opening an issue

- Search existing issues first.
- Use the bug or feature issue form.
- Do not include private transcripts, API keys, or sensitive log contents.
- For security problems, follow [SECURITY.md](SECURITY.md) instead.

## Development setup

FreeFlow targets Windows and Python 3.11 or newer.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python freeflow.py --smoke-test
.\.venv\Scripts\python freeflow.py --demo
```

Install `requirements-gpu.txt` only when testing NVIDIA acceleration.

## Pull requests

1. Create a focused branch.
2. Keep changes small enough to review.
3. Run the syntax and smoke checks.
4. Update documentation for user-facing behavior.
5. Add a changelog entry under **Unreleased**.
6. Explain privacy or clipboard implications when relevant.

By contributing, you agree that your contribution will be licensed under the
MIT License.
