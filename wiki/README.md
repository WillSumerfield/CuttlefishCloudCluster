# CCC Wiki

Quartz v5 site for the project wiki. Pages live in `content/`; the rules and page template are in the repo's
`CLAUDE.md`.

```bash
./setup.sh                  # first time: fetches Quartz, installs packages and plugins
npx quartz build --serve    # http://localhost:8080
python3 check_links.py      # after a build: report broken links
```

Only the wiki's own files are committed (`content/`, `quartz.config.yaml`, `plugins/`, these scripts); `setup.sh`
fetches Quartz itself at a pinned commit.

`plugins/graph` is a local copy of `quartz-community/graph`, changed to colour nodes by Section (the `sections`
option in `quartz.config.yaml`), draw Nodes larger, ring the current page, keep labels readable and inside the view,
and show a Section legend. Search its source for "Wiki:" to find the changes.
