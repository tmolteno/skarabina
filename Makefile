# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
MS=~/astro/1519747221.subset.ms
test:
	rm -rf foo.ms
	skarabina --ms ${MS} --debug --clobber --msout "foo.ms"

summary:
	skarabina --ms ${MS} --summary
uvw:
	uv run skarabina --ms ~/astro/cyg2052.ms --flag "nan, clip 0 10, uv-above 250" --apply --clobber --debug

barber:
	skarabina --ms ${MS} --barber

opt:
	skarabina --ms foo.ms --optimize --msout "bar.ms" --clobber
install:
	uv sync

lint:
	uv run flake8 skarabina/

stimela:
	stimela run --native skarabina-stimela-recipe.yml ms=${MS}
