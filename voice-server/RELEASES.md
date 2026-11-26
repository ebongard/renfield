# voice-server releases

Appended by `bin/release-voice-server.sh` after a successful push. The digest
column is what consuming manifests pin (`image: <digest>` with
`imagePullPolicy: IfNotPresent`) — tags are treated as immutable, digests make
that enforceable.

| tag | date | git | digest |
|-----|------|-----|--------|
| v0.3.0 | 2026-07-18 | f3aab0dc | `your-registry.example/renfield/voice-server@sha256:a252cd9294827705f4ecd419280ac19a962d30d1266aaafda61dc0fe0a3b7396` |
| v0.3.1 | 2026-07-18 | b5d8373a | `your-registry.example/renfield/voice-server@sha256:e93a2b46491054964fe72330884550c502181b681e0249eb3a1f7fcf96982e41` |
| v0.3.2 | 2026-07-19 | b16e04c3 | `your-registry.example/renfield/voice-server@sha256:733e96566b6a599796c4150f329ea6e106c0eeaeb946e52941f46f7f811e3ba4` |
| v0.3.3 | 2026-07-19 | d929420c | `your-registry.example/renfield/voice-server@sha256:2e977a367522561431435c4b412fcaefe9f62463ae52c89ef7c67a2484ff2a30` |
| v0.3.7 | 2026-07-22 | ad403347 | `your-registry.example/renfield/voice-server@sha256:25ecbb757143978bdc03b4bbd781fe87bf795d00107ae36e952e1ed2aa29341c` |
| v0.3.8 (broken, never pin; see CHANGELOG 0.3.9) | 2026-11-01 | d7fdd242 | `your-registry.example/renfield/voice-server@sha256:3211adbfb39af10a7d6f45afd91e9f79bd463836a91d23663099c536e1e5c3eb` |
| v0.3.9 | 2026-11-01 | ea65eecd | `your-registry.example/renfield/voice-server@sha256:dcc54dafffc7bd6ec25c2bdd624b8b1a84bc098c0774702d041ceb701b5b73e0` |
| v0.3.10 | 2026-11-22 | edf11631 | `your-registry.example/renfield/voice-server@sha256:5c7bd11588d0c3b5525fd7cbaecd5c4ff8af21fd7839c66f0e022213d9d32a70` |
| v0.3.11 | 2026-11-22 | 4b016cbc | `your-registry.example/renfield/voice-server@sha256:ed703e39cabfd3545bba1a055c76003a0c6f48f5881567b9d088bf5e509d0f89` |
| v0.3.12 | 2026-11-24 | db392f12 | `your-registry.example/renfield/voice-server@sha256:282b0c12c1e6985f4a21056ae19faeb4dfe656a04abb5d89f910b91b0e9dbd0e` |
| v0.3.13 | 2026-11-26 | 0019053f | `your-registry.example/renfield/voice-server@sha256:eb1a4ae422d2f9450c9b5d0b258b74329e05d02d992efaf327187470f084bdd0` |
