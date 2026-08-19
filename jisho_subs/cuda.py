"""Make the venv's own CUDA libraries reachable before anything dlopen()s them.

CTranslate2 — which faster-whisper sits on — resolves cuDNN and cuBLAS by soname
at runtime rather than through the venv's RPATH.  If they are not on the loader
path it dies with a bare native abort::

    Unable to load any of {libcudnn_ops.so.9.1.0, ..., libcudnn_ops.so}
    Invalid handle. Cannot load symbol cudnnCreateTensorDescriptor

— no traceback, no usable exit code.  glibc caches ``LD_LIBRARY_PATH`` when the
process starts, so setting ``os.environ`` afterwards is too late; the only fix
from inside Python is to re-exec once with the variable in place.

Doing it here rather than in a shell wrapper means the tool cannot be broken by
being invoked some other way — which is exactly how the neighbouring ``subplz``
wrapper accumulated a stale cuDNN 8 path that outlived its reason to exist.
"""

from __future__ import annotations

import os
import sys

_GUARD = "JISHO_SUBS_CUDA_REEXEC"


def _library_dirs() -> list[str]:
    dirs = []
    for module in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
        try:
            mod = __import__(module, fromlist=["__path__"])
            dirs.append(list(mod.__path__)[0])
        except Exception:
            pass
    return dirs


def ensure_library_path() -> None:
    """Re-exec with the venv's CUDA library directories on ``LD_LIBRARY_PATH``.

    A no-op when they are already there, when there is nothing to add, or when
    this has already run once (so a missing library can never loop).
    """
    if os.environ.get(_GUARD) or sys.platform != "linux":
        return

    dirs = _library_dirs()
    if not dirs:
        return                              # CPU-only install: nothing to do

    current = os.environ.get("LD_LIBRARY_PATH", "")
    present = set(filter(None, current.split(os.pathsep)))
    missing = [d for d in dirs if d not in present]
    if not missing:
        return

    env = dict(os.environ)
    env[_GUARD] = "1"
    env["LD_LIBRARY_PATH"] = os.pathsep.join(missing + ([current] if current else []))
    os.execve(sys.executable, [sys.executable, "-m", "jisho_subs"] + sys.argv[1:], env)
