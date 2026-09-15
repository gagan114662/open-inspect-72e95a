# tools

`manifest.json` is the registry of functions this agent can call. Every entry names a script under
`scripts/`; a test asserts each script exists and that every loop script under `scripts/` is
registered, so this list is the truth.
