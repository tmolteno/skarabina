# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
from importlib import resources

from omegaconf import OmegaConf


def test_schema_loads():
    """The stimela cab schema should load and define the skarabina cab."""
    recipe = resources.files("skarabina_cargo").joinpath("skarabina.yml")
    schemas = OmegaConf.load(recipe)
    cab = schemas.cabs.get("skarabina")
    assert cab is not None
    inputs = cab.inputs
    for key in ("ms", "summary", "barber", "apply", "clobber"):
        assert key in inputs, f"missing input '{key}' in schema"
