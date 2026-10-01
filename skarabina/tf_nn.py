# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The ``tf-nn`` verb — ``tfcrop`` and a served neural flagger, combined.

``tf-nn`` runs two flaggers over the same data block and combines their
decisions before the block's flags are written:

1. **tfcrop** — :mod:`skarabina.tfcrop`, exactly as the ``tfcrop`` verb runs
   it (its parameters are CASA's defaults);
2. the **neural flagger** — a served radio-nn model (``nn-flag-server``),
   reached over Apache Arrow Flight at the verb's server URL.  The block's
   visibilities are streamed out and its flag mask comes back.

The combination happens **per block**, so only one block is ever resident and
the run's memory is bounded exactly as ``tfcrop``'s is, however large the
table (see :mod:`skarabina.memory`).  ``dask-ms`` does the chunking.

Semantics that make the combination well-defined:

* the two decisions are independent — tfcrop sees the incoming ``FLAG``
  (CASA honours flags on read, so a flagger must run on the flags as they
  are), while the neural flagger reads ``DATA`` only and is unaffected by
  whatever else has flagged.  Neither feeds the other's input.
* **union** (``mode=or``, the default) — flag what either flags.  This is
  the measured state of the art: the network adds recall where tfcrop is
  weak, at a modest cost in flag rate (radio-nn ``SUMMARY.md``).
* **intersection** (``mode=and``) — flag only what both flag.  A
  precision-minded operating point for tight flag budgets.

The server thresholds its own probability (``flag_threshold`` in the served
bundle's manifest); this verb applies the decision it gets back.  For the
union to beat ``tfcrop`` alone the served threshold must be the model's
saturated tail (~0.9997), not a loose one — see radio-nn ``SUMMARY.md``.

The serving protocol needs the per-row ``ANTENNA1``/``ANTENNA2``/``TIME``
columns (the server derives uvw and integration grouping from them), and the
MS's antenna rows must match the served bundle's antenna map — a MeerKAT
``m0xx`` set in the same row order.  Baselines the bundle does not know come
back with their generic flags only (non-finite visibilities).
"""

import threading

import numpy as np

from skarabina import nnflight
from skarabina.tfcrop import TFCropParams

#: The combine modes.
MODES = ("or", "and")


class TFCropNNParams:
    """Validated parameters for one ``tf-nn`` operation.

    ``DEFAULTS`` is what the console summary compares against, so a default
    run prints its server URL and an ``and`` run is obvious at a glance.
    ``chan_freq_hz`` is filled in at run time from the MS (see
    ``DaskMS.flag_tf_nn``); ``tfcrop`` holds the tfcrop verb's parameters,
    fixed at CASA's defaults for now.
    """

    __slots__ = ("server", "mode", "chan_freq_hz", "tfcrop")

    DEFAULTS = {
        "server": None,
        "mode": "or",
    }

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(
                f"unknown tf-nn parameter(s): {', '.join(sorted(unknown))}."
                f" Valid parameters are {', '.join(sorted(self.DEFAULTS))}"
            )
        merged = dict(self.DEFAULTS)
        merged.update(kwargs)
        self.server = merged["server"]
        self.mode = merged["mode"]
        self.chan_freq_hz = None
        self.tfcrop = TFCropParams()
        self._validate()

    def _validate(self):
        if not isinstance(self.server, str) or not self.server.strip():
            raise ValueError(
                "tf-nn needs a server URL, e.g."
                " 'tf-nn grpc://flagger.example:8815'"
            )
        self.server = self.server.strip()
        if self.mode not in MODES:
            raise ValueError(
                f"mode must be one of {', '.join(MODES)}, got {self.mode!r}"
            )


def combine_flags(tfcrop_flags, nn_flags, existing, mode="or"):
    """Combine the two flaggers' masks over one block.

    ``tfcrop_flags`` is what the ``tfcrop`` verb would write for the block
    (it includes the pre-existing flags, as CASA's own output does);
    ``nn_flags`` is the served model's mask.  ``existing`` is the block's
    incoming flags and is preserved either way: a combination never *clears*
    a flag.  Concretely the result is ``existing | (tfcrop_new | nn_new)``
    for ``or`` and ``existing | (tfcrop_new & nn_new)`` for ``and``.
    """
    if mode == "or":
        return np.logical_or(tfcrop_flags, nn_flags)
    if mode == "and":
        return np.logical_and(
            tfcrop_flags, np.logical_or(nn_flags, existing)
        )
    raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")


def nn_block_mask(vis, time, antenna1, antenna2, params):
    """The served model's flag mask for one block.

    Rows the server cannot model (autos, unknown baselines, incomplete
    integrations) come back flagged only where the server's generic rules
    say so (non-finite visibilities), which is what the wire protocol means
    by "keep their existing flags".
    """
    n_rows, n_chan, n_pol = vis.shape
    if n_rows == 0:
        return np.zeros((0, n_chan, n_pol), dtype=bool)
    scorer = _scorer(params, n_chan, n_pol)
    return scorer.score(vis, time, antenna1, antenna2)


def _scorer(params, n_chan, n_pol):
    """A cached :class:`nnflight.NNScorer` for this server and shape.

    Cached per thread and key so the run keeps one gRPC channel per worker
    thread instead of one per block; the key includes the channel
    frequencies because they travel in the stream descriptor.
    """
    freq = params.chan_freq_hz
    key = (
        params.server,
        n_chan,
        n_pol,
        None if freq is None else np.ascontiguousarray(freq, dtype=np.float64).tobytes(),
    )
    cache = getattr(_SCORER_CACHE, "cache", None)
    if cache is None:
        cache = _SCORER_CACHE.cache = {}
    scorer = cache.get(key)
    if scorer is None:
        scorer = nnflight.NNScorer(
            params.server, n_chan, n_pol, chan_freq_hz=freq
        )
        cache[key] = scorer
    return scorer


_SCORER_CACHE = threading.local()
