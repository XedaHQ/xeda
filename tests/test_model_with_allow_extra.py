"""`model_with_allow_extra` must relax *only* the subclass, and must survive a process boundary.

Two regressions this guards, both invisible to the rest of the suite:

1. Under pydantic v1 the helper called `copy.deepcopy(cls)`, which returns the *same* class
   object for a class -- so it mutated the original globally. v2 compiles a model's validator at
   class creation, so the relaxation now has to be a real subclass.
2. The subclass is built at runtime, so a `spawn`ed child process has no such attribute to look
   up. `xeda dse` ships designs to `pebble` workers by pickle; without `__reduce__` every DSE
   run dies with `BrokenProcessPool`.
"""

import multiprocessing as mp
import pickle

import pytest

from xeda.dataclass import model_with_allow_extra
from xeda.design import Design, DesignValidationError

RTL = {"sources": [], "top": "t"}


def _make():
    return model_with_allow_extra(Design)(name="d", rtl=RTL, unknown_key=42)


def test_extra_keys_are_accepted_by_the_derived_model():
    assert _make().unknown_key == 42  # type: ignore[attr-defined]


def test_the_base_model_is_left_strict():
    """v1 flipped `Design` itself; nothing outside the derived class may be relaxed."""
    model_with_allow_extra(Design)
    assert Design.model_config["extra"] == "forbid"
    with pytest.raises(DesignValidationError, match="unknown_key"):
        Design(name="d", rtl=RTL, unknown_key=42)


def test_derived_class_is_cached():
    """A fresh class per call would defeat pickle identity and rebuild schemas needlessly."""
    assert model_with_allow_extra(Design) is model_with_allow_extra(Design)


def test_instance_pickles_in_process():
    restored = pickle.loads(pickle.dumps(_make()))
    assert (restored.name, restored.rtl.top, restored.unknown_key) == ("d", "t", 42)


def _child(blob, q):
    d = pickle.loads(blob)
    q.put((type(d).__name__, d.name, d.unknown_key))


def test_instance_unpickles_in_a_spawned_process():
    """The DSE path: the child imports xeda fresh and must rebuild the runtime class."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_child, args=(pickle.dumps(_make()), q))
    proc.start()
    proc.join(120)
    assert proc.exitcode == 0, "spawned worker died unpickling the derived model"
    assert q.get(timeout=10) == ("DesignAllowExtra", "d", 42)
