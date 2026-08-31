# Security Policy

Kaleo is the desktop client for the silkscreen PCB engine. It is a fork of
[Pluely](https://github.com/iamsrikanthnani/pluely) (GPL-3.0), so a report may
land in code this project wrote or in code it inherited. Send it here either
way — we will forward anything that turns out to be upstream's.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the
[Security tab](https://github.com/machmoon/Kaleo/security) of this repository
and click **"Report a vulnerability"**. That keeps the report private until a
fix ships.

Do not open a public issue for a suspected vulnerability.

Useful things to include: the version or commit, your OS, what an attacker
gains, and the smallest set of steps that reproduces it.

## Scope

Kaleo talks to a silkscreen engine over loopback HTTP and nothing else. Reports
that the app reaches a host outside `127.0.0.1`, `localhost`, or `[::1]`, that
it escapes the capability allowlist in `src-tauri/capabilities`, or that it
sends anything anywhere without the user asking, are in scope and taken
seriously.

Vulnerabilities in the silkscreen engine itself belong in the engine's own
repository, not here.
