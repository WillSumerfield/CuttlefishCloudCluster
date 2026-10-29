# CCC — working notes

## Temporary plans
- Put temporary/working plans in the repo-local `temp/` folder (gitignored), not `docs/`.
- `docs/` is durable user-facing documentation (e.g. `Architecture.md`); `temp/` is scratch.

## Wiki

The wiki is the project's documentation — for Users, donors, Admins and contributors alike. It lives in
`wiki/`, with pages in `wiki/content/`. The Head serves it at `/wiki/` to signed-in Users (built by
`cuttle setup --head` / `cuttle update`). The Dashboard links to it once, under the name in the rail; the wiki
links back from its sidebar, and from pages that describe a Dashboard page.

- **Read first.** Before starting a task, read the relevant Section Node and pages.
- **View it.** `cd wiki && ./setup.sh` once (fetches Quartz), then `npx quartz build --serve` →
  http://localhost:8080.

### Terms

- **Node**: a page whose main job is to group other pages (its children); it may also have content of its own. It is
  a folder's `index.md`. A **Leaf** is a page about one specific thing, with no children. A Leaf is promoted to a
  Node once pages hang off it: move `X.md` to `X/index.md` and rewrite every link to it.
- **Parent**: every page has exactly one, which is the folder it sits in (a Section's Parent is the home page). Any
  other connection is a link in Relates To.
- **Sections** (top-level folders):
  - `Start`: what CCC is, getting set up, and the glossary.
  - `Using CCC`: working in your Tidepool and Dives: files, Karma, environments, the CLI and Dashboard.
  - `Donating`: lending a machine to the cluster as an Arm.
  - `Admin`: running the Head: setup, users, mesh, alerts, backup, security.
  - `Contributing`: how CCC is built, for people changing it.

### Rules

1. **Write for people.** Human-readable above all: colloquial, clear, easy to skim — plain words, short
   sentences, "you" in guides, an example before the abstraction. Especially guides and the glossary.
   Design rationale and internals go in Contributing, never in user pages.
2. **New topics get pages, with permission.** When a session explores something with no page, propose the page and
   ask first. Show the draft for confirmation before finishing.
3. **Ask before big changes.** Big changes to existing pages also need asking first. Small edits (a line, a link, a
   fix) go ahead and are listed in the final message.
4. **Keep the glossaries in step.** CONTEXT.md is the authority on what a term means; the wiki's `Start/Glossary`
   says the same in plain language, and the home page's "main words" in one or two sentences. When a definition
   changes, update all of them in the same task.
5. **Link to the Dashboard where it helps.** A page about something you see in the Dashboard (Karma, Arms,
   Files, a Dive, Users) gives a plain link to it, e.g. `[Open Karma in the Dashboard](dashboard:/karma)`
   (Quartz would turn a plain `/karma` into a wiki link; the Head's build rewrites `dashboard:`). The link
   checker doesn't see these: if a Dashboard route moves, grep the wiki for it.
6. **Commits.** Commit wiki changes yourself, in small commits of their own, separate from code changes. Never
   delete a page unasked.

### Page template

A default, not a rule: use the headings that suit the page. A guide or the home page reads better as plain
prose ("CCC is…") than under `## Summary`. Keep the frontmatter, and on Nodes a list of children.

```markdown
---
title: <Page>
tags: [leaf]          # or [node]
---
## Summary
1–2 sentences: what this is at a high level.

## Details
What we've learned or noted.

## Contains            <- Nodes only
- [[Child]]: few words

## Relates To
- [[Parent]]: parent
- [[Other]]: few words on why (repeat links from above)
```

### Links

The graph draws edges only from links, so the first Relates To bullet is always the Parent.

- **Leaves** are linked by name: `[[Some Leaf]]`.
- **Nodes** must be linked by full path: `[[Section/Topic/index|Topic]]`. A bare `[[Topic]]` does not resolve to a
  folder index.

After editing, check that no link is broken:

```bash
cd wiki && npx quartz build && python3 check_links.py
```

If `npx quartz build --serve` is running, stop it first and restart it afterwards: a separate build overwrites the
server's files (pages 404 or show an empty graph), and the server's own quick rebuilds can report false broken links
for new pages.
