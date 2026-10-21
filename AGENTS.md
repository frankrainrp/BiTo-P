# FamilyOS API development

This repository contains the Python/SQLite API edition and synthetic-data demo. `README.md` describes the delivered components; `docs/ENGINEERING.md` explains domain and execution boundaries.

- Preserve `https://github.com/frankrainrp/FamilyOS-Showcase`, which is referenced in the resume.
- Resolve identity from server-side sessions. Enforce ownership, visibility and action confirmation in domain services.
- Keep the default listener on loopback and document the trusted-local username identity model.
- Use synthetic data. Keep production household records, credentials, deployment addresses and private UI packages outside this repository.
- Preserve migrations, tests, dependency files and distribution notices.
- Run the Python suite and `python examples/demo.py` after behavior changes. Public CI verifies code and containers independently of private deployments.
- Write documentation in English, describing concrete APIs, storage, model adapters and data flows.
