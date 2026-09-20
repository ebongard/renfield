---
name: git-workflow
description: Git workflow rules for Renfield. Commit message format, issue numbering, branch naming, PR creation, documentation updates before push. Triggers on "commit", "push", "PR erstellen", "pull request", "branch", "git", "merge".
---

# Git Workflow

## CRITICAL RULES

1. **NIEMALS ohne Erlaubnis pushen** — `git push` NUR nach expliziter Bestätigung
2. **Issue-Nummer bei jedem Commit** — Vor jedem Commit nach der Issue-Nummer fragen
3. **Dokumentation vor Push** — die Rule des Subsystems (`.claude/rules/`), docs/, README müssen Änderungen widerspiegeln (`CLAUDE.md` nur für Regeln, die JEDE Sitzung braucht — sie bleibt unter 200 Zeilen)
4. **Branch Protection** — Direct push to `main` is blocked. Always use feature branch → PR → merge.
5. **Co-Authored-By** — Jeder Commit muss die Co-Author-Zeile enthalten

## Commit Message Format

```
type(scope): Kurze Beschreibung (#issue)

Längere Beschreibung falls nötig.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
```

**Example:** `feat(satellites): Add monitoring dashboard (#25)`

## Commit Types

| Type | Usage |
|------|-------|
| `feat` | Neues Feature |
| `fix` | Bugfix |
| `docs` | Dokumentation |
| `refactor` | Code-Refactoring |
| `test` | Tests hinzufügen/ändern |
| `chore` | Wartung, Dependencies |

## Workflow

1. Create feature branch: `git checkout -b type/short-description`
2. Make changes + write tests (TDD)
3. Update documentation (`.claude/rules/<subsystem>.md`, docs/ — `CLAUDE.md` only for always-on rules)
4. Ask for issue number
5. Commit with proper format
6. Ask user: "Soll ich pushen?"
7. Only after confirmation: `git push -u origin branch-name`
8. Create PR: `gh pr create --title "type(scope): Description (#issue)" --body "..."`

## PR Format

```bash
gh pr create --title "type(scope): Description (#issue)" --body "$(cat <<'EOF'
## Summary
- Bullet points describing changes

## Test plan
- [ ] Tests added/updated
- [ ] Manual testing done

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
