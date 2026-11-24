# Maintainer guide: repositories, pull requests and security

This page is for the maintainer and for AI assistants working on Redstone Panel. It explains
how the two repositories fit together, how outside pull requests and Dependabot updates get
in, and where the security features live. Contributors only need
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## The repositories

| | Role |
| --- | --- |
| **GitLab** `https://gitlab.cielocloud.dev/lab/redstonepanel` | **Source of truth.** All changes merge here through merge requests into `main`. GitLab CI (`.gitlab-ci.yml`) runs the checks, unit tests on each supported system, the .deb build and the GitLab release. |
| **GitHub** `https://github.com/qrussell/redstonepanel` | **Push mirror** of GitLab (remote mirror id 14, all branches and tags). Hosts the public face: issues, Discussions, pull requests, releases, the wiki, GitHub Pages and the security features. GitHub Actions (`release.yml`) runs the install, ARM64 and cluster tests and publishes the GitHub release. |

Website: <https://redstonepanel.cielocloud.org> (GitHub Pages, `docs/CNAME`).

If `gitlab.cielocloud.dev` answers 502 (the home internet connection is down), the same GitLab
is reachable at `https://gitlab-cf.cielocloud.dev` (a Cloudflare tunnel), for git and the API.

### Rules

1. **Never push or merge on GitHub.** The mirror overwrites GitHub's branches with GitLab's on
   every run, so anything merged or pushed only on GitHub is lost (or makes the mirror fail).
   In a local clone, keep the `github` remote's push URL disabled.
2. **Every change goes through a GitLab merge request.** That includes files that only matter
   on GitHub (`.github/`, `SECURITY.md`, `CONTRIBUTING.md`).
3. **Releases merge in version order.** Several features can be built in parallel, but they
   merge and release in roadmap order, and each release's notes describe only its own change.
   Version, `debian/changelog`, `CHANGELOG` and release notes change only at release time. See
   the README's [Releases](../../README.md#releases) section.
4. **No two-way sync.** Don't set up a pull mirror from GitHub to GitLab: both sides would
   fight over `main`.
5. After a merge or a release tag, the mirror can take minutes. To push it now:
   `POST /api/v4/projects/lab%2Fredstonepanel/remote_mirrors/14/sync` (answers 204).

## Bringing in a GitHub pull request

Contributors open pull requests on GitHub (see CONTRIBUTING.md). Review and discuss them there,
but don't press **Merge**. When the maintainer says to take one (pull request number `N`):

```bash
# In a clone of GitLab (origin = GitLab)
git fetch https://github.com/qrussell/redstonepanel.git pull/N/head:contrib/pr-N
git checkout contrib/pr-N
git rebase origin/main        # only if it is behind; rebasing keeps each commit's author
git push -u origin contrib/pr-N
```

Then open a GitLab merge request from `contrib/pr-N` into `main`:

- Title: the pull request's title. Description: a link to the GitHub pull request, and
  `Co-authored-by` credit if the maintainer adds commits.
- Keep the contributor's commits and authorship. Prefer a normal merge (no squash), so the
  pull request's commits land on `main` unchanged.
- Fixes the maintainer wants on top go as extra commits on the same branch.
- GitLab CI must pass, and if it belongs to a release, it merges in version order.

When GitLab mirrors `main`, GitHub sees the pull request's commits on `main` and marks it
**merged** by itself. If the commits changed (squashed or rebased with edits), GitHub can't
tell: close the pull request with a comment linking the GitLab merge request and the commit on
`main`.

Workflows on outside pull requests run with read-only tokens and no secrets. Don't add
`pull_request_target` workflows that check out the pull request's code.

## Dependabot pull requests

`.github/dependabot.yml` asks Dependabot to check, every Monday:

- **GitHub Actions** used in `.github/workflows/` (version updates, grouped into one pull
  request).
- **pip**, `requirements-dev.txt`: only what CI installs for the unit tests. The panel itself
  runs on the system's apt packages (`python3-fastapi` and friends), which Dependabot can't
  see. The lower bounds there deliberately follow Ubuntu 24.04, the oldest supported system,
  so version-update pull requests for pip are turned off (`open-pull-requests-limit: 0`).
  Security alerts for these packages still appear under Dependabot alerts.

Dependabot opens its pull requests on GitHub. Treat them exactly like an outside pull request:
redo the change in a GitLab merge request (or fetch `pull/N/head` as above) and let the mirror
close it. If the change isn't wanted, close the pull request on GitHub.
**Dependabot security updates** (automatic pull requests for alerts) are off for the same
reason; read the alerts and fix them through GitLab.

## Security features (GitHub, Security tab)

| Feature | Where it is set up | Notes |
| --- | --- | --- |
| Security policy | `SECURITY.md` | How to report, what to expect (first answer in 7 days, fix normally within 30), what is by design. |
| Private vulnerability reporting | Settings → Advanced Security | Reports arrive under Security → Advisories. |
| Security advisories | Security → Advisories | Publish one, with a CVE if needed, when a fix is released. The fix itself goes through GitLab like any change. |
| Dependency graph and Dependabot alerts | Settings → Advanced Security | Alerts under Security → Dependabot. |
| Dependabot version updates | `.github/dependabot.yml` | See above. |
| Code scanning (CodeQL) | `.github/workflows/codeql.yml`, `.github/codeql/codeql-config.yml` | Python, JavaScript/TypeScript and the workflows, `security-extended` queries, on every `main` push of the mirror, on pull requests, and weekly. Separate from `release.yml`, so a finding never blocks a release. Results under Security → Code scanning. |
| Secret scanning and push protection | Settings → Advanced Security | Alerts under Security → Secret scanning. Push protection blocks pushes to GitHub only; GitLab pushes reach GitHub through the mirror, so keep secrets out of commits in the first place. |

Code scanning alerts: CodeQL doesn't know about the panel's own guards (server names checked
against `minecraft-*` units, `core/files.py` opening each path component with `O_NOFOLLOW`,
the admin-only routes), so many "path expression" and "server-side request forgery" alerts
are already handled. For each one: check the code path, fix real problems in a GitLab merge
request, and dismiss the rest on GitHub with the reason ("false positive" or "used in tests")
so the list stays useful.

### Notes for AI assistants

- Read this page and `docs/design/roadmap.md` before changing anything; check the current
  version with `head -1 debian/changelog`.
- Work on a branch in GitLab and open a merge request there. Don't push to GitHub, merge
  GitHub pull requests, or enable settings that make GitHub write to the repository (for
  example Dependabot security updates with auto-merge).
- Repository settings (Security tab toggles, Discussions, releases) are changed on GitHub
  by the maintainer, or by an assistant only when the maintainer asks.
- Taking an outside contribution, publishing an advisory and closing pull requests are the
  maintainer's call: prepare them, then ask.
