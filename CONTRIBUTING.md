# Contributing to Redstone Panel

Thanks for helping. Redstone Panel is a free (MIT), self-hosted panel for Minecraft Java and
Bedrock servers, written and maintained by one developer. Bug reports, ideas and pull requests
are all welcome.

## Before you start

- **Security problems:** do not open an issue or pull request. Follow the
  [security policy](SECURITY.md) and report it privately.
- **Bugs:** open an [issue](https://github.com/qrussell/redstonepanel/issues) with your
  version, your system (for example Ubuntu 24.04 or Raspberry Pi OS) and the steps to see it.
- **Ideas and questions:** use [Discussions](https://github.com/qrussell/redstonepanel/discussions).
  For a bigger change, start a discussion first so we can agree on the approach before you
  write the code.
- **Chat:** the [RedstonePanel Discord](https://discord.gg/MpWzy84Eh) for quick questions and release
  announcements. Decisions and bug reports still go in Discussions and issues, so they can be
  found later.

## How pull requests work here

Open pull requests on **GitHub** as usual: fork
[qrussell/redstonepanel](https://github.com/qrussell/redstonepanel), push a branch, and open a
pull request against `main`. GitHub Actions runs CodeQL on it.

One thing is different from most projects. **GitHub is a mirror.** The main repository lives
on the maintainer's own GitLab server, and GitLab pushes `main` and the tags to GitHub. So
pull requests are never merged with GitHub's **Merge** button (the next mirror push would undo
it). Instead, once your pull request is approved:

1. The maintainer brings your branch into GitLab as a merge request. Your commits and your
   name as their author are kept.
2. GitLab runs its own checks, and the change is merged there. Releases go out in version
   order, so it may wait for the release it belongs to.
3. GitLab mirrors `main` to GitHub. Your commits appear on `main`, and GitHub marks your pull
   request as merged by itself. If your commits were squashed into one, the pull request is
   closed with a link to the GitLab merge request instead.

You don't need a GitLab account, and you don't need to do anything differently.

## What makes a pull request easy to take

- **One change per pull request**, small enough to review.
- **Follow the design rules** in [docs/design/roadmap.md](docs/design/roadmap.md). The ones
  that matter most:
  - Protect people's worlds first. Anything that could lose data needs a backup or an undo.
  - The panel runs as **root**, so every feature that touches files or runs commands is a
    security feature. Keep file access inside the server's folder and run game servers as the
    `minecraft` user.
  - Use the Python standard library and the packages Ubuntu ships (`python3-fastapi` and
    friends). No new pip dependencies.
  - Use systemd for anything that must keep working while the panel is stopped.
  - New code that touches the operating system goes through `core/platform`.
- **Frontend:** plain JavaScript in `static/js/`, no framework. If you change Tailwind classes,
  run `packaging/build-css.sh` and commit `static/css/tailwind.css`. Every screen must also work
  on a phone (390 x 844).
- **Tests:** add or update unit tests in `tests/` and run them:

  ```bash
  python3 -m unittest discover -s tests -v
  ```

  They need `pip install -r requirements-dev.txt`. The README's
  [Building from source](README.md#building-from-source) section explains the .deb build and the
  install tests in Docker.
- **Shell scripts** keep LF line endings. Every new `core/*.py` file goes in
  `debian/redstonepanel.install`.
- **Don't bump the version** or edit `debian/changelog`, `CHANGELOG` or `release-notes/`. That
  happens at release time.
- **Docs:** if users will see the change, update the user manual (`docs/guide/README.md`) and
  the wiki pages in `docs/wiki/`. Never edit the wikis directly, they are generated from the
  repository.

## License

By contributing, you agree that your contribution is licensed under the project's
[MIT license](LICENSE).
