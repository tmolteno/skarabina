import click
from scabha.schema_utils import clickify_parameters
from omegaconf import OmegaConf
from importlib import resources

from . import recipes

recipe = resources.files(recipes) / "hello.yml"
schemas = OmegaConf.load(recipe)


def hello(name, count):
    for x in range(count):
        click.echo(f"Hello {name}!")


@click.command("hello")
@clickify_parameters(schemas.cabs.get('hello'))
def main(**kw):
    """Simple program that greets NAME for a total of COUNT times."""
    opts = OmegaConf.create(kw)

    hello(opts.name, opts.count)
