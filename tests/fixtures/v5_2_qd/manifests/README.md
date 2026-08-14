# v5.2-QD manifest fixtures

`tests.test_preflight.make_complete_inventory` creates the tiny sealed raw,
sidecar, and split-manifest trees used by the preflight tests. Keeping the
fixture generated makes every hash mutation explicit and avoids copying any
production raw data into the test suite.
