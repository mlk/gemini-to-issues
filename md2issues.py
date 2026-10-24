#!/usr/bin/env python3
"""Turn a Markdown "Epics & User Stories" document into GitHub issues and milestones.

The document format is expected to look like this::

    # Project Title

    ## Architecture & Technology Stack
    * **Language:** Kotlin

    ## Definition of Done (DoD)
    1. **CI Pipeline:** ...

    ## Epic 1: Project Scaffolding
    > **Goal:** Set up the project ...

    ### Story 1.1: Initialize the Project
    * **As a** developer,
    * **I want** a baseline project,
    * **So that** quality gates are enforced.
    * **Acceptance Criteria:**
      * `./gradlew check` passes.
      * ...

Every ``## Epic`` heading becomes a GitHub milestone and every ``### Story``
heading becomes an issue attached to that milestone.  Issue bodies are made
self-contained: they repeat the epic goal, the user story, the acceptance
criteria and any top-level context sections (architecture, definition of done)
from the source document.

Authentication is delegated to the ``gh`` CLI, so no tokens are handled here.

Usage:
    python3 md2issues.py epics.md
    python3 md2issues.py epics.md --repo owner/name --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

EPIC_RE = re.compile(r"^epic\s+(\d+(?:\.\d+)*)\s*[:\-\u2013\u2014]\s*(.+)$", re.IGNORECASE)
STORY_RE = re.compile(r"^story\s+(\d+(?:\.\d+)*)\s*[:\-\u2013\u2014]\s*(.+)$", re.IGNORECASE)

# "  * **Acceptance Criteria:**" / "* **As a** developer,"
LABEL_RE = re.compile(r"^(?P<indent>[ \t]*)[*+-]\s+\*\*(?P<label>[^*]+?):?\*\*\s*(?P<rest>.*)$")
BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)[*+-]\s+(?P<text>.*)$")
NUMBER_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>\d+[.)])\s+(?P<text>.*)$")
HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
GOAL_LABEL_RE = re.compile(r"^\*\*(?P<label>[^*]+?):?\*\*\s*(?P<rest>.*)$", re.DOTALL)

NARRATIVE_LABELS = {
    "as", "as a", "an", "as an", "as the", "as a user", "as the user",
    "i want", "i need", "i wish", "so that", "in order that",
    "given", "when", "then",
}

CRITERIA_LABELS = {
    "acceptance criteria", "acceptance criteria(s)", "criteria", "ac",
    "definition of done", "dod", "acceptance criteria for the story",
}


# --------------------------------------------------------------------------- #
# Document model
# --------------------------------------------------------------------------- #

@dataclass
class Block:
    """A labelled chunk of a story body, e.g. ``**Acceptance Criteria:**``."""

    label: Optional[str] = None
    head: str = ""
    content: List[str] = field(default_factory=list)


@dataclass
class Story:
    number: str
    title: str
    blocks: List[Block] = field(default_factory=list)


@dataclass
class Epic:
    number: str
    title: str
    goal: str = ""
    notes: List[str] = field(default_factory=list)
    stories: List[Story] = field(default_factory=list)


@dataclass
class Document:
    title: str = ""
    preamble: List[str] = field(default_factory=list)
    context: List[Tuple[str, List[str]]] = field(default_factory=list)
    epics: List[Epic] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def clean_lines(lines: List[str]) -> List[str]:
    """Drop horizontal rules and trailing blank lines."""
    kept = [ln.rstrip() for ln in lines if not HR_RE.match(ln)]
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def split_blocks(lines: List[str]) -> List[Block]:
    """Split a story body into labelled blocks.

    Only bullets at the story's base indentation start a new block, so nested
    bullets that happen to contain bold text stay part of their parent block.
    """
    indents = [len(m.group("indent")) for m in (BULLET_RE.match(ln) for ln in lines) if m]
    base = min(indents) if indents else 0

    blocks: List[Block] = []
    current: Optional[Block] = None
    for raw in lines:
        m = LABEL_RE.match(raw)
        if m and len(m.group("indent")) <= base:
            current = Block(
                label=m.group("label").strip().rstrip(":").strip(),
                head=m.group("rest").strip(),
            )
            blocks.append(current)
            continue
        if current is None:
            if raw.strip():
                blocks.append(Block(label=None, head="", content=[raw]))
        else:
            current.content.append(raw)
    return blocks


def extract_goal(quote_lines: List[str]) -> str:
    """Pull the ``**Goal:**`` sentence out of an epic's blockquote."""
    text = "\n".join(quote_lines).strip()
    if not text:
        return ""
    m = GOAL_LABEL_RE.match(text)
    if m:
        label = m.group("label").strip().rstrip(":").lower()
        if label in {"goal", "goals", "objective", "objectives", "purpose"}:
            return m.group("rest").strip()
    return text


def parse_document(text: str) -> Document:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    doc = Document()

    ctx_title: Optional[str] = None
    ctx_body: List[str] = []
    epic: Optional[Epic] = None
    story: Optional[Story] = None
    story_body: List[str] = []
    epic_quotes: List[str] = []
    epic_notes: List[str] = []

    def end_story() -> None:
        nonlocal story, story_body
        if story is not None:
            story.blocks = split_blocks(story_body)
            if epic is not None:
                epic.stories.append(story)
        story = None
        story_body = []

    def end_context() -> None:
        nonlocal ctx_title, ctx_body
        if ctx_title is not None:
            body = clean_lines(ctx_body)
            if body:
                doc.context.append((ctx_title, body))
        ctx_title = None
        ctx_body = []

    def end_epic() -> None:
        nonlocal epic, epic_quotes, epic_notes
        if epic is not None:
            epic.goal = extract_goal(epic_quotes)
            epic.notes = clean_lines(epic_notes)
            doc.epics.append(epic)
        epic = None
        epic_quotes = []
        epic_notes = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("### "):
            end_story()
            heading = stripped[4:].strip()
            m = STORY_RE.match(heading)
            if m and epic is not None:
                story = Story(number=m.group(1), title=m.group(2).strip())
            elif m:
                print(f"warning: story '{heading}' has no enclosing epic; skipped",
                      file=sys.stderr)
            else:
                end_context()
                ctx_title = heading
                ctx_body = []
            continue

        if stripped.startswith("## "):
            end_story()
            end_epic()
            heading = stripped[3:].strip()
            m = EPIC_RE.match(heading)
            if m:
                epic = Epic(number=m.group(1), title=m.group(2).strip())
            else:
                end_context()
                ctx_title = heading
                ctx_body = []
            continue

        if stripped.startswith("# "):
            end_story()
            end_epic()
            end_context()
            if not doc.title:
                doc.title = stripped[2:].strip()
            continue

        if story is not None:
            story_body.append(line)
        elif epic is not None:
            if stripped.startswith(">"):
                epic_quotes.append(re.sub(r"^\s*>\s?", "", line))
            elif stripped:
                epic_notes.append(line)
        elif ctx_title is not None:
            ctx_body.append(line)
        elif stripped:
            doc.preamble.append(line)

    end_story()
    end_epic()
    end_context()
    return doc


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def is_narrative(label: str) -> bool:
    return label.strip().lower() in NARRATIVE_LABELS


def is_criteria(label: str) -> bool:
    return label.strip().lower() in CRITERIA_LABELS


def render_list(lines: List[str], checkboxes: bool = False) -> str:
    """Render raw list lines, optionally turning top level bullets into tasks."""
    out: List[str] = []
    base: Optional[int] = None
    for raw in lines:
        if not raw.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        m = BULLET_RE.match(raw)
        n = NUMBER_RE.match(raw)
        if not m and not n:
            out.append(raw.strip())
            continue
        indent = len((m or n).group("indent"))
        if base is None:
            base = indent
        depth = max(0, (indent - base) // 2)
        if m:
            marker = "- [ ] " if (checkboxes and depth == 0) else "- "
            out.append("  " * depth + marker + m.group("text").rstrip())
        else:
            out.append("  " * depth + n.group("marker") + " " + n.group("text").rstrip())
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def render_story_body(doc: Document, epic: Epic, story: Story,
                      include_context: bool = True,
                      checkboxes: bool = True) -> str:
    out: List[str] = [f"## Epic {epic.number}: {epic.title}"]
    if epic.goal:
        out.append("")
        out.append(f"**Goal:** {epic.goal}")

    # group consecutive narrative blocks (As a / I want / So that) so that they
    # read as a single user story paragraph
    groups: List[List[Block]] = []
    for block in story.blocks:
        if block.label is not None and is_narrative(block.label):
            if groups and groups[-1][0].label is not None and is_narrative(groups[-1][0].label):
                groups[-1].append(block)
            else:
                groups.append([block])
        else:
            groups.append([block])

    for group in groups:
        block = group[0]
        if block.label is None:
            text = "\n".join(block.content).strip()
            if text:
                out.append("")
                out.append(text)
        elif is_narrative(block.label):
            out.append("")
            for item in group:
                line = f"**{item.label}**"
                if item.head:
                    line += " " + item.head
                out.append(line)
                extra = render_list(item.content, checkboxes=False)
                if extra:
                    out.append(extra)
        else:
            title = block.label
            if block.head:
                title = f"{title} {block.head}"
            out.append("")
            out.append(f"## {title}")
            body = render_list(block.content, checkboxes=checkboxes and is_criteria(block.label))
            if body:
                out.append("")
                out.append(body)

    if include_context:
        sections: List[Tuple[str, List[str]]] = []
        if doc.preamble:
            sections.append(("Overview", clean_lines(doc.preamble)))
        sections.extend(doc.context)
        if sections:
            out.append("")
            out.append("## Project Context")
            for title, body_lines in sections:
                out.append("")
                out.append(f"### {title}")
                out.append("")
                out.append("\n".join(body_lines).rstrip())

    return "\n".join(out).strip() + "\n"


def render_milestone_description(epic: Epic) -> str:
    out: List[str] = []
    if epic.goal:
        out.append(f"**Goal:** {epic.goal}")
    if epic.notes:
        out.append("")
        out.append("\n".join(epic.notes))
    if epic.stories:
        out.append("")
        out.append("**Stories:**")
        out.append("")
        for story in epic.stories:
            out.append(f"- [ ] Story {story.number}: {story.title}")
    return "\n".join(out).strip() + "\n"


# --------------------------------------------------------------------------- #
# GitHub via the gh CLI
# --------------------------------------------------------------------------- #

class GhError(RuntimeError):
    pass


def gh(*args: str, stdin: Optional[str] = None, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise GhError("the `gh` command was not found; install GitHub CLI first")
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise GhError(f"gh {' '.join(args[:3])} failed:\n{message}")
    return proc.stdout


def detect_repo() -> str:
    out = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", check=False)
    repo = out.strip()
    if not repo or "/" not in repo:
        raise GhError("could not detect the target repository; pass --repo owner/name")
    return repo


def current_user() -> str:
    return gh("api", "user", "--jq", ".login").strip()


def resolve_assignees(assignees: List[str]) -> List[str]:
    """Expand ``@me`` into the login of the authenticated user."""
    resolved: List[str] = []
    for name in assignees:
        if name.strip() == "@me":
            login = current_user()
            if login and login not in resolved:
                resolved.append(login)
        elif name.strip() and name.strip() not in resolved:
            resolved.append(name.strip())
    return resolved


def list_milestones(repo: str) -> Dict[str, Tuple[int, str]]:
    out = gh("api", f"repos/{repo}/milestones?state=all&per_page=100",
             "--jq", ".[] | [.number, .title, .state] | @tsv")
    result: Dict[str, Tuple[int, str]] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        number, title, state = line.split("\t", 2)
        result.setdefault(title, (int(number), state))
    return result


def list_issues(repo: str, max_pages: int = 10) -> Tuple[Dict[str, int], bool]:
    """Return {title: number} for every issue (pull requests excluded).

    The second element of the tuple is True when the scan hit ``max_pages``
    and therefore may not have seen every issue in the repository.
    """
    result: Dict[str, int] = {}
    truncated = False
    for page in range(1, max_pages + 1):
        out = gh("api", f"repos/{repo}/issues?state=all&per_page=100&page={page}",
                 "--jq", ".[] | [.number, .title, (has(\"pull_request\") | tostring)] | @tsv")
        rows = [line for line in out.splitlines() if line.strip()]
        for line in rows:
            number, title, is_pr = line.split("\t", 2)
            if is_pr == "true":
                continue
            result.setdefault(title, int(number))
        if len(rows) < 100:
            return result, truncated
        truncated = True
    return result, truncated


def list_labels(repo: str) -> set:
    out = gh("api", "--paginate", f"repos/{repo}/labels?per_page=100", "--jq", ".[].name")
    return {line.strip() for line in out.splitlines() if line.strip()}


def ensure_label(repo: str, name: str, color: str) -> None:
    payload = {"name": name, "color": color}
    gh("api", "--method", "POST", f"repos/{repo}/labels", "--input", "-",
       stdin=json.dumps(payload))


def create_milestone(repo: str, title: str, description: str) -> int:
    payload = {"title": title, "description": description, "state": "open"}
    out = gh("api", "--method", "POST", f"repos/{repo}/milestones", "--input", "-",
             stdin=json.dumps(payload))
    return int(json.loads(out)["number"])


def create_issue(repo: str, title: str, body: str, milestone: Optional[int],
                 labels: List[str], assignees: List[str]) -> int:
    payload: Dict[str, object] = {"title": title, "body": body}
    if milestone is not None:
        payload["milestone"] = milestone
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    out = gh("api", "--method", "POST", f"repos/{repo}/issues", "--input", "-",
             stdin=json.dumps(payload))
    return int(json.loads(out)["number"])


def update_issue(repo: str, number: int, body: str, milestone: Optional[int]) -> None:
    payload: Dict[str, object] = {"body": body}
    if milestone is not None:
        payload["milestone"] = milestone
    gh("api", "--method", "PATCH", f"repos/{repo}/issues/{number}", "--input", "-",
       stdin=json.dumps(payload))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create GitHub milestones and issues from a Markdown epics/stories file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  python3 md2issues.py epics.md --repo owner/name --dry-run\n"
            "  python3 md2issues.py epics.md --label epic --assignee @me\n"
        ),
    )
    parser.add_argument("markdown", help="path to the Markdown file to parse")
    parser.add_argument("--repo", help="target repository as owner/name (default: current repo)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be created without touching GitHub")
    parser.add_argument("--no-context", action="store_true",
                        help="omit top level context sections (architecture, DoD, ...) from issue bodies")
    parser.add_argument("--no-checkboxes", action="store_true",
                        help="render acceptance criteria as plain bullets instead of task lists")
    parser.add_argument("--label", action="append", default=[], metavar="NAME",
                        help="label to add to every issue (repeatable, created if missing)")
    parser.add_argument("--epic-label", action="store_true",
                        help="also add an 'epic-N' label to each issue")
    parser.add_argument("--assignee", action="append", default=[], metavar="USER",
                        help="assignee for every issue (repeatable)")
    parser.add_argument("--title-prefix", default="", metavar="TEXT",
                        help="prefix added to every issue title")
    parser.add_argument("--update", action="store_true",
                        help="update the body of issues that already exist")
    parser.add_argument("--max-pages", type=int, default=10, metavar="N",
                        help="how many pages of existing issues to scan (100 issues each, default 10)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        with open(args.markdown, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"error: cannot read {args.markdown}: {exc}", file=sys.stderr)
        return 1

    doc = parse_document(text)
    if not doc.epics:
        print(f"error: no '## Epic N: ...' headings found in {args.markdown}", file=sys.stderr)
        return 1

    try:
        repo = args.repo or detect_repo()
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        milestones: Dict[str, Tuple[int, str]] = {}
        issues: Dict[str, int] = {}
    else:
        milestones = list_milestones(repo)
        issues, truncated = list_issues(repo, max_pages=args.max_pages)
        if truncated:
            print(f"warning: only the first {args.max_pages * 100} issues were scanned; "
                  f"raise --max-pages if duplicates appear", file=sys.stderr)

    if args.label and not args.dry_run:
        known = list_labels(repo)
        for label in args.label:
            if label not in known:
                ensure_label(repo, label, "ededed")
                print(f"+ label   {label}")

    assignees = resolve_assignees(args.assignee) if args.assignee else []
    if assignees and args.assignee and any(a.strip() == "@me" for a in args.assignee):
        print(f"assignees: {', '.join(assignees)}")

    created_milestones = created_issues = skipped = 0

    for epic in doc.epics:
        milestone_title = f"Epic {epic.number}: {epic.title}"
        description = render_milestone_description(epic)

        milestone_number = 0
        if milestone_title in milestones:
            milestone_number, state = milestones[milestone_title]
            print(f"= milestone  {milestone_title} (exists, #{milestone_number}, {state})")
        elif args.dry_run:
            print(f"+ milestone  {milestone_title}")
            print(description)
            created_milestones += 1
        else:
            milestone_number = create_milestone(repo, milestone_title, description)
            print(f"+ milestone  {milestone_title} (#{milestone_number})")
            created_milestones += 1

        for story in epic.stories:
            issue_title = f"{args.title_prefix}Story {story.number}: {story.title}"
            body = render_story_body(
                doc, epic, story,
                include_context=not args.no_context,
                checkboxes=not args.no_checkboxes,
            )
            labels = list(args.label)
            if args.epic_label:
                labels.append(f"epic-{epic.number}")

            if issue_title in issues:
                skipped += 1
                print(f"= issue      {issue_title} (exists, #{issues[issue_title]})")
                if args.update and not args.dry_run:
                    update_issue(repo, issues[issue_title], body, milestone_number or None)
                    print(f"~ issue      {issue_title} (body updated)")
                continue

            if args.dry_run:
                print(f"+ issue      {issue_title}")
                print("-" * 60)
                print(body)
                print("-" * 60)
            else:
                new_number = create_issue(repo, issue_title, body, milestone_number or None,
                                          labels, assignees)
                issues[issue_title] = new_number
                print(f"+ issue      {issue_title} (#{new_number})")
            created_issues += 1

    print()
    print(f"repo: {repo}")
    print(f"milestones created: {created_milestones}, issues created: {created_issues}, skipped: {skipped}")
    if args.dry_run:
        print("(dry run - nothing was written to GitHub)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
