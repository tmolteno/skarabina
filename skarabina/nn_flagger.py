# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The ``nn-flagger`` verb — union in a served neural flagger's decisions.

``nn-flagger`` streams each data block to a served radio-nn model
(``nn-flag-server``) over Apache Arrow Flight and ORs the returned flag mask
into ``FLAG``.  Written after another verb it unions with that verb's
decisions, which is what the ``--flag`` order means::

    skarabina --ms foo.ms --flag "tfcrop [timecutoff=5], nn-flagger grpc://host:8815"

That is the measured state of the union (radio-nn ``SUMMARY.md``): the
network adds recall where ``tfcrop`` is weak.  Write ``nn-flagger`` **after**
the classical verbs it unions with — it is unaffected by existing flags (it
reads ``DATA`` only), but ``tfcrop`` is not: it honours ``FLAG`` on read the
way CASA's ``flagdata`` does, so a pre-flagged table changes its statistics
and its numbers no longer transfer.

The verb is union-only by construction: an *intersection* would have to clear
flags an earlier verb set, which is not what an ordered run of flagging
operations should do.  ``tf-nn`` (see :mod:`skarabina.tf_nn`) has both
masks in hand per chunk and offers ``and`` as well, at the cost of running
tfcrop at CASA's defaults.

The served model thresholds its own probability (``flag_threshold`` in the
served bundle's manifest); this verb applies the decision it gets back.  For
the union to beat ``tfcrop`` alone the served threshold must be the model's
saturated tail (~0.9997) — see radio-nn ``SUMMARY.md``.

The serving protocol needs the per-row ``ANTENNA1``/``ANTENNA2``/``TIME``
columns (the server derives uvw and integration grouping from them), and the
MS's antenna rows must match the served bundle's antenna map — a MeerKAT
``m0xx`` set in the same row order.  Baselines the bundle does not know come
back with their generic flags only (non-finite visibilities).
"""

import numpy as np


class NNFlaggerParams:
    """Validated parameters for one ``nn-flagger`` operation.

    ``DEFAULTS`` is what the console summary compares against, so a run
    prints its server URL.  ``chan_freq_hz`` is filled in at run time from
    the MS (see ``DaskMS.flag_nn_flagger``).
    """

    __slots__ = ("server", "chan_freq_hz")

    DEFAULTS = {
        "server": None,
    }

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(
                f"unknown nn-flagger parameter(s): {', '.join(sorted(unknown))}."
                f" Valid parameters are {', '.join(sorted(self.DEFAULTS))}"
            )
        merged = dict(self.DEFAULTS)
        merged.update(kwargs)
        self.server = merged["server"]
        self.chan_freq_hz = None
        self._validate()

    def _validate(self):
        if not isinstance(self.server, str) or not self.server.strip():
            raise ValueError(
                "nn-flagger needs a server URL, e.g."
                " 'nn-flagger grpc://flagger.example:8815'"
            )
        self.server = self.server.strip()


def union_flags(existing, nn_flags):
    """OR the served model's mask into the block's flags.

    A union never clears a flag: everything already set stays set.
    """
    return np.logical_or(existing, nn_flags)
