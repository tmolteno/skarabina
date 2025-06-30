MS=~/astro/1519747221.subset.ms
test:
	rm -rf foo.ms
	skarabina --ms ${MS} --debug --clobber --msout "foo.ms"

summary:
	skarabina --ms ${MS} --summary
uvw:
	skarabina --ms foo.ms --flag-nan --flag-clip [0,10] --apply --clobber --debug --flag-uv-above 250

barber:
	skarabina --ms ${MS} --barber

opt:
	skarabina --ms foo.ms --optimize --msout "bar.ms" --clobber
install:
	poetry install

stimela:
	stimela run --native skarabina-stimela-recipe.yml ms=${MS}
