# md2issues

Turn a Markdown "Epics & User Stories" document into GitHub **milestones** and
**issues** — one milestone per epic, one issue per user story, with issue bodies
that are self-contained.

Authentication is delegated to the [`gh`](https://cli.github.com) CLI, so no
tokens are handled by this tool.

## Requirements

- Python 3.7+ (standard library only, no dependencies to install)
- GitHub CLI (`gh`) installed and authenticated (`gh auth login`)

## Quick start

```bash
# preview: parse and print what would be created, write nothing
python3 md2issues.py epics.md --dry-run

# create in the current repository
python3 md2issues.py epics.md

# create in a specific repository
python3 md2issues.py epics.md --repo owner/name --label epic --assignee @me
```

## Expected document format

```markdown
# Recruiter Management CLI - Epics & User Stories

## Architecture & Technology Stack
* **Language:** Kotlin

## Definition of Done (DoD)
1. **CI Pipeline:** GitHub Actions workflow passing cleanly on every push/PR.

## Epic 1: Project Scaffolding, Quality Gates & CLI Skeleton
> **Goal:** Set up the Kotlin Gradle project with strict quality tooling.

### Story 1.1: Initialize Kotlin Gradle Project
* **As a** developer,
* **I want** a baseline Kotlin project,
* **So that** quality gates are enforced from day one.
* **Acceptance Criteria:**
  * `./gradlew check` passes.
  * `./gradlew jacocoTestReport` fails below 80% coverage.
```

## How the document maps to GitHub

| Markdown | GitHub |
| --- | --- |
| `## Epic N: Title` | milestone `Epic N: Title` |
| `> **Goal:** …` | milestone description, repeated in every story issue |
| `### Story N.M: Title` | issue `Story N.M: Title` attached to the epic's milestone |
| `**As a** / **I want** / **So that**` | the user story paragraph of the issue |
| `**Acceptance Criteria:**` | `- [ ]` task list in the issue (nested lists preserved) |
| any other `##` section | a `## Project Context` block appended to every issue |

Headings accept `:` or `-` as a separator (`## Epic 1: …` and `## Epic 1 - …`
both work), and `##`/`###` levels are case-insensitive.

## Issue body layout

Each issue is self-contained: it carries the epic it belongs to, the epic goal,
the user story, the acceptance criteria, and any project-wide context sections
from the source document.

```markdown
## Epic 1: Project Scaffolding, Quality Gates & CLI Skeleton

**Goal:** Set up the Kotlin Gradle project with strict quality tooling.

**As a** developer,
**I want** a baseline Kotlin project,
**So that** quality gates are enforced from day one.

## Acceptance Criteria

- [ ] `./gradlew check` passes.
- [ ] `./gradlew jacocoTestReport` fails below 80% coverage.

## Project Context

### Architecture & Technology Stack

* **Language:** Kotlin
```

Use `--no-context` to drop the context block, or `--no-checkboxes` to render
acceptance criteria as plain bullets instead of a task list.

## Options

| Option | Description |
| --- | --- |
| `--repo owner/name` | target repository (default: the current repository) |
| `--dry-run` | print what would be created, write nothing to GitHub |
| `--no-context` | omit project context sections from issue bodies |
| `--no-checkboxes` | render acceptance criteria as plain bullets |
| `--label NAME` | label to add to every issue (repeatable, created if missing) |
| `--epic-label` | also add an `epic-N` label to each issue |
| `--assignee USER` | assignee for every issue (repeatable, `@me` is supported) |
| `--title-prefix TEXT` | prefix added to every issue title |
| `--update` | update the body of issues that already exist |
| `--max-pages N` | pages of existing issues to scan when matching (100 each, default 10) |

## Behaviour

- **Idempotent.** Re-running matches existing milestones and issues by title
  and skips them, so nothing is duplicated. `--update` refreshes the body of
  issues that already exist.
- **Milestones** are reused whether they are open or closed. A milestone
  description lists the goal and the stories it contains.
- **Labels** passed with `--label` are created if the repository does not have
  them yet.
- **Orphan stories** — a `### Story` heading that appears before any
  `## Epic` — are reported on stderr and skipped.
- **Existing issues are scanned** page by page (up to `--max-pages`); a warning
  is printed if the scan was truncated, since issues beyond the scan may not be
  detected as duplicates.

## Development

Single file, no dependencies, no build step:

```bash
python3 md2issues.py --help
python3 -m py_compile md2issues.py
```
