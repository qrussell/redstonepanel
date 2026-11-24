# Notes for AI assistants

Redstone Panel is a self-hosted Minecraft server panel (Python/FastAPI backend in `main.py`
and `core/`, plain-JS frontend in `static/`, packaged as a .deb, runs as root under systemd).

- **GitLab is the source of truth; GitHub is a push mirror.** Never push or merge on GitHub.
  Every change, including `.github/` files, goes through a GitLab merge request.
- Outside pull requests and Dependabot pull requests on GitHub are brought into GitLab, not
  merged on GitHub. Releases merge in version order; don't bump versions outside a release.
- How it all works, step by step, and where the security features are:
  [docs/maintainers/README.md](docs/maintainers/README.md).
- Design rules: [docs/design/roadmap.md](docs/design/roadmap.md). Contributor rules:
  [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).
- Tests: `python3 -m unittest discover -s tests -v` (needs `pip install -r requirements-dev.txt`).
