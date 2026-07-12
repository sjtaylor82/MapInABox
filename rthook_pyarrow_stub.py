# Runtime hook: if pyarrow somehow ends up partially bundled (missing native
# extensions), give it a __version__ so pandas' compat module doesn't crash
# with AttributeError before its ImportError fallback path can handle things.
import sys

try:
    import pyarrow
    if not hasattr(pyarrow, "__version__"):
        pyarrow.__version__ = "0.0.0"
except ImportError:
    pass
