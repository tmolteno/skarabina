# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The ordered ``--flag`` grammar, and what it costs.

The list is the run: every entry runs, exactly once, in the order written.  The
tests here pin the grammar and its error messages, and — because ordering means
more operations over the same data — that a sequence of operations still reads
the data column **once**, not once per operation.
"""
import pytest

from skarabina import flag_ops
from skarabina.flag_ops import FlagOrderError


# --- grammar -----------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("nan", [("nan", ())]),
        ("clip 0 100", [("clip", ("0", "100"))]),
        ("uv-above 2000", [("uv-above", ("2000",))]),
        ("uvabove 2000", [("uv-above", ("2000",))]),
        ("uv_above 2000", [("uv-above", ("2000",))]),
        ("autos", [("autos", ())]),
        ("spectral-window r.yml", [("spectral-window", ("r.yml",))]),
        ("save:before", [("save", ("before",))]),
        ("restore:orig", [("restore", ("orig",))]),
        ("nan, clip 0 100", [("nan", ()), ("clip", ("0", "100"))]),
        (
            "save:before, uv-above 2000, save:after",
            [("save", ("before",)), ("uv-above", ("2000",)), ("save", ("after",))],
        ),
    ],
)
def test_parse_entries(spec, expected):
    got = [(o.verb, o.args) for o in flag_ops.parse([spec])]
    assert got == expected


def test_multiple_occurrences_concatenate_in_order():
    ops = flag_ops.parse(["save:first", "nan", "clip 0 1"])
    assert [o.describe() for o in ops] == ["save:first", "nan", "clip 0 1"]


def test_yaml_list_form():
    ops = flag_ops.parse(['["nan", "clip 0 100"]'])
    assert [o.describe() for o in ops] == ["nan", "clip 0 100"]


def test_commas_inside_brackets_and_quotes_are_not_separators():
    ops = flag_ops.parse(["spectral-window 'my rules.yml', nan"])
    assert [o.describe() for o in ops] == ["spectral-window my rules.yml", "nan"]


@pytest.mark.parametrize(
    "bad,why",
    [
        ("barber", "barber is not a flagging operation"),
        ("clip 100", "clip needs two values"),
        ("clip 0", "clip needs two values"),
        ("clip a b", "clip values must be numbers"),
        ("uv-above", "uv-above needs one value"),
        ("uv-above fast", "uv-above value must be a number"),
        ("spectral-window", "needs one rule file"),
        ("nan 5", "nan takes no arguments"),
        ("autos 1", "autos takes no arguments"),
        ("nope", "unknown verb"),
        ("save:", "needs a version name"),
        ("save:a b", "takes no further arguments"),
        ("save:a/b", "path separator"),
        ("", "empty entry"),
        ("nan,,clip", "empty entry"),
    ],
)
def test_parse_errors(bad, why):
    with pytest.raises(FlagOrderError) as exc:
        flag_ops.parse([bad])
    assert why in exc.value.message


def test_barber_error_points_at_the_separate_option():
    """barber is rejected, and the message says what to use instead."""
    with pytest.raises(FlagOrderError) as exc:
        flag_ops.parse(["barber"])
    message = exc.value.message
    assert "does not write FLAG" in message
    assert "--barber" in message


def test_empty_spec_list_is_no_operations():
    assert flag_ops.parse([]) == []
    assert flag_ops.parse(None) == []


# --- --flag-file -------------------------------------------------------------


def test_flag_file_plain_text_with_comments(tmp_path):
    path = tmp_path / "flags.txt"
    path.write_text("# leading comment\nnan\n\nclip 0 100  # trailing\nsave:x\n")
    entries = flag_ops.load_file(path)
    assert entries == ["nan", "clip 0 100", "save:x"]


def test_flag_file_yaml_list(tmp_path):
    path = tmp_path / "flags.yml"
    path.write_text("- nan\n- clip 0 100\n")
    assert flag_ops.load_file(path) == ["nan", "clip 0 100"]


def test_flag_file_rejects_a_mapping(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("nan: true\n")
    with pytest.raises(FlagOrderError):
        flag_ops.load_file(path)


# --- ordering ----------------------------------------------------------------


def test_describe_names_the_operation_as_written():
    (op,) = flag_ops.parse(["uv-above 2000"])
    assert op.describe() == "uv-above 2000"
    (marker,) = flag_ops.parse(["save:before"])
    assert marker.describe() == "save:before"
    assert marker.name == "before"
    assert marker.is_marker


def test_order_is_the_list_order():
    ops = flag_ops.parse(["clip 0 100", "nan"])
    assert [o.verb for o in ops] == ["clip", "nan"]


def test_reversing_two_verbs_is_possible():
    """The order must be real: unlike the old flagger, nan and clip swap."""
    forward = [o.verb for o in flag_ops.parse(["nan, clip 0 100"])]
    reverse = [o.verb for o in flag_ops.parse(["clip 0 100, nan"])]
    assert forward == ["nan", "clip"]
    assert reverse == ["clip", "nan"]


def test_operations_may_repeat():
    ops = flag_ops.parse(["spectral-window a.yml, nan, spectral-window b.yml"])
    assert [o.verb for o in ops] == ["spectral-window", "nan", "spectral-window"]


# --- cost: one pass over DATA ------------------------------------------------


def _counting_scheduler(counter):
    """A dask scheduler that counts executions of DATA-loading tasks."""
    import dask.local

    real = dask.local.get_sync

    def get(dsk, out, **kwargs):
        for key in dsk:
            name = str(key)
            if "DATA" in name and "FLAG" not in name:
                counter[0] += 1
        return real(dsk, out, **kwargs)

    return get


def _run(ms_path, deferred):
    import dask

    from skarabina.dask_ms import DaskMS

    ds = DaskMS(ms_path)
    counter = [0]
    with dask.config.set(scheduler=_counting_scheduler(counter)):
        if deferred:
            sink = {}
            ds.flag_data({"NAN": True}, defer=sink)
            ds.flag_data({"CLIP": (0.0, 5.0)}, defer=sink)
            ds.report_data_flags(sink)
        else:
            ds.flag_data({"NAN": True})
            ds.flag_data({"CLIP": (0.0, 5.0)})
    return counter[0]


def test_two_data_operations_read_data_once(tmp_path):
    """The point of deferring: N operations must not mean N passes over DATA.

    Statistics computed per operation make each one its own compute, so DATA is
    loaded twice.  Consolidated into one compute, dask shares the single
    ``abs(DATA)`` subgraph and the column is loaded once.
    """
    from ms_fixture import make_synthetic_ms

    path = str(tmp_path / "seq.ms")
    make_synthetic_ms(path, nchan=16, ncorr=2, nrow=2000)

    deferred = _run(path, deferred=True)
    eager = _run(path, deferred=False)
    assert deferred == 1, f"deferred sequence should load DATA once, got {deferred}"
    assert eager > deferred, (
        "the eager path is expected to cost more passes; if it does not, this"
        " test is no longer measuring what it claims"
    )


def test_tfcrop_parameters_are_key_value_pairs():
    """tfcrop takes CASA's parameter names, in any order, any subset."""
    assert flag_ops.parse(["tfcrop"])[0].args == ()
    ops = flag_ops.parse(["tfcrop timecutoff=5 freqcutoff=2.5"])
    assert ops[0].verb == "tfcrop"
    assert ops[0].args == ("timecutoff=5", "freqcutoff=2.5")
    # the bracketed list form is equivalent
    assert flag_ops.parse(["tfcrop [timecutoff=5, freqcutoff=2.5]"])[0].args == \
        ops[0].args
    # A comma between tfcrop parameters needs the brackets: at the top level a
    # comma separates --flag *entries*, so 'tfcrop a=1, b=2' is two entries and
    # the second is not a verb.  That is the grammar working, not a gap.
    with pytest.raises(FlagOrderError, match="unknown verb"):
        flag_ops.parse(["tfcrop timecutoff=5, freqcutoff=2.5"])


def test_tfcrop_accepts_stimela_escaped_brackets():
    """Stimela escapes ``[`` and ``]`` when passing a parameter to a container.

    A bracketed entry therefore arrives as ``\\[...\\]``, and without handling
    that the brackets survive into the parameter names and the cab rejects a
    recipe that works perfectly on the command line -- in the one situation the
    brackets were introduced for.
    """
    expected = ("timecutoff=5", "freqcutoff=2.5")
    for spec in (
        r"tfcrop \[timecutoff=5, freqcutoff=2.5\]",
        r"nan, tfcrop \[timecutoff=5, freqcutoff=2.5\], clip 0 100",
    ):
        ops = [op for op in flag_ops.parse([spec]) if op.verb == "tfcrop"]
        assert len(ops) == 1, spec
        assert ops[0].args == expected, (spec, ops[0].args)


def test_tfcrop_rejects_a_bad_parameter_name_or_value():
    """A typo must be an error, not a silently ignored setting."""
    with pytest.raises(Exception, match="maxnpices"):
        flag_ops.parse(["tfcrop maxnpices=3"])
    with pytest.raises(Exception, match="maxnpieces"):
        flag_ops.parse(["tfcrop maxnpieces=99"])
    with pytest.raises(Exception, match="key=value"):
        flag_ops.parse(["tfcrop timecutoff"])
    with pytest.raises(Exception, match="flagdimension"):
        flag_ops.parse(["tfcrop flagdimension=diagonal"])


def test_tfcrop_runs_through_the_dispatcher(tmp_path):
    """`run` must dispatch tfcrop to DaskMS.flag_tfcrop with parsed params."""
    from ms_fixture import make_synthetic_ms

    from skarabina.dask_ms import DaskMS

    path = str(tmp_path / "tfcrop.ms")
    make_synthetic_ms(path, nchan=32, ncorr=1, nrow=40)
    ms = DaskMS(path)
    ops = flag_ops.parse(["tfcrop [timecutoff=4, freqcutoff=3, maxnpieces=2]"])
    flag_ops.run(ms, ops, log=lambda *_: None)
    import numpy as np
    assert np.asarray(ms.ds.FLAG.data).any(), "the verb flagged nothing"


def test_run_reports_every_operation_in_order(tmp_path):
    """flag_ops.run announces each entry, in the order it executes."""
    from ms_fixture import make_synthetic_ms
    from skarabina.dask_ms import DaskMS

    path = str(tmp_path / "order.ms")
    make_synthetic_ms(path, nchan=4, ncorr=1, nrow=8)

    seen = []
    ops = flag_ops.parse(["uv-above 1000, clip 0 100, nan"])
    flag_ops.run(DaskMS(path), ops, log=seen.append)
    assert seen == ["uv-above 1000", "clip 0 100", "nan"]
