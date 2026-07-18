# voice-server releases

Appended by `bin/release-voice-server.sh` after a successful push. The digest
column is what consuming manifests pin (`image: <digest>` with
`imagePullPolicy: IfNotPresent`) — tags are treated as immutable, digests make
that enforceable.

| tag | date | git | digest |
|-----|------|-----|--------|
| v0.3.0 | 2026-07-18 | f3aab0dc | `registry.treehouse.x-idra.de/renfield/voice-server@sha256:a252cd9294827705f4ecd419280ac19a962d30d1266aaafda61dc0fe0a3b7396` |
