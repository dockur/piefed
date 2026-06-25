<h1 align="center">PieFed<br />
<div align="center">
<a href="https://github.com/dockur/piefed"><img src="https://raw.githubusercontent.com/dockur/piefed/master/.github/logo.png" title="Logo" style="max-width:100%;" width="128" /></a>
</div>
<div align="center">

[![Build]][build_url]
[![Version]][tag_url]
[![Size]][tag_url]
[![Package]][pkg_url]
[![Pulls]][hub_url]

</div></h1>

Docker container of [PieFed](https://join.piefed.social/), a Lemmy/Mbin alternative written in Python with Flask.

 - Clean, simple code that is easy to understand and contribute to. No fancy design patterns or algorithms.
 - Easy setup, easy to manage - few dependencies and extra software required.
 - [First class moderation tools](https://join.piefed.social/2024/06/22/piefed-features-for-growing-healthy-communities/).

## Project goals

To build a federated discussion and link aggregation platform, similar to Reddit, Lemmy, Mbin interoperable with as
much of the fediverse as possible.

## For developers

- [Screencast: overview of the PieFed codebase](https://join.piefed.social/2024/01/22/an-introduction-to-the-piefed-codebase/)
- [Database / entity relationship diagram](https://join.piefed.social/wp-content/uploads/2024/02/PieFed-entity-relationships.png)
- API Documentation:
  - Stable branch: [https://stable.wjs018.xyz/api/alpha/swagger](https://stable.wjs018.xyz/api/alpha/swagger)
  - Development branch: [https://crust.piefed.social/api/alpha/swagger](https://crust.piefed.social/api/alpha/swagger) or [https://piefed.wjs018.xyz/api/alpha/swagger](https://piefed.wjs018.xyz/api/alpha/swagger)
- see [INSTALL.md](INSTALL.md) or [INSTALL-docker.md](INSTALL-docker.md)
- Asking questions about the project:
	- Open a new thread in our [Piefed Devellopers community](https://piefed.social/c/piefed_dev)
	- Chat in realtime with piefed developers in our [Matrix room](https://matrix.to/#/#piefed-developers:matrix.org)
- see docs/project_management/* for a project roadmap, contributing guide and much more.

## Stars 🌟
[![Stargazers](https://raw.githubusercontent.com/star-stats/stars/refs/heads/data/charts/dockur-piefed.svg)](https://github.com/dockur/piefed/stargazers)

[build_url]: https://github.com/dockur/piefed
[hub_url]: https://hub.docker.com/r/dockurr/piefed
[tag_url]: https://hub.docker.com/r/dockurr/piefed/tags
[pkg_url]: https://github.com/dockur/piefed/pkgs/container/piefed

[Build]: https://github.com/dockur/piefed/actions/workflows/build.yml/badge.svg
[Size]: https://img.shields.io/docker/image-size/dockurr/piefed/latest?color=066da5&label=size
[Pulls]: https://img.shields.io/docker/pulls/dockurr/piefed.svg?style=flat&label=pulls&logo=docker
[Version]: https://img.shields.io/docker/v/dockurr/piefed/latest?arch=amd64&sort=semver&color=066da5
[Package]: https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fipitio.github.io%2Fbackage%2Fdockur%2Fpiefed%2Fpiefed.json&query=%24.downloads&logo=github&style=flat&color=066da5&label=pulls
