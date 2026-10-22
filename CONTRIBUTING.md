# Contributing to the Kaiten MCP server

Thank you for contributing to the Kaiten MCP server.

This project is licensed under the Apache License, Version 2.0.

## Developer Certificate of Origin

Kaiten uses the Developer Certificate of Origin 1.1 (DCO) for contributions.

Every commit must include a `Signed-off-by` trailer matching the commit author.

Use:

```bash
git commit -s -m "Describe your change"
```

This produces:

```text
Signed-off-by: Jane Doe <jane@example.com>
```

`git commit -s` is a **DCO sign-off**: it certifies the contribution under
[DCO.md](./DCO.md). `git commit -S`, with a capital S, is a **cryptographic
commit signature** made with a GPG or SSH key. They are not the same thing, and
the sign-off is what this project requires. There is nothing to install.

### Fixing a missing sign-off

A check named **DCO** verifies every commit of every pull request, and it is the
final word. If it fails, it names the commits to fix.

For the latest commit:

```bash
git commit --amend --signoff --no-edit
git push --force-with-lease
```

For several commits, sign off every commit of your branch at once:

```bash
git rebase --signoff origin/main
git push --force-with-lease
```

## Before you open a pull request

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Every Python file opens with the project's license notice, and the tests check it:

```python
# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0
```

## Contribution rules

Please:

- keep pull requests focused;
- add or update tests when behavior changes;
- update documentation when relevant;
- do not include secrets, customer data, or confidential information;
- do not submit code or assets that you do not have the right to contribute;
- preserve required third-party license and attribution notices.

Accepted contributions are contributed under Apache-2.0.

## Releasing

Releases are made by maintainers. A merge to `main` publishes the container image
`ghcr.io/kaitencloud/kaiten-mcp:main`. To release a version, set `version` in
`pyproject.toml`, merge it, and push the tag:

```bash
git tag v0.3.0
git push origin v0.3.0
```

CI then publishes the image as `:0.3.0` and `:0.3`, and the Python package to PyPI as
[`kaitencloud-mcp`](https://pypi.org/project/kaitencloud-mcp/). The `publish` job refuses a
tag that does not match `pyproject.toml`'s version.

PyPI publishing uses trusted publishing, so no token is stored: the project's publisher on
PyPI must name this repository, the `CI` workflow (`ci.yml`) and the `pypi` environment.
The image's package is created by the first publish and takes the repository's visibility,
so the repository must be public before then.

## Security

Do not report unpatched vulnerabilities in public issues.

See [SECURITY.md](./SECURITY.md).
