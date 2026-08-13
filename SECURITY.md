# Security

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/akin-oz/harrier/security/advisories/new)
on this repository. Do not open a public issue.

Expect an acknowledgement within a week. This is a personal project maintained
by one person, so that is a realistic figure rather than an SLA.

## What this project's threat model actually is

Harrier is local-first and single-user. It binds to localhost, it holds one
person's data on one machine, and it is not deployed anywhere. That makes some
otherwise-serious classes of issue out of scope, and it makes a few narrow
ones genuinely interesting.

**In scope, and worth reporting:**

- Anything that gets personal data into git. Every path has exactly one class
  in `config/data-classification.json`, and a gap there is a real finding.
- A way for a web page in another browser tab to reach the local API. It holds
  a token and a trusted-host check for exactly this (spec 035); a bypass of
  either, including DNS rebinding, is in scope.
- A credential reaching a log, a run stream, a rendered artifact, or an error
  message. Credentials are scrubbed at the boundary and identity values are
  redacted from logs (`harrier.logredact`); a sink that misses is in scope.
- Path traversal out of the fixture directory, the artifact directory, or a
  backup archive during restore.
- A prompt injection through job or email content that causes the tool to
  take an action rather than draft one. Nothing here auto-sends; a path that
  makes it send is a real finding.

**Out of scope, by design rather than by oversight:**

- No authentication or authorisation model. There is one user, one machine,
  and no accounts. "There is no login" is a documented limitation, not a
  vulnerability.
- Denial of service against a localhost service you already control.
- Anything requiring an attacker to already have local shell access, since at
  that point the database is readable directly.
- Dependency advisories with no reachable path from this code. Report them if
  you can show the path.

## What the design does not protect

Stated plainly because the README's limitations section says the same thing:
personal data lives unencrypted in a local SQLite database. Disk encryption
and backup custody are the operator's responsibility. Never-in-git protects
the repository, not the machine.
