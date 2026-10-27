# Windows companion handoff

Download or copy `carter-companion-1.0.0.zip` and its `.zip.sha256` sidecar
from this directory. The ZIP contains the canonical boss profiles, JSON schemas,
protocol, installation notes and dependency-free reference parser/matcher.
It contains no tokens, Discord credentials, player logs or generated analyses.

From a Windows checkout, the ZIP is at
`dist\companion\carter-companion-1.0.0.zip`.

Rebuild from the repository root with:

```text
python -m log_backend.build_companion_bundle --output dist/companion
```

Verify SHA-256 before extracting. After extraction, read `INSTALL.md` and
`PROTOCOL.md`, and validate the manifest file hashes.

The private 335 MB combat log is deliberately not distributed in Git. For replay
testing, use a local Windows WoW log or transfer the private Linux log separately.
Do not invent a Windows path for a file that has not been transferred.
