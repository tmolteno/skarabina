# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import json
import re
from importlib import resources
from pathlib import Path

import pytest
from omegaconf import OmegaConf

EXAMPLE_RECIPE = Path(__file__).resolve().parent.parent / "examples" / "skarabina-demo-pipeline.yml"


@pytest.fixture(scope="module")
def schemas():
    recipe = resources.files("skarabina_cargo").joinpath("skarabina.yml")
    return OmegaConf.load(recipe)


def test_schema_loads(schemas):
    """The stimela cab schema should load and define the skarabina cab."""
    cab = schemas.cabs.get("skarabina")
    assert cab is not None
    inputs = cab.inputs
    for key in ("ms", "summary", "barber", "apply", "clobber"):
        assert key in inputs, f"missing input '{key}' in schema"


def test_selection_inputs_are_exposed(schemas):
    """Row selection must be reachable from a recipe: the white-belt pipeline
    keeps a subset of scans, and per-target MSs are written with `split`."""
    inputs = schemas.cabs["skarabina"].inputs
    for key in ("scan", "split"):
        assert key in inputs, f"missing input '{key}' in schema"
    assert "scan" in str(inputs["scan"].get("info", "")).lower()


def test_no_unimplemented_outputs_are_declared(schemas):
    """The cab must only advertise outputs the CLI can actually produce.

    `reference-antenna` and `max-uv` were declared for a long time but nothing
    ever populated them, so a recipe binding to them silently got nothing."""
    outputs = set(schemas.cabs["skarabina"].outputs or {})
    assert outputs == {"msout"}, f"unexpected skarabina cab outputs: {outputs}"


def test_no_input_output_name_collisions(schemas):
    """Stimela forbids a name appearing in both inputs and outputs."""
    for cab_name, cab in schemas.cabs.items():
        inputs = set(cab.get("inputs") or {})
        outputs = set(cab.get("outputs") or {})
        assert not inputs & outputs, (
            f"cab '{cab_name}': {inputs & outputs} appears in both inputs and outputs"
        )


def _is_file_type(schema) -> bool:
    return str(schema.get("dtype", "")).startswith(("File", "Directory", "MS"))


def _value_outputs(cab):
    """Named file outputs are fed on the command line; value outputs are
    populated by a wrangler."""
    return {name: out for name, out in cab.outputs.items() if not _is_file_type(out)}


def test_analyze_value_outputs_are_valid_identifiers(schemas):
    """PARSE_JSON_OUTPUT_DICT assigns JSON keys directly to output names, so
    every value-type output must be a valid Python identifier.  A kebab-case
    name such as 'image-size' would never be populated."""
    value_outputs = _value_outputs(schemas.cabs["skarabina-analyze"])
    assert value_outputs, "expected scalar outputs on the analyze cab"
    for name in value_outputs:
        assert name.isidentifier(), f"value output '{name}' is not a valid Python identifier"


def _wrangler_pattern(schemas):
    """The regex the analyze cab uses to spot the --json-stdout line."""
    wranglers = schemas.cabs["skarabina-analyze"].management.wranglers
    patterns = [p for p, specs in wranglers.items() if "PARSE_JSON_OUTPUT_DICT" in list(specs)]
    assert patterns, "analyze cab must wrangle its JSON via PARSE_JSON_OUTPUT_DICT"
    (pattern,) = patterns
    return pattern


def _parse_json_output_dict(pattern, line):
    """Reimplement stimela's PARSE_JSON_OUTPUT_DICT semantics: search the line
    for the pattern and json.loads() the first ()-group."""
    match = re.search(pattern, line)
    assert match, f"wrangler pattern {pattern!r} did not match line {line!r}"
    return json.loads(match.group(1))


def _sample_analyze_stdout(schemas):
    """Stand in for `skarabina-analyze --json-stdout`: a realistic multi-line
    transcript, where the JSON carries both wrangled keys and extra ones that
    the cab does not expose."""
    declared = set(_value_outputs(schemas.cabs["skarabina-analyze"]))
    payload = {
        "max_baseline_m": 7697.0,
        "max_frequency_hz": 1800000000.0,
        "max_frequency_mhz": 1800.0,
        "resolution_arcsec": 4.4689,
        "field_of_view": "2.5 deg",
        "oversampling_factor": 5.0,
        "recommended_image_size_pixels": 10066,
        "ms": "observation.ms",
    }
    assert declared <= set(payload), f"sample output lacks declared keys: {declared - set(payload)}"
    return "\n".join(
        [
            "Measurement set:  observation.ms",
            "  Max baseline:   7697 m",
            "Recommended image size: 10066 x 10066 pixels",
            "SKARABINA_ANALYZE_JSON " + json.dumps(payload),
        ]
    )


def test_wrangler_extracts_analyze_outputs(schemas):
    """The wrangler must pull every declared scalar output out of a realistic
    console transcript, and tolerate the extra JSON keys."""
    cab = schemas.cabs["skarabina-analyze"]
    parsed = _parse_json_output_dict(_wrangler_pattern(schemas), _sample_analyze_stdout(schemas))
    for name in _value_outputs(cab):
        assert name in parsed, f"declared output '{name}' is not produced by the wrangler"
    assert parsed["recommended_image_size_pixels"] == 10066
    assert parsed["resolution_arcsec"] == pytest.approx(4.4689)


def test_wrangler_ignores_unrelated_lines(schemas):
    """Ordinary console output must not be mistaken for the JSON payload."""
    pattern = _wrangler_pattern(schemas)
    for line in (
        "Measurement set:  observation.ms",
        "  Max baseline:   7697 m",
        "Recommended image size: 10066 x 10066 pixels",
    ):
        assert re.search(pattern, line) is None, f"wrangler matched unrelated line {line!r}"


def test_demo_pipeline_wires_analyze_outputs(schemas):
    """The shipped demo must show the analyze cab feeding a downstream step."""
    assert EXAMPLE_RECIPE.exists(), f"demo pipeline example missing at {EXAMPLE_RECIPE}"
    demo = OmegaConf.load(EXAMPLE_RECIPE)
    recipe = demo.get("demo-imaging-pipeline")
    assert recipe, "demo pipeline should define a 'demo-imaging-pipeline' recipe"
    steps = recipe.steps

    analyze = steps.get("analyze")
    assert analyze is not None, "demo must contain an 'analyze' step"
    assert analyze.get("cab") == "skarabina-analyze"
    assert analyze["params"].get("json-stdout") is True, (
        "the analyze step must set json-stdout so the scalar outputs are produced"
    )

    # A downstream step must bind one of the scalars the wrangler produces.
    flat = json.dumps(OmegaConf.to_container(steps, resolve=False))
    assert "recommended_image_size_pixels" in flat, (
        "demo must consume recommended_image_size_pixels downstream"
    )
    assert "resolution_arcsec" in flat, "demo must consume resolution_arcsec downstream"


def test_demo_recipe_aliases_are_not_also_declared_as_outputs(schemas):
    """Stimela rejects an alias whose name also appears under inputs/outputs
    ('alias also appears under inputs or outputs').  The `aliases:` section is
    itself the declaration, so there must be no separate `outputs:` entry."""
    recipe = OmegaConf.load(EXAMPLE_RECIPE)["demo-imaging-pipeline"]
    declared = set(recipe.get("inputs") or {}) | set(recipe.get("outputs") or {})
    for name in recipe.get("aliases") or {}:
        assert name not in declared, (
            f"alias '{name}' must not also be declared under inputs/outputs"
        )


def test_demo_uses_no_undefined_cabs(schemas):
    """`echo` is not a stimela built-in (checked against 2.1.4), so the demo
    must define any such cab inline rather than referencing it by bare name."""
    recipe = OmegaConf.load(EXAMPLE_RECIPE)["demo-imaging-pipeline"]
    known = set(schemas.cabs) | {"skarabina", "skarabina-analyze"}
    for step_name, step in recipe.steps.items():
        cab = step.get("cab")
        assert cab is not None, f"step '{step_name}' has no cab"
        if isinstance(cab, str):
            assert cab in known, (
                f"step '{step_name}' references cab '{cab}', which is not defined "
                "by this cargo package and is not a stimela built-in"
            )


def test_demo_steps_are_container_portable(schemas):
    """A cab with no `image` is rejected by container backends ("container
    image not specified by cab"), so every demo step must either use a packaged
    cab (which defines an image) or be a python-flavour cab, which picks up
    stimela's default image."""
    recipe = OmegaConf.load(EXAMPLE_RECIPE)["demo-imaging-pipeline"]
    for step_name, step in recipe.steps.items():
        cab = step.get("cab")
        if not isinstance(cab, dict):
            continue  # packaged cab: the cargo schema supplies the image
        flavour = (cab.get("flavour") or {})
        kind = flavour.get("kind") if isinstance(flavour, dict) else flavour
        assert kind in ("python", "python-code"), (
            f"step '{step_name}' defines an inline cab of flavour {kind!r} with no "
            "image; container backends reject that. Use the python flavour or set image:"
        )


def test_release_versions_are_consistent():
    """AGENTS.md requires the three version files to agree: the root package,
    the cargo package, and the container image tag.  The image tag drops the
    'v' prefix (CI's docker/metadata-action uses type=semver)."""
    root = Path(__file__).resolve().parent.parent.parent
    cargo_dir = root / "cargo"

    def project_version(toml_path):
        m = re.search(r'^version\s*=\s*"([^"]+)"', toml_path.read_text(), re.M)
        assert m, f"no project version found in {toml_path}"
        return m.group(1)

    pyproject_version = project_version(root / "pyproject.toml")
    cargo_version = project_version(cargo_dir / "pyproject.toml")
    base = (cargo_dir / "skarabina_cargo" / "genesis" / "skarabina-cargo-base.yml").read_text()
    image_version = re.search(r"^\s+version:\s*(\S+)", base, re.M).group(1)

    assert pyproject_version == cargo_version == image_version, (
        "version mismatch: pyproject.toml="
        f"{pyproject_version}, cargo/pyproject.toml={cargo_version}, "
        f"image={image_version}"
    )
    assert not image_version.startswith("v"), "image tag must not carry a 'v' prefix"
