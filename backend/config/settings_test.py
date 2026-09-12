"""Settings used by the test suite.

Tests must not depend on a developer's .env file or on real secrets, so the
required variables are defaulted here *before* config.settings is imported.
Everything else is inherited unchanged, which means tests exercise the same
configuration the application actually runs with.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-insecure-key")
os.environ.setdefault("DJANGO_DEBUG", "False")

from config.settings import *  # noqa: E402,F401,F403
