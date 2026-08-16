# Getting a static shpool binary

The kit is built and tested against shpool 0.11.0, and the install preflight
refuses any other version (`lib/sh/session_kit_checks.sh`). Pin the version in
both routes below — an unpinned `cargo install shpool` fetches whatever is
current on crates.io, and the preflight then rejects it.

**1. cargo** — if the server has a Rust toolchain:

```bash
cargo install shpool --version 0.11.0 --locked
```

**2. Container build** — when the server has no compiler. A throwaway
container produces a fully static musl binary and copies it out; nothing is
installed on the host. Tested with shpool 0.11.0:

```bash
mkdir -p ~/shpool-build
docker run --rm --cpus 2 -v ~/shpool-build:/out rust:alpine sh -c '
  apk add -q musl-dev &&
  cargo install shpool --version 0.11.0 --locked --quiet &&
  cp /usr/local/cargo/bin/shpool /out/ &&
  /out/shpool version
'
mkdir -p ~/.cargo/bin
install -m 755 ~/shpool-build/shpool ~/.cargo/bin/shpool
~/.cargo/bin/shpool version   # -> shpool 0.11.0
```

The shpool GitHub releases page may carry prebuilt archives for several Linux
architectures and C-library targets, but Session Kit accepts only shpool
0.11.0. Use one of the two pinned routes above unless an official archive is
that exact version and you verify it before installation.

Podman works the same way: replace `docker run` with `podman run`. The build
needs no privileges beyond running a container, and the container is discarded
when it exits.

The container route yields a static-pie executable for the container host's
architecture (check with `file`), independent of the host's glibc version.
Session Kit does not require `~/.cargo/bin/shpool`: installation records the
absolute result of `command -v shpool` in the generated service definition.
