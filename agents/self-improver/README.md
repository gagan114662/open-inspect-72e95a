# self-improver

The repository's self-improvement loop, laid out the way [eve](https://github.com/vercel/eve) lays
out an agent: **an agent is a directory of files.**

```
agents/self-improver/
  agent.json         runtime config: roles, thresholds, paths, schedule
  instructions.md    what this agent is for, what it may never do
  tools/             the typed functions it can call (registry -> scripts/)
  skills/            playbooks distilled from its own archive, loaded when relevant
  channels/          where it talks: pull requests, issues, Codex review comments
  schedules/         when it runs on its own
```

Nothing here is a second implementation. The files point at the scripts and workflows that already
run; `tools/manifest.json` is checked by a test so the map cannot drift from the territory.
`skills/` is generated from `docs/self-improvement-archive.jsonl` by `scripts/distill-skills.py`,
the step this layout adds: resolved review rounds become playbooks the next fix can load, so the
loop improves the doer as well as the judge.

Run everything from the repository root:

```bash
python3 scripts/distill-skills.py            # rebuild skills/ from the archive
python3 -m pytest scripts/ -q               # includes the agent-directory checks
```
