MS=~/astro/1519747221.subset.ms
test:
	rm -rf foo.ms
	skarabina --ms ${MS} --debug --clobber --msout "foo.ms"

summary:
	skarabina --ms ${MS} --summary
uvw:
	skarabina --ms foo.ms --apply --clobber --debug --uv-above 300

barber:
	skarabina --ms ${MS} --barber

install:
	poetry install
