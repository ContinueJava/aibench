"""Isolate WebUI tests from the root tests/conftest.py.

The root conftest imports transformers + genai_bench.user (heavy/optional in a
minimal WebUI environment). Running pytest from tests/web/ (or pointing at this
dir) uses THIS conftest instead, so the self-contained WebUI suite needs only
flask + gevent + pytest.
"""
