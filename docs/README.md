# ReplyAI documentation

Start with the project [README](../README.md) for an overview, then use this index.

## Get it running

| Document | Read it when you want to… |
| --- | --- |
| [Getting started](getting-started.md) | Go from clone to a running app in a few commands |
| [Installation](installation.md) | Install step by step: prerequisites, environment files, dependencies, migrations, startup, health checks |
| [Simulation guide](simulation-guide.md) | Explore every workflow with a fake channel and scripted models, no credentials needed |
| [Real-mode setup](real-mode-setup.md) | Connect your YouTube channel and real model providers safely |
| [Configuration](configuration.md) | Look up an environment variable or per-channel setting |
| [Troubleshooting](troubleshooting.md) | Fix Docker, database, Redis, OAuth, provider, port, worker or quota problems |

## Understand it

| Document | Read it when you want to… |
| --- | --- |
| [Features](features.md) | See every feature, where it is in the UI, how it works and its limits |
| [Architecture](architecture.md) | Understand the processes, model pipeline, LangGraph engine, job queue, decision policy and publication safety |
| [AI safety](ai-safety.md) | See the 24 failure modes (P01–P20, M1–M4), the defenses and the evidence for each |
| [Limitations](limitations.md) | Know what is and isn't validated |
| [Data retention](data-retention.md) | Know what is stored, for how long, and how deletion works |

## Change it

| Document | Read it when you want to… |
| --- | --- |
| [Development](development.md) | Learn the project structure, conventions, commands, migrations and worker debugging |
| [API](api.md) | Find an endpoint group; the running API also serves OpenAPI docs at `/docs` |

## Screenshots

Screenshots in [`assets/screenshots/`](assets/screenshots/) are captured from the running
application. `videos.png`, `inbox.png`, `settings.png` and `posted.png` show **real mode**
on the author's YouTube test channel, with commenter names and avatars blurred.
`review-queue.png`, `automation.png` and `jobs.png` show **simulation mode** (fake
channel, scripted models) and keep the *Simulation mode* banner.
