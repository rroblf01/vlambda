"""vlambda: an ASGI-like server for Python lambdas, written in V."""

import os

from vlambda.capabilities import EgressResponse, fetch, kv  # noqa: F401
from vlambda.response import Response  # noqa: F401

# Sandboxed runners only need the pure-Python modules; importing the native
# extension there adds startup latency without being used.
if os.environ.get("VLRUN_NO_NATIVE") != "1":
    from vlambda_native import *  # noqa: F401,F403
