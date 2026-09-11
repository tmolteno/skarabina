# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Contract tests for the `skarabina-analyze` stimela interface.

`skarabina/analyze.py` and the cargo cab in `cargo/skarabina_cargo/skarabina.yml`
are two halves of one interface: the CLI emits a single-line JSON record, and
the cab wrangles typed outputs out of it.  These tests keep the two in step
without importing analyze.py's heavy runtime dependencies (dask-ms, casacore).
"""

import ast
import re
from importlib import resources
from pathlib import Path

from omegaconf import OmegaConf

ANALYZE_PY = Path(__file__).resolve().parent.parent / "skarabina" / "analyze.py"


def _analyze_module_source() -> str:
    return ANALYZE_PY.read_text()


def _result_dict_keys(src: str) -> set:
    """Names of the keys in the `result = {...}` dict, via AST."""
    keys = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "result" in targets and isinstance(node.value, ast.Dict):
            keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    assert keys, "could not locate the `result` dict in skarabina/analyze.py"
    return keys


def _json_stdout_prefix(src: str) -> str:
    match = re.search(r'JSON_STDOUT_PREFIX\s*=\s*"([^"]*)"', src)
    assert match, "JSON_STDOUT_PREFIX not found in skarabina/analyze.py"
    return match.group(1)


def test_cli_emits_single_line_json():
    """--json-stdout must print one line: the prefix, then a JSON object.
    A multi-line payload would break stimela's line-by-line wrangling."""
    src = _analyze_module_source()
    assert "--json-stdout" in src, "analyze CLI is missing the --json-stdout option"
    prefix = _json_stdout_prefix(src)
    assert prefix and not prefix.endswith("\n")


def _schema_value_outputs():
    schemas = OmegaConf.load(resources.files("skarabina_cargo").joinpath("skarabina.yml"))
    cab = schemas.cabs["skarabina-analyze"]
    return {
        name
        for name, schema in cab.outputs.items()
        if not str(schema.get("dtype", "")).startswith(("File", "Directory", "MS"))
    }


def test_schema_outputs_exist_in_cli_json():
    """Every scalar output the cab exposes must be a key of the CLI's JSON
    record, otherwise the wrangler would silently never populate it."""
    keys = _result_dict_keys(_analyze_module_source())
    declared = _schema_value_outputs()
    assert declared <= keys, f"cab outputs absent from CLI result dict: {declared - keys}"


def test_cli_reports_all_schema_outputs():
    """The schema should expose the headline numbers; a missing one is a
    documentation/wiring regression rather than a crash."""
    keys = _result_dict_keys(_analyze_module_source())
    declared = _schema_value_outputs()
    for name in (
        "recommended_image_size_pixels",
        "resolution_arcsec",
        "max_baseline_m",
        "max_frequency_hz",
    ):
        assert name in declared, f"analyze cab should expose '{name}'"
        assert name in keys, f"CLI JSON should contain '{name}'"


def test_wrangler_pattern_contains_cli_prefix():
    """The cab's wrangler regex and the CLI's printed prefix are one contract."""
    schemas = OmegaConf.load(resources.files("skarabina_cargo").joinpath("skarabina.yml"))
    wranglers = schemas.cabs["skarabina-analyze"].management.wranglers
    patterns = [p for p, specs in wranglers.items() if "PARSE_JSON_OUTPUT_DICT" in list(specs)]
    assert patterns, "analyze cab must declare a PARSE_JSON_OUTPUT_DICT wrangler"
    prefix = _json_stdout_prefix(_analyze_module_source())
    assert any(prefix in p for p in patterns), (
        f"wrangler pattern does not contain the CLI prefix {prefix!r}"
    )
