# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Arrow Flight client for a served neural flagger (the ``tf-nn`` verb).

The wire protocol is **vendored** from the radio-nn serving tree
(``production/common/schema.py``, ``PROTOCOL_VERSION = 1``) so skarabina can
talk to ``nn-flag-server`` without depending on that repository.  Keep the
two copies in sync: the server rejects any other protocol version.

Request batch (client -> server)::

    ROW_ID     int64                 per-row ids, echoed back in the response
    TIME       float64               per-row time (the server derives uvw and
                                     integration grouping from it)
    ANTENNA1   int32                 MS antenna-row indices of each baseline
    ANTENNA2   int32
    DATA       fixed_size_list<float32>[2 * n_chan * n_pol]
                                     per-row visibilities as interleaved
                                     (real, imag) float32 pairs, row-major
                                     over (chan, pol)
    WEIGHT     fixed_size_list<float32>[n_chan * n_pol]
                                     per-row channel weight spectrum

Response batch (server -> client)::

    ROW_ID     int64                 echo of request row ids
    FLAG       fixed_size_list<bool>[n_chan * n_pol]
                                     per-row flag mask, row-major over
                                     (chan, pol); True == flag this sample

The stream descriptor is a JSON command naming the shape (and optionally the
per-channel frequencies); see :func:`descriptor_bytes`.

This module deliberately depends only on numpy and (lazily) pyarrow, so a
client can be exercised against a live server without the dask/ms stack.
"""
from __future__ import annotations

import json
import threading

import numpy as np

FLAG_CMD = "flag_visibilities"
PROTOCOL_VERSION = 1

# Field names used on the wire.
ROW_ID = "ROW_ID"
TIME = "TIME"
ANTENNA1 = "ANTENNA1"
ANTENNA2 = "ANTENNA2"
DATA = "DATA"
WEIGHT = "WEIGHT"
FLAG = "FLAG"

#: Rows per exchange.  Bounds the transient arrow buffers of one round trip
#: however big the block; the server answers each request batch in order.
NN_BATCH_ROWS = 2048


class ProtocolError(ValueError):
    """Raised when a message does not match the flagging wire protocol."""


def _flight():
    """The pyarrow.flight module, or a clear error naming the extra."""
    try:
        import pyarrow.flight as flight
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise ImportError(
            "the tf-nn verb needs pyarrow to speak Arrow Flight:"
            " pip install 'skarabina[nn]'"
        ) from exc
    return flight


# ---------------------------------------------------------------------------
# Flight descriptor
# ---------------------------------------------------------------------------


def descriptor_bytes(n_chan, n_pol, chan_freq_hz=None) -> bytes:
    """Encode the stream command: {cmd, version, n_chan, n_pol[, chan_freq_hz]}.

    ``chan_freq_hz`` (optional) is the per-channel centre frequency in Hz —
    needed by flaggers whose per-channel uvw phase coordinates derive from
    frequency (the served MeerKAT models are).
    """
    _check_shape(n_chan, n_pol)
    payload = {
        "cmd": FLAG_CMD,
        "version": PROTOCOL_VERSION,
        "n_chan": int(n_chan),
        "n_pol": int(n_pol),
    }
    if chan_freq_hz is not None:
        payload["chan_freq_hz"] = [float(f) for f in chan_freq_hz]
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def parse_descriptor_meta(command: bytes) -> dict:
    """Full descriptor payload as a dict (cmd, version, n_chan, n_pol, ...)."""
    try:
        meta = json.loads(command.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"descriptor command is not valid JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise ProtocolError("descriptor command must be a JSON object")
    return meta


def parse_descriptor(command: bytes) -> tuple:
    """Decode and validate a stream descriptor command.

    Returns ``(n_chan, n_pol)``.  Raises :class:`ProtocolError` on any
    mismatch.
    """
    meta = parse_descriptor_meta(command)
    if meta.get("cmd") != FLAG_CMD:
        raise ProtocolError(
            f"unknown command {meta.get('cmd')!r} (expected {FLAG_CMD!r})"
        )
    version = meta.get("version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"protocol version {version!r} unsupported (server speaks "
            f"{PROTOCOL_VERSION})"
        )
    try:
        n_chan, n_pol = int(meta["n_chan"]), int(meta["n_pol"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(
            f"descriptor must carry integer n_chan/n_pol, got {meta!r}"
        ) from exc
    _check_shape(n_chan, n_pol)
    return n_chan, n_pol


# ---------------------------------------------------------------------------
# Schemas and batches
# ---------------------------------------------------------------------------


def _check_shape(n_chan, n_pol) -> None:
    if not isinstance(n_chan, (int, np.integer)) or n_chan < 1:
        raise ProtocolError(f"n_chan must be a positive int, got {n_chan!r}")
    if not isinstance(n_pol, (int, np.integer)) or n_pol < 1:
        raise ProtocolError(f"n_pol must be a positive int, got {n_pol!r}")


def _as(arr, dtype, name):
    try:
        return np.ascontiguousarray(arr, dtype=dtype)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"cannot interpret {name} as {dtype}: {exc}") from exc


def _require(batch, name, pa_type):
    try:
        field = batch.schema.field(name)
    except KeyError as exc:  # pyarrow raises KeyError for missing fields
        raise ProtocolError(
            f"batch is missing column {name!r} (got {batch.schema.names})"
        ) from exc
    if field.type != pa_type:
        raise ProtocolError(
            f"column {name!r} has arrow type {field.type}, expected {pa_type}"
        )
    return batch.column(name)


def _pa():
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise ImportError(
            "the tf-nn verb needs pyarrow to speak Arrow Flight:"
            " pip install 'skarabina[nn]'"
        ) from exc
    return pa


def request_schema(n_chan, n_pol):
    """Arrow schema of a request (visibility) RecordBatch."""
    pa = _pa()
    _check_shape(n_chan, n_pol)
    n_samples = n_chan * n_pol
    return pa.schema(
        [
            pa.field(ROW_ID, pa.int64(), nullable=False),
            pa.field(TIME, pa.float64(), nullable=False),
            pa.field(ANTENNA1, pa.int32(), nullable=False),
            pa.field(ANTENNA2, pa.int32(), nullable=False),
            pa.field(DATA, pa.list_(pa.float32(), 2 * n_samples), nullable=False),
            pa.field(WEIGHT, pa.list_(pa.float32(), n_samples), nullable=False),
        ]
    )


def response_schema(n_chan, n_pol):
    """Arrow schema of a response (flag mask) RecordBatch."""
    pa = _pa()
    _check_shape(n_chan, n_pol)
    n_samples = n_chan * n_pol
    return pa.schema(
        [
            pa.field(ROW_ID, pa.int64(), nullable=False),
            pa.field(FLAG, pa.list_(pa.bool_(), n_samples), nullable=False),
        ]
    )


def make_request_batch(
    n_chan, n_pol, row_ids, time, antenna1, antenna2, vis, weight
):
    """Build a request RecordBatch from one chunk of rows.

    ``vis`` is complex64/complex128 with shape (n_rows, n_chan, n_pol);
    ``weight`` float32 (n_rows, n_chan, n_pol).  Values are validated and
    coerced to the wire types; input arrays are not modified.
    """
    pa = _pa()
    _check_shape(n_chan, n_pol)
    row_ids = _as(row_ids, np.int64, "row_ids")
    time = _as(time, np.float64, "time")
    antenna1 = _as(antenna1, np.int32, "antenna1")
    antenna2 = _as(antenna2, np.int32, "antenna2")
    vis = np.ascontiguousarray(vis, dtype=np.complex64)
    weight = np.ascontiguousarray(weight, dtype=np.float32)
    n_rows = len(row_ids)
    if vis.shape != (n_rows, n_chan, n_pol):
        raise ProtocolError(
            f"vis has shape {vis.shape}, expected {(n_rows, n_chan, n_pol)}"
        )
    if weight.shape != (n_rows, n_chan, n_pol):
        raise ProtocolError(
            f"weight has shape {weight.shape}, expected {(n_rows, n_chan, n_pol)}"
        )
    for name, arr, n in (
        ("time", time, n_rows),
        ("antenna1", antenna1, n_rows),
        ("antenna2", antenna2, n_rows),
    ):
        if arr.shape != (n,):
            raise ProtocolError(f"{name} has shape {arr.shape}, expected ({n},)")

    # Interleave (real, imag) float32 pairs: complex64 memory layout is
    # [re, im] per element, so a reinterpret view does exactly this.
    vis_pairs = vis.view(np.float32).reshape(n_rows, 2 * n_chan * n_pol)

    schema = request_schema(n_chan, n_pol)
    return pa.RecordBatch.from_arrays(
        [
            pa.array(row_ids, type=pa.int64()),
            pa.array(time, type=pa.float64()),
            pa.array(antenna1, type=pa.int32()),
            pa.array(antenna2, type=pa.int32()),
            pa.FixedSizeListArray.from_arrays(
                pa.array(vis_pairs.ravel(), type=pa.float32()),
                list_size=2 * n_chan * n_pol,
            ),
            pa.FixedSizeListArray.from_arrays(
                pa.array(weight.ravel(), type=pa.float32()),
                list_size=n_chan * n_pol,
            ),
        ],
        schema=schema,
    )


def request_batch_to_arrays(batch, n_chan, n_pol):
    """Parse a request batch into numpy arrays (for tests and fake servers).

    Returns ``row_ids`` (int64), ``time`` (float64), ``antenna1`` /
    ``antenna2`` (int32), ``vis`` (complex64, shape (n, n_chan, n_pol)) and
    ``weight`` (float32, same shape).
    """
    # A do_exchange reader yields FlightStreamChunk objects (data plus
    # metadata); accept either them or bare RecordBatches.
    batch = getattr(batch, "data", batch)
    _check_shape(n_chan, n_pol)
    n_samples = n_chan * n_pol
    row_ids = _require(batch, ROW_ID, _pa().int64()).to_numpy()
    time = _require(batch, TIME, _pa().float64()).to_numpy()
    antenna1 = _require(batch, ANTENNA1, _pa().int32()).to_numpy()
    antenna2 = _require(batch, ANTENNA2, _pa().int32()).to_numpy()

    data_list = _require(batch, DATA, _pa().list_(_pa().float32(), 2 * n_samples))
    weight_list = _require(batch, WEIGHT, _pa().list_(_pa().float32(), n_samples))
    n_rows = len(batch)
    if data_list.null_count or weight_list.null_count:
        raise ProtocolError("DATA/WEIGHT columns must not contain nulls")

    pairs = np.ascontiguousarray(
        data_list.values.to_numpy(zero_copy_only=False)
    )
    if pairs.size != n_rows * 2 * n_samples:
        raise ProtocolError(
            f"DATA flat length {pairs.size} != {n_rows} x {2 * n_samples}"
        )
    vis = pairs.view(np.complex64).reshape(n_rows, n_chan, n_pol)

    wflat = np.ascontiguousarray(
        weight_list.values.to_numpy(zero_copy_only=False)
    )
    if wflat.size != n_rows * n_samples:
        raise ProtocolError(
            f"WEIGHT flat length {wflat.size} != {n_rows} x {n_samples}"
        )
    weight = wflat.reshape(n_rows, n_chan, n_pol)

    return {
        "row_ids": row_ids,
        "time": time,
        "antenna1": antenna1,
        "antenna2": antenna2,
        "vis": vis,
        "weight": weight,
    }


def make_response_batch(n_chan, n_pol, row_ids, flags):
    """Build a response RecordBatch (for tests and fake servers).

    ``flags`` is boolean with shape (n_rows, n_chan, n_pol); True marks a
    sample to be flagged.
    """
    pa = _pa()
    _check_shape(n_chan, n_pol)
    row_ids = _as(row_ids, np.int64, "row_ids")
    flags = np.ascontiguousarray(flags, dtype=bool)
    n_rows = len(row_ids)
    if flags.shape != (n_rows, n_chan, n_pol):
        raise ProtocolError(
            f"flags has shape {flags.shape}, expected {(n_rows, n_chan, n_pol)}"
        )
    schema = response_schema(n_chan, n_pol)
    return pa.RecordBatch.from_arrays(
        [
            pa.array(row_ids, type=pa.int64()),
            pa.FixedSizeListArray.from_arrays(
                pa.array(flags.ravel(), type=pa.bool_()),
                list_size=n_chan * n_pol,
            ),
        ],
        schema=schema,
    )


def response_batch_to_arrays(batch, n_chan, n_pol):
    """Parse a response batch into ``(row_ids, flags)`` numpy arrays.

    ``flags`` comes back boolean with shape (n_rows, n_chan, n_pol).
    """
    _check_shape(n_chan, n_pol)
    n_samples = n_chan * n_pol
    row_ids = _require(batch, ROW_ID, _pa().int64()).to_numpy()
    flag_list = _require(batch, FLAG, _pa().list_(_pa().bool_(), n_samples))
    if flag_list.null_count:
        raise ProtocolError("FLAG column must not contain nulls")
    n_rows = len(batch)
    fflat = np.ascontiguousarray(
        flag_list.values.to_numpy(zero_copy_only=False)
    )
    if fflat.size != n_rows * n_samples:
        raise ProtocolError(
            f"FLAG flat length {fflat.size} != {n_rows} x {n_samples}"
        )
    flags = fflat.reshape(n_rows, n_chan, n_pol)
    return row_ids, flags


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_local = threading.local()


def _client(server):
    """A FlightClient per thread and server URL.

    Blocks may be evaluated concurrently (dask's threaded scheduler), and a
    gRPC channel is not shared across threads; thread-local reuse keeps one
    connection per worker thread instead of one per block.
    """
    clients = getattr(_local, "clients", None)
    if clients is None:
        clients = _local.clients = {}
    client = clients.get(server)
    if client is None:
        client = _flight().FlightClient(server)
        clients[server] = client
    return client


class NNScorer:
    """Flags blocks of visibilities with a remote ``nn-flag-server``.

    One ``score`` call streams the block to the server in :data:`NN_BATCH_ROWS`
    exchanges and returns the server's flag mask.  The server thresholds its
    own probabilities (the served bundle's ``flag_threshold``); what comes back
    is a decision, not a score.
    """

    def __init__(self, server, n_chan, n_pol, chan_freq_hz=None):
        _check_shape(n_chan, n_pol)
        self.server = server
        self.n_chan = int(n_chan)
        self.n_pol = int(n_pol)
        self.chan_freq_hz = chan_freq_hz

    def score(self, vis, time, antenna1, antenna2):
        """Flag one block; returns bool (n_rows, n_chan, n_pol).

        Row ids are the block-local positions — the protocol only requires
        that they round-trip, and the response is checked against them.  One
        exchange stream serves the whole block, with a write/read pair per
        :data:`NN_BATCH_ROWS` batch (the interleaving is required: closing
        the write side before the responses are read tears the stream down).
        """
        vis = np.ascontiguousarray(vis, dtype=np.complex64)
        n_rows = vis.shape[0]
        if n_rows == 0:
            return np.zeros((0, self.n_chan, self.n_pol), dtype=bool)
        out = np.zeros((n_rows, self.n_chan, self.n_pol), dtype=bool)
        ids = np.arange(n_rows, dtype=np.int64)
        flight = _flight()
        descriptor = flight.FlightDescriptor.for_command(
            descriptor_bytes(self.n_chan, self.n_pol, chan_freq_hz=self.chan_freq_hz)
        )
        writer, reader = _client(self.server).do_exchange(descriptor)
        try:
            writer.begin(request_schema(self.n_chan, self.n_pol))
            for start in range(0, n_rows, NN_BATCH_ROWS):
                stop = min(start + NN_BATCH_ROWS, n_rows)
                batch = make_request_batch(
                    self.n_chan,
                    self.n_pol,
                    ids[start:stop],
                    time[start:stop],
                    antenna1[start:stop],
                    antenna2[start:stop],
                    vis[start:stop],
                    np.ones(vis[start:stop].shape, dtype=np.float32),
                )
                writer.write_batch(batch)
                out[start:stop] = self._read(reader, ids[start:stop])
        finally:
            writer.close()
        return out

    def _read(self, reader, expect_ids):
        try:
            response = reader.read_chunk()
        except StopIteration:
            raise ProtocolError("server closed the stream without a response")
        response = getattr(response, "data", response)
        got_ids, flags = response_batch_to_arrays(
            response, self.n_chan, self.n_pol
        )
        if not np.array_equal(got_ids, expect_ids):
            raise ProtocolError(
                "server echoed row ids out of order or wrong:"
                f" expected [{expect_ids[0]}..{expect_ids[-1]}],"
                f" got [{got_ids[0]}..{got_ids[-1]}]"
            )
        return flags
