# simguard

Helps AI agents that run long scientific simulations avoid the costliest kind of mistake: **the model runs, the numbers look plausible, and the model is still wrong.**

simguard has two parts:

- **`simq`**: a single-machine solver queue for Windows. Several conversations and subagents share one workstation. Jobs are started according to cores, memory and solver licenses, and a job keeps running even if the agent that submitted it goes away.
- **`sim-guard` skill**: a workflow plus check scripts for Claude Code. It scopes what an agent may touch, reads back the saved model before solving, freezes pass/fail criteria before results exist, requires validation that is independent of the model, and makes the report say what was not checked.

The detailed docs are in Chinese; this page is a summary.

## Failure classes, not one-off fixes

Each check targets a general class of silent error, not a single incident. The full catalogue is in [failure-modes.md](skills/sim-guard/references/failure-modes.md).

| Class | General form | Examples | Defense |
|---|---|---|---|
| A. Setup | The saved model is not the model you meant | Only one component of a tensor set, the rest left at default 1; a sweep parameter never reached the model; an "off" switch that does not switch anything off | Read back the saved file (`.mph` parsed directly, other solvers via exported JSON) and check declared expectations. A built-in rule flags any tensor whose diagonal mixes the default `1` with other values, without knowing physics names. Response tests judge from numbers whether an input really matters. |
| B. Execution | The result is not from this run | Exit code 0 on failure; NaN in results; drivers edited after the check | Log error markers, output freshness and finite-value checks. The preflight receipt hashes the script, drivers, criteria and read-back models, and the queue re-checks them at submit time. |
| C. Validation | The check cannot fail | Benchmark shares the model's bug; identical results on every mesh; conservation not closed | Validation must not share code with the model; `converged` and `sum_approx` result expectations |
| D. Process | The workflow was bypassed | Threshold loosened after seeing results; code copied from a known-bad run; queue bypassed | Criteria frozen and hash-checked at submit, report and collect time; `DO_NOT_REUSE` markers (parents included); `sg.py taint` lists every downstream run; scope rules and queue receipts |

When a new error shows up: write its general form, find its class, add the cheapest check that catches it automatically, put it in `simguard.project.json`, and **confirm the check fails on a deliberately broken version**.

## Requirements

- Windows (`simq` uses Windows process and memory APIs; `sg.py` is portable but only tested on Windows)
- Python 3.9+, standard library only
- [Claude Code](https://claude.com/claude-code) to load the skill
- Your own solvers and licenses; none are included

## Install

```powershell
Copy-Item simq\config.example.json simq\config.json        # then set cores, memory, licenses
Copy-Item -Recurse skills\sim-guard $HOME\.claude\skills\sim-guard
$env:SIMQ_PY = "C:\path\to\simq\simq.py"                   # or set simq_py in simguard.project.json
```

## Quick tour

```powershell
python sg.py init runs\case1                  # creates gate.json, manifest.json, REPORT.md
python sg.py freeze runs\case1                # freeze criteria before any result exists
python sg.py readback model.mph --list        # every parameter, physics and material setting in a saved model
python sg.py submit runs\case1 --script run.ps1 --sw comsol --est 3
python sg.py report runs\case1                # refuses "Overall: PASS" when a criterion or required check failed
python sg.py taint runs\old --reason "..."    # mark a bad run and list everything derived from it
python sweep.py plan|advance|collect sweep.json
```

A readback block in `manifest.json`:

```json
"readback": {
  "sources": {"model": "prepared.mph", "rb": "readback.json"},
  "expect": [
    {"select": "model:param/lambda0", "op": "approx", "value": 1550, "rel": 1e-9},
    {"select": "model:physics/*/*/c", "op": "isotropic", "physics_op": ["CoefficientFormPDE"]},
    {"select": "rb:source.wavelength_m", "op": "range", "min": 1.5e-6, "max": 1.6e-6}
  ]
}
```

## Limits

These checks reduce mistakes. They do not prove a model correct.

- Agents fill in the manifest, criteria and report. A script only sees records, so a human or another agent should review claims such as "validation shares no code with the model".
- `no_partial_default` is a heuristic. A genuinely anisotropic tensor with one component equal to 1 is flagged and must be allowed with a reason.
- Receipts are plain files. They catch carelessness, not deliberate forgery.
- Without a hook, an agent can still launch a solver directly.

## Tests

```powershell
python -m unittest discover -s tests
```

The tests start a private dispatcher in a temporary directory, never touch an existing queue, and never call a solver.

## License

[MIT](LICENSE)
