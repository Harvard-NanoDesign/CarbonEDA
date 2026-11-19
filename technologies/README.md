# Technologies

This folder contains the process flows, grouped in category folders (such as `examples/`). Each technology is a folder with:

- a process flow CSV
- a layout `.gds`
- a `tech.yaml`, which names the CSV and layout and sets the clip window, carrier wafers, and render views

`TECH=<name>` finds a technology in any category; `TECH=<category>/<name>` also works.

## Examples

| Technology | Contents |
| --- | --- |
| `examples/bond_flip/` | Wafer bonding and flipping: a layer transfer from a donor wafer, a carrier wafer bond, and wafer flips (25 process steps). |
| `examples/nsh_example/` | A 2 nm-class nanosheet (GAA) logic flow from the wafer start to M0 (246 process steps). |

## Process flow CSV

One row per spx module and process step, read in `seq` order. Columns can be in any order, and `make fmt TECH=<tech>` rewrites the CSV in the standard layout.

| Column | Contents |
| --- | --- |
| `seq` | Row order (number in 10s, so a step can be inserted as `125`) |
| `row_type` | `module` or `step` |
| `route` | Optional: the wafer the row builds, for a CSV with several wafers; blank is the main wafer |
| `flow`, `module`, `step` | spx flow, module, and step names |
| `step_type` | The spx function (`depo_isotropic`, `etch_cmp`, `litho_patterning`, `bond_structure`, `flip_structure`, ...), or a carbon-only type such as `clean` |
| `carboneda_process_area` | The step's process area: a key of `energy_per_step_kwh` in `config/carbon_config.yaml` (a litho step names its tool, e.g. `EUV`), or `Wafer` for substrate, bond, and flip steps |
| `carboneda_energy_kwh` | Optional: overrides the step's tool energy |
| `Comment` | Optional: shown on the step's report page |
| Any other column | An spx parameter, named as in spx; a blank cell keeps the spx default |

Cell syntax:

| Kind | Example |
| --- | --- |
| Quantity | `0.04 min`, `1.0 um min^-1`, `90.0 deg` |
| Material list | `SiO2\|Oxide`, or `*` for every material |
| Rate per material (`MaterialDependent`) | `SiO2 => 1.0 um min^-1; Silicon => 0.1 um min^-1` |
| Mask layers (`LayerNames`) | `1:0\|2:0` |
| Boolean | `True`, `False` |
| Python literal | `py:[('a', 1)]` |

A step type that is not an spx function, with no spx parameters (e.g. `clean`), is a carbon-only step: spx leaves the structure unchanged, and the step keeps its carbon. A `bond_structure` step bonds the wafer named in `BondTopStep` (a route in the CSV, or a carrier wafer under `routes:` in `tech.yaml`) on top, face down with `Flip` = `True`; `flip_structure` turns the whole stack over. `examples/bond_flip` shows both.
