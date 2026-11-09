# Author: David Kong
# License: MIT
#
# CarbonEDA: flow CSV + layout -> spx route -> tdr per step -> png per step -> design-review PDF
#   make test                  check every flow CSV and config/ for user errors (seconds)
#   make spx TECH=bond_flip    spx route + sim (tdr per step) + svisual images (slow)
#   make carbon TECH=bond_flip carbon CSVs only, from the current CSV and config/ (seconds)
#   make report TECH=bond_flip carbon from the current CSV and config/, then the PDF (minutes);
#                              carbon CSVs only if the technology has no spx run yet
#   make all TECH=bond_flip    test (this TECH) + spx + report
#   make fmt TECH=bond_flip    rewrite the flow CSV in the standard column layout (the flow is unchanged)
#   make clean TECH=bond_flip  delete the spx run outputs; carbon/ and report/ are kept
#   TECH=examples/bond_flip works too; a bare name is found in any category subfolder.
#   LIMIT=8      spx/carbon/report/all: only the first 8 steps (quick check)
#   STEPS=1-20   report: only these step pages
#   JOBS=4       spx/all: render images with 4 svisual processes at once
#   ZERO_FILL=1  spx/all: run steps with no spx parameters with 0 values without asking
#                (config/spx_steps.yaml; needed when spx runs with no terminal, e.g. nohup)
TECH  ?= examples/bond_flip
PY    ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
LIMIT ?=
STEPS ?=
JOBS  ?=
ZERO_FILL ?=

.PHONY: all spx carbon report test fmt venv clean

all:
	CARBON_EDA_TECH=$(TECH) $(PY) -m unittest discover tests
	$(PY) -m carbon_eda all $(TECH) $(if $(LIMIT),--limit $(LIMIT)) $(if $(JOBS),--jobs $(JOBS)) $(if $(ZERO_FILL),--zero-fill)

# spx files + manifest, spx batch run -> outputs/$(TECH)/tdr/*.tdr.zst (zstd as written;
# tdr_zstd_level in config/tools.yaml), svisual -> outputs/$(TECH)/img/*.png
spx:
	$(PY) -m carbon_eda spx $(TECH) $(if $(LIMIT),--limit $(LIMIT)) $(if $(JOBS),--jobs $(JOBS)) $(if $(ZERO_FILL),--zero-fill)

# outputs/$(TECH)/carbon/*.csv only (no PDF)
carbon:
	$(PY) -m carbon_eda carbon $(TECH) $(if $(LIMIT),--limit $(LIMIT))

# outputs/$(TECH)/carbon/*.csv, then outputs/$(TECH)/report/$(TECH)_design_review.pdf
report:
	$(PY) -m carbon_eda report $(TECH) $(if $(LIMIT),--limit $(LIMIT)) $(if $(STEPS),--steps $(STEPS))

test:
	$(PY) -m unittest discover tests

# standard layout: fixed columns, route, carbon columns, then parameters; empty columns dropped
fmt:
	$(PY) -m carbon_eda fmt $(TECH)


venv:
	python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -e .

# delete the spx run of TECH (spx/, tdr/, tbson/, img/, manifest.json, logs); carbon/ and report/ are kept
clean:
	$(PY) -m carbon_eda clean $(TECH)
