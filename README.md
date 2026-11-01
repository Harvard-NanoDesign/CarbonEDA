# CarbonEDA

CarbonEDA quantifies the *embodied carbon* (C<sub>embodied</sub>) of Integrated Circuit (IC) fabrication, accounting for the energy, material, and gas use of every process step in a full fabrication process. The process flow is defined in a CSV file. CarbonEDA drives the 3D process simulation in Synopsys Sentaurus Process Explorer (SPX) and quantifies emboeded carbon footprint in terms of energy per wafer (EPW), materials per wafer (MPW), and gases per wafer (GPW), in kilograms of CO<sub>2</sub> equivalent (kgCO<sub>2</sub>e). The outputs include:

- carbon footprint data: EPW, MPW, and GPW per process step, per gas, per module, and per process area, and the totals per wafer (CSV)
- the generated SPX files, the 3D structure after each process step (`.tdr`), and a checkpoint after each module (`.tbson`)
- images of each step's 3D structure and its x and y cross-sections
- a design-review report (PDF) summarizing the process and its carbon footprint step by step: one page per process step with its 3D structure, cross-sections, mask, materials, and EPW, MPW, and GPW, plus summaries by module and process area

## Repository layout

| Path | Contents |
| --- | --- |
| `technologies/` | Process flows. Each technology is a folder with a process flow CSV, a layout `.gds`, and a `tech.yaml`. [`technologies/README.md`](technologies/README.md) describes the CSV format and the examples. |
| `config/` | `carbon_config.yaml` (energy, gas use, and gas emissions per process area), `materials.yaml` (density and GWP of each material, wafer size, yield), `spx_steps.yaml` (SPX step functions), and `tools.yaml` (how this machine runs SPX and svisual). |
| `carbon_eda/` | The pipeline: CSV reader, SPX file generation, svisual rendering (`svisual_render.tcl`), carbon model (`carbon/`), and report. |
| `outputs/examples/` | Carbon CSVs and design-review PDFs of the examples. |
| `tests/` | Checks of the flow CSVs and `config/`. |

## Running

Requires Python 3.9 or later; `make venv` installs the dependencies into `.venv`. The carbon CSVs need only Python. The SPX simulation and step images need SPX and Sentaurus Visual (svisual), set up in `config/tools.yaml` (or `config/tools.local.yaml` for one machine).

```bash
make venv
make test                       # check every flow CSV and config/
make carbon TECH=nsh_example    # carbon CSVs (no SPX needed)
make spx TECH=nsh_example       # SPX simulation and step images
make report TECH=nsh_example    # carbon CSVs and the design-review PDF
make all TECH=nsh_example       # test, SPX, and report
```

Options: `LIMIT=N` (first N steps only), `STEPS=1-20` (report pages), `JOBS=N` (svisual processes). Outputs go to `outputs/<category>/<tech>/`:

| Path | Contents |
| --- | --- |
| `carbon/` | `steps.csv`, `gases.csv`, `by_module.csv`, `by_area.csv`, `totals.csv` |
| `report/` | `<tech>_design_review.pdf` |
| `spx/`, `tdr/`, `tbson/`, `img/` | Generated SPX files, the structure after each step (`.tdr.zst`), a checkpoint after each module, and the step images |

## Carbon model

Per step, in kgCO<sub>2</sub>e per wafer, from the step's process area (`carboneda_process_area` in the CSV) and the values in `config/`:

- **EPW**: tool energy × (1 + facility overhead) × fab carbon intensity.
- **MPW**: deposited film mass × material GWP, plus each starting wafer, plus upstream production of the input gases.
- **GPW**: unused input gas and the byproducts it forms, after abatement, × gas GWP.

Steps the config does not cover (an unknown process area, a material with no data) are listed in `carbon/steps.csv`, on the report's "Config gaps" page, and as `make test` warnings.

## Citation

If you use CarbonEDA, please cite:

D. Kong, D. Grey-Stewart, M. Elgamal, Z. Chen, Z. Piao, J. Morris, Y. Yao, and G. Hills, “Quantifying Trade-Offs in Power, Performance, Area, and Total Carbon Footprint (PPAtC) of Next Directions in FET Technologies for VLSI Digital Logic Circuits,” *2026 IEEE International Electron Devices Meeting (IEDM)*, San Francisco, CA, USA, 2026, pp. 1-4.

## License

CarbonEDA is released under the MIT License, so you are free to use, modify, and distribute it, provided the copyright and license notice are kept in all copies of the software. See [LICENSE](LICENSE) for the full text.
