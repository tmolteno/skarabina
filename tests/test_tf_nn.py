# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the ``tf-nn`` verb: :mod:`skarabina.tf_nn` and its grammar entry.

The combination is tested with a synthetic flagger standing in for the served
model, so recall/precision behaviour is exact and no network is involved; the
wire client is separately round-tripped against a fake Flight server.  The
dask block wiring is exercised with a monkeypatched model mask, keeping the
tests fast and deterministic.
"""
import numpy as np
import pytest

from skarabina.flag_ops import FlagOrderError, parse_entry
from skarabina.tf_nn import (
    MODES,
    TFCropNNParams,
    combine_flags,
    nn_block_mask,
)


# ---------------------------------------------------------------------------
# Parameters and grammar
# ---------------------------------------------------------------------------


class TestParams:
    def test_defaults(self):
        params = TFCropNNParams(server="grpc://host:8815")
        assert params.server == "grpc://host:8815"
        assert params.mode == "or"
        assert params.chan_freq_hz is None
        assert params.tfcrop is not None

    def test_server_is_required(self):
        with pytest.raises(ValueError, match="server URL"):
            TFCropNNParams()

    def test_bad_mode(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            TFCropNNParams(server="grpc://host:8815", mode="xor")

    def test_unknown_parameter(self):
        with pytest.raises(ValueError, match="unknown tf-nn parameter"):
            TFCropNNParams(server="grpc://host:8815", thresold=3)

    def test_server_is_stripped(self):
        params = TFCropNNParams(server="  grpc://host:8815  ")
        assert params.server == "grpc://host:8815"


class TestGrammar:
    def test_url_positional(self):
        op = parse_entry("tf-nn grpc://host:8815")
        assert op.verb == "tf-nn"
        assert op.args == ("server=grpc://host:8815",)

    def test_mode_positional(self):
        op = parse_entry("tf-nn grpc://host:8815 and")
        assert op.args == ("server=grpc://host:8815", "mode=and")

    def test_key_value_form(self):
        op = parse_entry("tf-nn server=grpc://host:8815 mode=and")
        assert op.args == ("server=grpc://host:8815", "mode=and")

    @pytest.mark.parametrize("alias", ["tf-nn", "tf_nn", "tfnn"])
    def test_aliases(self, alias):
        assert parse_entry(alias + " grpc://host:8815").verb == "tf-nn"

    def test_missing_server(self):
        with pytest.raises(FlagOrderError, match="server URL"):
            parse_entry("tf-nn")

    def test_two_urls(self):
        with pytest.raises(FlagOrderError, match="one server URL"):
            parse_entry("tf-nn grpc://a:1 grpc://b:2")

    def test_two_modes(self):
        with pytest.raises(FlagOrderError, match="mode given twice"):
            parse_entry("tf-nn grpc://a:1 and or")

    def test_bad_mode_is_an_error(self):
        with pytest.raises(FlagOrderError, match="mode must be one of"):
            parse_entry("tf-nn grpc://a:1 mode=xor")

    def test_describe(self):
        op = parse_entry("tf-nn grpc://host:8815")
        assert op.describe() == "tf-nn server=grpc://host:8815"


class TestDispatch:
    def test_run_dispatches_to_the_verb(self):
        from skarabina import flag_ops

        class StubMS:
            defer_reports = False

            def __init__(self):
                self.calls = []

            def flag_tf_nn(self, params):
                self.calls.append(params)

        ms = StubMS()
        ops = flag_ops.parse(["tf-nn grpc://host:8815 and"])
        assert flag_ops.run(ms, ops, log=lambda *a: None, flush=False) == 1
        (params,) = ms.calls
        assert params.server == "grpc://host:8815"
        assert params.mode == "and"


# ---------------------------------------------------------------------------
# The combination
# ---------------------------------------------------------------------------


class TestCombine:
    def setup_method(self):
        rng = np.random.default_rng(7)
        self.existing = rng.random((4, 5, 2)) < 0.1
        self.tfc = self.existing | (rng.random((4, 5, 2)) < 0.3)
        self.nn = rng.random((4, 5, 2)) < 0.3

    def test_or_is_the_union_of_new_flags(self):
        out = combine_flags(self.tfc, self.nn, self.existing, "or")
        assert np.array_equal(
            out, self.existing | (self.tfc & ~self.existing) | self.nn
        )

    def test_and_keeps_only_the_agreement(self):
        out = combine_flags(self.tfc, self.nn, self.existing, "and")
        expected = self.existing | (
            (self.tfc & ~self.existing) & (self.nn & ~self.existing)
        )
        assert np.array_equal(out, expected)

    def test_neither_mode_clears_existing_flags(self):
        for mode in MODES:
            out = combine_flags(self.tfc, self.nn, self.existing, mode)
            assert np.all(out[self.existing])

    def test_bad_mode(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            combine_flags(self.tfc, self.nn, self.existing, "xor")


# ---------------------------------------------------------------------------
# The served-model call, with a synthetic scorer standing in
# ---------------------------------------------------------------------------


class _FakeScorer:
    """Flags whole rows whose first sample amplitude exceeds a cutoff."""

    def __init__(self, cutoff):
        self.cutoff = cutoff
        self.calls = []

    def score(self, vis, time, antenna1, antenna2):
        self.calls.append(vis.shape[0])
        decision = np.abs(vis)[:, 0, 0] > self.cutoff
        return np.broadcast_to(decision[:, None, None], vis.shape)


class TestNNBlockMask:
    def _params(self):
        params = TFCropNNParams(server="grpc://unused:1")
        params.chan_freq_hz = np.linspace(1.2e9, 1.3e9, 8)
        return params

    def test_shape_and_decision(self, monkeypatch):
        import skarabina.tf_nn as tf_nn

        fake = _FakeScorer(2.0)
        monkeypatch.setattr(tf_nn, "_scorer", lambda params, n_chan, n_pol: fake)
        vis = np.zeros((6, 8, 2), dtype=np.complex64)
        vis[2, 0, 0] = 50.0
        out = nn_block_mask(
            vis, np.arange(6.0), np.zeros(6, np.int32), np.ones(6, np.int32),
            self._params(),
        )
        assert out.shape == (6, 8, 2)
        assert out.dtype == bool
        assert out[2].all()  # the spiked row, every sample
        assert not out[[0, 1, 3, 4, 5]].any()

    def test_block_is_one_scorer_call(self, monkeypatch):
        """Sub-batching is NNScorer's job (TestWire); a block is one call."""
        import skarabina.tf_nn as tf_nn

        fake = _FakeScorer(1e9)  # flags nothing
        monkeypatch.setattr(tf_nn, "_scorer", lambda params, n_chan, n_pol: fake)
        vis = np.zeros((10, 8, 2), dtype=np.complex64)
        out = nn_block_mask(
            vis, np.arange(10.0), np.zeros(10, np.int32), np.ones(10, np.int32),
            self._params(),
        )
        assert out.shape == (10, 8, 2)
        assert fake.calls == [10]

    def test_empty_block(self, monkeypatch):
        import skarabina.tf_nn as tf_nn

        fake = _FakeScorer(1.0)
        monkeypatch.setattr(tf_nn, "_scorer", lambda params, n_chan, n_pol: fake)
        vis = np.zeros((0, 8, 2), dtype=np.complex64)
        out = nn_block_mask(
            vis, np.zeros(0), np.zeros(0, np.int32), np.zeros(0, np.int32),
            self._params(),
        )
        assert out.shape == (0, 8, 2)
        assert fake.calls == []


# ---------------------------------------------------------------------------
# The wire client against a fake Flight server
# ---------------------------------------------------------------------------


class TestWire:
    @pytest.fixture
    def server(self):
        flight = pytest.importorskip("pyarrow.flight")
        nnflight = pytest.importorskip("skarabina.nnflight")
        import threading

        class FakeServer(flight.FlightServerBase):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.seen_batches = 0

            def do_exchange(self, context, descriptor, reader, writer):
                n_chan, n_pol = nnflight.parse_descriptor(descriptor.command)
                meta = nnflight.parse_descriptor_meta(descriptor.command)
                assert len(meta["chan_freq_hz"]) == 8
                # The production server (radio-nn nn_flag_server.flight) starts
                # its writer before reading; the responses are read back
                # interleaved with the requests by the client.
                writer.begin(nnflight.response_schema(n_chan, n_pol))
                for chunk in reader:
                    self.seen_batches += 1
                    req = nnflight.request_batch_to_arrays(chunk.data, n_chan, n_pol)
                    flags = np.abs(req["vis"]) > 2.0
                    writer.write_batch(
                        nnflight.make_response_batch(
                            n_chan, n_pol, req["row_ids"], flags
                        )
                    )
                writer.close()

        srv = FakeServer(flight.Location.for_grpc_tcp("127.0.0.1", 0))
        thread = threading.Thread(target=srv.serve, daemon=True)
        thread.start()
        yield srv, nnflight
        srv.shutdown()

    def test_round_trip(self, server):
        srv, nnflight = server
        rng = np.random.default_rng(11)
        vis = (
            rng.standard_normal((12, 8, 2)) + 1j * rng.standard_normal((12, 8, 2))
        ).astype(np.complex64)
        vis[7, 3, 1] = 50.0
        scorer = nnflight.NNScorer(
            "grpc://127.0.0.1:%d" % srv.port,
            8,
            2,
            chan_freq_hz=np.array([1.2e9, 1.3e9, 1.2e9, 1.3e9, 1.2e9, 1.3e9, 1.2e9, 1.3e9]),
        )
        out = scorer.score(
            vis, np.arange(12.0), np.zeros(12, np.int32), np.ones(12, np.int32)
        )
        assert out.shape == (12, 8, 2)
        assert out[7, 3, 1]
        assert srv.seen_batches == 1

    def test_sub_batched_round_trip(self, server, monkeypatch):
        srv, nnflight = server
        monkeypatch.setattr(nnflight, "NN_BATCH_ROWS", 5)
        rng = np.random.default_rng(12)
        vis = (
            rng.standard_normal((12, 8, 2)) + 1j * rng.standard_normal((12, 8, 2))
        ).astype(np.complex64)
        scorer = nnflight.NNScorer(
            "grpc://127.0.0.1:%d" % srv.port,
            8,
            2,
            chan_freq_hz=np.array([1.2e9, 1.3e9, 1.2e9, 1.3e9, 1.2e9, 1.3e9, 1.2e9, 1.3e9]),
        )
        out = scorer.score(
            vis, np.arange(12.0), np.zeros(12, np.int32), np.ones(12, np.int32)
        )
        assert srv.seen_batches == 3
        expected = np.abs(vis) > 2.0
        assert np.array_equal(out, expected)


# ---------------------------------------------------------------------------
# The dask block wiring
# ---------------------------------------------------------------------------


class TestBlock:
    def _rows(self, n_rows):
        return (
            np.zeros(n_rows, np.int32),
            np.ones(n_rows, np.int32),
            np.full(n_rows, -1, np.int32),
            np.arange(n_rows, dtype=np.float64),
        )

    def _data(self, n_rows=32, n_chan=8, n_pol=2, seed=5):
        rng = np.random.default_rng(seed)
        return (
            rng.standard_normal((n_rows, n_chan, n_pol))
            + 1j * rng.standard_normal((n_rows, n_chan, n_pol))
        ).astype(np.complex64)

    def test_block_needs_row_columns(self):
        pytest.importorskip("dask")
        from skarabina.dask_ms import _tf_nn_block

        data = self._data()
        existing = np.zeros(data.shape, dtype=bool)
        params = TFCropNNParams(server="grpc://unused:1")
        with pytest.raises(RuntimeError, match="ANTENNA1"):
            _tf_nn_block(data, existing, params, None)

    def test_block_combines_per_chunk(self, monkeypatch):
        pytest.importorskip("dask")
        import skarabina.dask_ms as dms
        from skarabina.dask_ms import _tf_nn_block, _tfcrop_block

        data = self._data()
        existing = np.zeros(data.shape, dtype=bool)
        rows = self._rows(data.shape[0])
        nn_mask = np.zeros(data.shape, dtype=bool)
        nn_mask[3:5] = True
        monkeypatch.setattr(
            dms, "nn_block_mask", lambda *args, **kwargs: nn_mask.copy()
        )
        params = TFCropNNParams(server="grpc://unused:1")

        tfc = _tfcrop_block(
            np.absolute(data), existing, params.tfcrop, rows[:3]
        )
        out = _tf_nn_block(data, existing, params, rows)
        assert np.array_equal(out, np.logical_or(tfc, nn_mask))

        params.mode = "and"
        out = _tf_nn_block(data, existing, params, rows)
        assert np.array_equal(
            out, np.logical_and(tfc, np.logical_or(nn_mask, existing))
        )
