# CLAUDE.md

Rules for every session on this project:

- Read SpeedMart_BUILD_SPEC.md before doing anything.
- Implement only the step you are asked to implement. Never jump ahead.
- Section 8 of the spec is the contract between modules. Never change it without asking.
- Respect the feature flags in config.json. Disabled features must never break the core loop.
- Do not add features, pages, or dependencies that are not in the spec.
- After finishing a step, run every acceptance check, report pass or fail for each, and list anything a human must verify physically.
- The dev machine is Windows (cmd and PowerShell). Every script must work on Windows: provide scripts/run_all.ps1 alongside run_all.sh, use COM ports for serial, and give Windows commands in all instructions and checklists.