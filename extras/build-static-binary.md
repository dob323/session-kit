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

The shpool GitHub releases page does carry prebuilt static tarballs
(x86_64/aarch64, gnu and musl), but they lag well behind — 0.6.3 at the time
of writing against 0.11.0 on crates.io — and the kit's config is untested
against them. Use one of the two routes above.

Podman works the same way: replace `docker run` with `podman run`. The build
needs no privileges beyond running a container, and the container is discarded
when it exits.

Either route yields a static-pie executable (check with `file`) — it runs on
any x86-64 Linux regardless of glibc version, which is why the kit keeps the
binary at `~/.cargo/bin/shpool` and the systemd unit points there.
