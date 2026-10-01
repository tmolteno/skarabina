# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the ``nn-flagger`` verb: :mod:`skarabina.nn_flagger` and its entry.

The verb is union-only by construction (an intersection would clear flags an
earlier verb set), so the tests pin that contract: the served model's mask is
ORed in, pre-existing flags always survive, and the grammar takes exactly one
server URL.  The dask block wiring is exercised with a monkeypatched model
mask, keeping the tests fast and deterministic.
"""
import numpy as np
import pytest

from skarabina.flag_ops import FlagOrderError, parse_entry
from skarabina.nn_flagger import NNFlaggerParams, union_flags


class TestParams:
    def test_defaults(self):
        params = NNFlaggerParams(server="grpc://host:8815")
        assert params.server == "grpc://host:8815"
        assert params.chan_freq_hz is None

    def test_server_is_required(self):
        with pytest.raises(ValueError, match="server URL"):
            NNFlaggerParams()

    def test_unknown_parameter(self):
        with pytest.raises(ValueError, match="unknown nn-flagger parameter"):
            NNFlaggerParams(server="grpc://host:8815", mode="or")

    def test_server_is_stripped(self):
        params = NNFlaggerParams(server="  grpc://host:8815  ")
        assert params.server == "grpc://host:8815"


class TestGrammar:
    def test_url_positional(self):
        op = parse_entry("nn-flagger grpc://host:8815")
        assert op.verb == "nn-flagger"
        assert op.args == ("server=grpc://host:8815",)

    def test_key_value_form(self):
        op = parse_entry("nn-flagger server=grpc://host:8815")
        assert op.args == ("server=grpc://host:8815",)

    @pytest.mark.parametrize("alias", ["nn-flagger", "nn_flagger", "nnflagger"])
    def test_aliases(self, alias):
        assert parse_entry(alias + " grpc://host:8815").verb == "nn-flagger"

    def test_missing_server(self):
        with pytest.raises(FlagOrderError, match="server URL"):
            parse_entry("nn-flagger")

    def test_two_urls(self):
        with pytest.raises(FlagOrderError, match="one server URL"):
            parse_entry("nn-flagger grpc://a:1 grpc://b:2")

    def test_composes_after_tfcrop(self):
        from skarabina.flag_ops import parse

        ops = parse(["tfcrop [timecutoff=5], nn-flagger grpc://host:8815"])
        assert [op.verb for op in ops] == ["tfcrop", "nn-flagger"]
        assert ops[0].args == ("timecutoff=5",)

    def test_describe(self):
        op = parse_entry("nn-flagger grpc://host:8815")
        assert op.describe() == "nn-flagger server=grpc://host:8815"


class TestUnion:
    def test_or_in_the_model_flags(self):
        existing = np.zeros((3, 4, 2), dtype=bool)
        existing[0, 0, 0] = True
        nn = np.zeros((3, 4, 2), dtype=bool)
        nn[1, 2, 1] = True
        out = union_flags(existing, nn)
        assert out[0, 0, 0] and out[1, 2, 1]
        assert out.sum() == 2

    def test_never_clears_existing_flags(self):
        rng = np.random.default_rng(5)
        existing = rng.random((3, 4, 2)) < 0.3
        nn = rng.random((3, 4, 2)) < 0.3
        out = union_flags(existing, nn)
        assert np.all(out[existing])
        assert np.array_equal(out, existing | nn)


class TestBlock:
    def _rows(self, n_rows):
        return (
            np.zeros(n_rows, np.int32),
            np.ones(n_rows, np.int32),
            np.full(n_rows, -1, np.int32),
            np.arange(n_rows, dtype=np.float64),
        )

    def test_block_needs_row_columns(self):
        pytest.importorskip("dask")
        from skarabina.dask_ms import _nn_flagger_block

        data = np.zeros((4, 8, 2), dtype=np.complex64)
        existing = np.zeros(data.shape, dtype=bool)
        params = NNFlaggerParams(server="grpc://unused:1")
        with pytest.raises(RuntimeError, match="ANTENNA1"):
            _nn_flagger_block(data, existing, params, None)

    def test_block_unions_per_chunk(self, monkeypatch):
        pytest.importorskip("dask")
        import skarabina.dask_ms as dms
        from skarabina.dask_ms import _nn_flagger_block

        data = np.zeros((8, 8, 2), dtype=np.complex64)
        existing = np.zeros(data.shape, dtype=bool)
        existing[0, 0, 0] = True
        rows = self._rows(8)
        nn_mask = np.zeros(data.shape, dtype=bool)
        nn_mask[3:5] = True
        monkeypatch.setattr(
            dms, "nn_block_mask", lambda *args, **kwargs: nn_mask.copy()
        )
        params = NNFlaggerParams(server="grpc://unused:1")
        out = _nn_flagger_block(data, existing, params, rows)
        assert np.array_equal(out, existing | nn_mask)


class TestDispatch:
    def test_run_dispatches_to_the_verb(self):
        from skarabina import flag_ops

        class StubMS:
            defer_reports = False

            def __init__(self):
                self.calls = []

            def flag_nn_flagger(self, params):
                self.calls.append(params)

        ms = StubMS()
        ops = flag_ops.parse(["nn-flagger grpc://host:8815"])
        assert flag_ops.run(ms, ops, log=lambda *a: None, flush=False) == 1
        (params,) = ms.calls
        assert params.server == "grpc://host:8815"
