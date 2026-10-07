# opencode-mem

Fork of [opencode](https://github.com/sst/opencode) v1.18.30 (`juagamesacc-pixel/opencode-mem`, branch `main`).

## Changes vs original opencode

- **Goal-pinned compaction:** built-in compaction prompt (`packages/opencode/src/agent/prompt/compaction.txt`) uses the custom goal-pinned strategy (instructions/constraints only; wiring and file format untouched).
- **MCP session id:** MCP tool calls carry an optional session id via `_meta`, so MCP servers can correlate calls within a session.
- **3-target release flow:** additive `--targets=a,b,c` flag in `packages/opencode/script/build.ts` (intersected with `allTargets`; default build behavior unchanged) plus `.github/workflows/release-3target.yml`, which builds and uploads exactly 3 assets: `linux-x64`, `linux-arm64`, `darwin-arm64`.
- **Restored assets:** code-needed assets that the upstream build expects are restored so the production build works from this fork.

## Install (one line per OS)

Binaries install to `~/.opencode/bin/opencode`. Add that directory to your `PATH` if needed (`export PATH="$HOME/.opencode/bin:$PATH"`).

**Linux x64 (glibc, AVX2):**

```sh
mkdir -p ~/.opencode/bin /tmp/opencode-dl && curl -fsSL https://github.com/juagamesacc-pixel/opencode-mem/releases/latest/download/opencode-linux-x64.tar.gz -o /tmp/opencode-dl/pkg.tar.gz && tar -xzf /tmp/opencode-dl/pkg.tar.gz -C /tmp/opencode-dl && mv /tmp/opencode-dl/opencode ~/.opencode/bin/opencode && chmod +x ~/.opencode/bin/opencode && ~/.opencode/bin/opencode --version
```

**Linux ARM64 (glibc):**

```sh
mkdir -p ~/.opencode/bin /tmp/opencode-dl && curl -fsSL https://github.com/juagamesacc-pixel/opencode-mem/releases/latest/download/opencode-linux-arm64.tar.gz -o /tmp/opencode-dl/pkg.tar.gz && tar -xzf /tmp/opencode-dl/pkg.tar.gz -C /tmp/opencode-dl && mv /tmp/opencode-dl/opencode ~/.opencode/bin/opencode && chmod +x ~/.opencode/bin/opencode && ~/.opencode/bin/opencode --version
```

**macOS ARM64 (Apple Silicon, e.g. MacBook Air M2):**

```sh
mkdir -p ~/.opencode/bin /tmp/opencode-dl && curl -fsSL https://github.com/juagamesacc-pixel/opencode-mem/releases/latest/download/opencode-darwin-arm64.zip -o /tmp/opencode-dl/pkg.zip && unzip -o /tmp/opencode-dl/pkg.zip -d /tmp/opencode-dl && mv /tmp/opencode-dl/opencode ~/.opencode/bin/opencode && chmod +x ~/.opencode/bin/opencode && ~/.opencode/bin/opencode --version
```

**Note:** musl (Alpine-style) and non-AVX2 (baseline) hosts are not covered by these 3 assets. Those variants exist in the upstream 12-target matrix but are intentionally excluded from this minimal release flow.
