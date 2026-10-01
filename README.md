# rpp-registry

The plugin index for [rpp](https://github.com/chunkzero/rpp). rpp reads this repository over
plain HTTP from `https://raw.githubusercontent.com/chunkzero/rpp-registry/main/`.

## Layout

- `plugins/<name>.json`: one file per plugin, edited by hand.
- `index.json`: generated summary (name, description, repository, latest version). Never edit it
  directly; run `python3 scripts/registry.py index`.

```json
{
  "name": "window",
  "repository": "https://github.com/chunkzero/window",
  "description": "Compiles TypeScript-authored UIs into resource-pack assets and Kotlin bindings",
  "versions": [
    { "version": "0.1.0",
      "url": "https://github.com/chunkzero/window/releases/download/v0.1.0/window-0.1.0.rpp.tgz",
      "sha256": "<64 lowercase hex>", "rpp": ">=0.2", "yanked": false }
  ]
}
```

- `name` matches `^[a-z0-9][a-z0-9_-]*$` (at most 64 characters) and equals the file name.
- `repository` is `https://github.com/<owner>/<repo>`.
- `versions` is oldest-first, unique semver versions. `url` must start with
  `<repository>/releases/download/`. `rpp` is the supported rpp semver range. `yanked` is optional.

A release archive is a gzipped tar with its entries at the archive root (no wrapper directory),
containing `rpp.json` with the same `name` and `version`, and only regular files and directories.

## Publishing a version

1. Build the release archive and its hash with `rpp plugin pack`, and upload the archive to a
   GitHub release in the plugin repository.
2. Open a PR that appends the version to the `versions` of `plugins/<name>.json` (or adds a new
   plugin file) and regenerates the index with `python3 scripts/registry.py index`.
3. CI runs `python3 scripts/registry.py check --base origin/main`, which downloads each new
   archive, verifies its hash and contents, and rejects changes to published versions.

Every PR needs review from the repository owner (see `.github/CODEOWNERS`).

## Yanking

Set `"yanked": true` on a version and regenerate `index.json`. Published versions are otherwise
immutable: they cannot be edited, reordered, removed, or un-yanked. A plugin whose versions are
all yanked disappears from `index.json`.

## Development

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/registry.py check
```
