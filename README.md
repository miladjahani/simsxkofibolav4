# SX-EW Digital Process Simulator V5

V5 upgrades the V4 native flowsheet engine into a stage-by-stage empirical SX-EW simulator.

## Core model
- Cu / Fe / H2SO4 component mass balances
- Aqueous / organic / solid phases
- Extraction equilibrium using distribution coefficient D(Cu) and O/A
- Optional acid dependence of D
- Finite organic loading (`Qmax_Cu`) and empirical isotherm controls (`isotherm_K`, `isotherm_n`)
- Stripping equilibrium using D(Cu) and A/O
- Organic inventory represented through the actual organic stream flow and loading
- Phase disengagement / phase recovery factor
- Independent parameters on every Extraction and Stripper stage
- Parallel branches and series stages
- Explicit recycle edges and damped fixed-point convergence
- Live PFD stream data and Sankey payload
- Workbook-native Excel engine remains available as the reference model

## Important engineering note
This is an empirical process simulator, not a thermodynamic property package. D, isotherm, acid dependence and disengagement correlations are intentionally parameterized so plant/lab data can be fitted without hard-coding a proprietary chemistry model. Before plant design use, calibrate D/isotherm and hydraulic correlations against validated SX testwork and site operating data.

## API
- `POST /api/process/simulate` — full flowsheet solution
- `POST /api/process/solve` — solver/KPI/Sankey-focused result
- `GET /api/process/schema` — block/parameter schema
- `GET /api/process/presets` — preset list
- `POST /api/native-calculate` — workbook-native Python calculation

## Running
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## V6 Industrial Flowsheet Editor
- Drag/drop block palette on the PFD canvas.
- Interactive input/output ports for graph connections.
- Explicit stage trains for Extraction and Stripper; each stage has independent D(Cu), O/A or A/O, Qmax and isotherm parameters.
- Counter-current flowsheet presets use explicit reverse/recycle edges and the iterative solver.
- Stage profiles and empirical isotherm curves are returned by `/api/process/analysis`.
- Scenario comparison is available at `/api/process/compare`.
- Live stream network/Sankey reports Flow, Cu, Fe and acid mass on every solved stream.
- The Excel/native workbook engine remains available as the reference calculation path.

### Important engineering boundary
The SX model is an empirical/thermodynamic-parameter framework, not a proprietary reagent/property package. D values, isotherm constants, capacity and disengagement parameters must be calibrated against laboratory or plant data before engineering design decisions are based on the simulator.

## V6 Optimization Studio

V6 includes a native Python optimization layer at `app/optimizer.py` and the authenticated endpoint `POST /api/process/optimize`. Each candidate circuit is re-solved by the SX-EW process engine, so Recycle convergence, stage calculations and mass balance are part of the optimization loop.

Supported objectives include maximizing Cu recovery/product and minimizing O/A, stage count, acid output and mass-balance error. Variables use paths such as `nodes.E1.params.O_A`, `nodes.E1.params.D_Cu`, `nodes.E1.params.Qmax_Cu` or `feed.flow`. Bounds and step sizes are enforced. The UI provides Optimization Studio, ranked candidates, score history and the best circuit/parameter set.

The optimizer is intentionally deterministic and bounded (coordinate-search) so it can run without a scientific-computing dependency. For plant deployment, measured calibration data and a domain-specific objective/cost function should be supplied before using results for engineering decisions.
