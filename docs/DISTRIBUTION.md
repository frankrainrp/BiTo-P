# Installation and components

| Component | Start | Interface |
|---|---|---|
| Windows API package | Extract `FamilyOS-API-Windows-x64.zip`; run `Start-Server.cmd` | HTTP API at `127.0.0.1:8080` |
| Synthetic demo | Run `Run-Demo.cmd` | Temporary HTTP service and console checks |
| Source | Python 3.12+ or Docker Compose | Python services, tests, migrations and HTTP demo |

[Release downloads](https://github.com/frankrainrp/FamilyOS-Showcase/releases/tag/v0.1.0)

The Windows package includes Python 3.12.10 and pinned dependencies. Extract into a writable directory. `Start-Server.cmd` creates `data/familyos.db`; stop with Ctrl+C. `Run-Demo.cmd` uses fictional records in temporary storage.

The HTTP demo is the included client. This distribution is the API edition; the Bito browser application is maintained separately. Optional activity-processing source is included under `agents/windows_activity/`.

Source archives include agent instructions, complete maintained source, tests, migrations, CI and Docker configuration. Runtime packages additionally contain Python and dependencies with their notices. Model weights and speech services are installed separately. Keep the username-based API on a trusted local network with the default loopback binding.
