# B.5 Spike — Test Corpus

Two corpora drive the listening pass:

- **`corpus_handwritten.txt`** — committed. 25 prompts in 4 categories (short / medium / long / special), authored to give matrix coverage of typical assistant-style German replies.
- **`corpus_production.txt`** — **gitignored**. 10 anonymised real prompts pulled from the production database. Privacy guarantee: never committed; report references prompts as `prod-01..prod-10`, raw text never appears in the report or repo.

## Producing `corpus_production.txt`

The backend's `piper_service.py` does not log synth text (status-only logging). The authoritative source is the `messages` table in the backend's Postgres, where each assistant reply that drove a TTS call is persisted as `role='assistant'`.

### One-shot extraction (operator action, before the maintenance window)

```bash
# Connect to the backend Postgres
kubectl --context renfield-private exec -n renfield -it postgres-0 -- \
    psql -U renfield -d renfield -c "
    SELECT content
    FROM messages
    WHERE role = 'assistant'
      AND created_at > NOW() - INTERVAL '7 days'
      AND length(content) BETWEEN 20 AND 600
    ORDER BY random()
    LIMIT 30;"
```

Returns 30 candidate assistant messages. Pick **10 representative** prompts manually with the following balance in mind:

- 3-4 short (one sentence)
- 4-5 medium (2-3 sentences)
- 1-2 long (paragraph)
- Cover at least one prompt with numbers/dates and one with names

### Anonymisation rules

Replace each token with the listed marker, in this order:

| Pattern | Replacement |
|---|---|
| Family names (anyone in the household) | `[NAME]` |
| Address fragments (street, city, postal code) | `[ORT]` |
| Phone numbers | `[TEL]` |
| Email addresses | `[EMAIL]` |
| Specific calendar dates referencing the household | `[DATUM]` |
| Other PII as judged | `[PII]` |

When in doubt, anonymise. A prompt that loses meaning under anonymisation should be replaced rather than kept partially identifiable.

### File format

Same shape as `corpus_handwritten.txt`:

```
prod-01: <anonymised text>
prod-02: <anonymised text>
…
prod-10: <anonymised text>
```

Save as `voice-server/tests/b5/corpus_production.txt`. The `.gitignore` entry at the repo root prevents accidental commit.

### After the listening pass

`corpus_production.txt` stays on the operator's local machine. The B.5 report (`docs/B5_XTTS_EVAL.md`) refers to these prompts only by `prod-01..prod-10` IDs — never quotes the text. Once the report is finalised, the operator decides whether to delete the file or archive it locally outside the repo.
