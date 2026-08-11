# Getting a static shpool binary

The kit is built and tested against shpool 0.11.0. Two ways to get it:

**1. cargo** — if the server has a Rust toolchain:

```bash
cargo install shpool
```

**2. Container build** — when the server has no compiler. A throwaway
container produces a fully static musl binary and copies it out; nothing is
installed on the host. Tested with shpool 0.11.0:

```bash
mkdir -p ~/shpool-build
docker run --rm --cpus 2 -v ~/shpool-build:/out rust:alpine sh -c '
  apk add -q musl-dev &&
  cargo install shpool --locked --quiet &&
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

Either route yields a static-pie executable (check with `file`) — it runs on
any x86-64 Linux regardless of glibc version, which is why the kit keeps the
binary at `~/.cargo/bin/shpool` and the systemd unit points there.
