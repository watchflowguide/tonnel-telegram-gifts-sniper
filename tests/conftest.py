"""The CLI refuses to run outside Windows (see README: Self-hosting); tests exercise it everywhere."""

import os

os.environ.setdefault("ALLOW_UNSUPPORTED_OS", "1")
