import path from "node:path"
import { promises as fs } from "node:fs"
import { execFile } from "node:child_process"
import { createInterface } from "node:readline"
import { fileURLToPath } from "node:url"
import { Global } from "@opencode-ai/core/global"
import { InstallationVersion } from "@opencode-ai/core/installation/version"
import { applyEdits, modify, parse } from "jsonc-parser"
import type { ConfigMCPV1 } from "@opencode-ai/core/v1/config/mcp"

/**
 * Option-2 bundling for the python MCP servers (memory + web).
 *
 * The .py sources live in `src/mcp/bundled/` for dev and are embedded into
 * the release binary via `opencode-mcp.gen.ts` (see script/build.ts,
 * mirroring the web-UI bundle). At runtime ensureBundledMcp() materializes
 * them under Global.Path.config/mcp/bundled/ with a .version marker.
 */
export const BUNDLED_MCP_FILES = ["memory_mcp.py", "web_mcp.py"] as const

/** Bump to force a rewrite of already-materialized bundled servers. */
const BUNDLED_MCP_VERSION = 1

export function bundledDir() {
  return path.join(Global.Path.config, "mcp", "bundled")
}

export function bundledMemoryRoot() {
  return path.join(Global.Path.data, "memory")
}

export function bundledMcpDefaults(): Record<string, ConfigMCPV1.Info> {
  const dir = bundledDir()
  return {
    memory: {
      type: "local",
      command: ["python3", path.join(dir, "memory_mcp.py"), "--root", bundledMemoryRoot()],
      cwd: dir,
      enabled: true,
      timeout: 10000,
    },
    web: {
      type: "local",
      command: ["python3", path.join(dir, "web_mcp.py")],
      cwd: dir,
      enabled: true,
      timeout: 600000,
    },
  }
}

async function loadBundledContents(): Promise<Record<string, string>> {
  try {
    // @ts-expect-error - generated file at build time (mirrors server/shared/ui.ts embeddedUI)
    const module = await import("opencode-mcp.gen.ts").then((m) => m.default as Record<string, string>)
    const entries = await Promise.all(
      BUNDLED_MCP_FILES.map(async (name) => {
        const file = module[name]
        if (!file) throw new Error(`bundled MCP file missing from embed: ${name}`)
        return [name, await fs.readFile(file, "utf8")] as const
      }),
    )
    return Object.fromEntries(entries)
  } catch {
    // Dev fallback: read the sources next to this file (src/mcp/bundled/).
    const here = path.dirname(fileURLToPath(import.meta.url))
    const entries = await Promise.all(
      BUNDLED_MCP_FILES.map(async (name) => [name, await fs.readFile(path.join(here, "bundled", name), "utf8")] as const),
    )
    return Object.fromEntries(entries)
  }
}

async function doEnsureBundledMcp(): Promise<void> {
  const dir = bundledDir()
  const version = `${InstallationVersion}:${BUNDLED_MCP_VERSION}`
  try {
    if ((await fs.readFile(path.join(dir, ".version"), "utf8")).trim() === version) {
      for (const name of BUNDLED_MCP_FILES) await fs.access(path.join(dir, name))
      return
    }
  } catch {
    // Missing or stale: (re)materialize below.
  }
  const contents = await loadBundledContents()
  await fs.mkdir(dir, { recursive: true })
  for (const [name, text] of Object.entries(contents)) {
    await fs.writeFile(path.join(dir, name), text, "utf8")
  }
  await fs.writeFile(path.join(dir, ".version"), version, "utf8")
}

let bundledPromise: Promise<void> | undefined
export function ensureBundledMcp(): Promise<void> {
  return (bundledPromise ??= doEnsureBundledMcp().catch((error) => {
    bundledPromise = undefined
    throw error
  }))
}

function getPath(root: unknown, segments: string[]): unknown {
  let current = root
  for (const segment of segments) {
    if (typeof current !== "object" || current === null || !(segment in current)) return undefined
    current = (current as Record<string, unknown>)[segment]
  }
  return current
}

/**
 * File-visible registration of the bundled defaults: adds missing keys only
 * to the given global config file (JSONC-aware). Best-effort, never throws.
 */
export async function ensureBundledMcpInConfigFile(file: string): Promise<void> {
  try {
    const defaults = bundledMcpDefaults()
    let text: string
    try {
      text = await fs.readFile(file, "utf8")
    } catch {
      await fs.mkdir(path.dirname(file), { recursive: true })
      await fs.writeFile(
        file,
        JSON.stringify({ $schema: "https://opencode.ai/config.json", mcp: defaults }, null, 2),
      )
      return
    }
    if (!text.trim()) text = "{}"
    let updated = text
    const ensureKey = (segments: string[], value: unknown) => {
      if (getPath(parse(updated), segments) !== undefined) return
      updated = applyEdits(
        updated,
        modify(updated, segments, value, { formattingOptions: { insertSpaces: true, tabSize: 2 } }),
      )
    }
    ensureKey(["mcp"], {})
    for (const [name, entry] of Object.entries(defaults)) {
      ensureKey(["mcp", name], {})
      for (const [key, value] of Object.entries(entry)) {
        ensureKey(["mcp", name, key], value)
      }
    }
    if (updated !== text) await fs.writeFile(file, updated, "utf8")
  } catch {
    // Best-effort: a broken config file must never block startup.
  }
}

function commandExists(command: string): Promise<boolean> {
  return new Promise((resolve) => {
    execFile(process.platform === "win32" ? "where" : "which", [command], (error) => resolve(!error))
  })
}

function runCommand(command: string, args: string[]): Promise<{ ok: boolean; output: string }> {
  return new Promise((resolve) => {
    execFile(command, args, { timeout: 600000 }, (error, stdout, stderr) => {
      resolve({ ok: !error, output: `${stdout ?? ""}${stderr ?? ""}`.slice(-1500) })
    })
  })
}

function pythonExists(): Promise<boolean> {
  return new Promise((resolve) => {
    execFile("python3", ["--version"], (error) => resolve(!error))
  })
}

function promptYesNo(question: string): Promise<string> {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin, output: process.stdout })
    rl.question(question, (answer) => {
      rl.close()
      resolve(answer)
    })
  })
}

async function installPython(): Promise<boolean> {
  const sudo = process.platform !== "win32" && typeof process.geteuid === "function" && process.geteuid() !== 0 && (await commandExists("sudo"))
    ? ["sudo"]
    : []
  const managers: Array<{ bin: string; args: string[] }> = [
    { bin: "apt-get", args: ["install", "-y", "python3"] },
    { bin: "dnf", args: ["install", "-y", "python3"] },
    { bin: "apk", args: ["add", "python3"] },
    { bin: "pacman", args: ["-Sy", "--noconfirm", "python"] },
    { bin: "brew", args: ["install", "python3"] },
  ]
  for (const manager of managers) {
    if (!(await commandExists(manager.bin))) continue
    const prefix = sudo // [] or ["sudo"]
    if (manager.bin === "apt-get") {
      await runCommand(
        prefix[0] ?? manager.bin,
        prefix.length ? [...prefix.slice(1), manager.bin, "update", "-qq"] : ["update", "-qq"],
      )
    }
    const result = await runCommand(
      prefix[0] ?? manager.bin,
      prefix.length ? [...prefix.slice(1), manager.bin, ...manager.args] : manager.args,
    )
    if (result.ok && (await pythonExists())) return true
  }
  return pythonExists()
}

async function doEnsurePythonAvailable(): Promise<void> {
  if (await pythonExists()) return
  const message =
    "Bundled MCP servers (memory, web) need python3, which was not found on PATH. Install it and restart, or answer yes to install now."
  if (!process.stdin.isTTY) {
    console.error(message + " (non-interactive shell, aborting)")
    process.exit(1)
  }
  const answer = await promptYesNo("python3 not found. Install it now? [y/N] ")
  if (!/^y(es)?$/i.test(answer.trim())) {
    console.error("python3 is required for the bundled memory/web MCP servers. Aborting.")
    process.exit(1)
  }
  if (!(await installPython())) {
    console.error("Automatic python3 install failed. Please install python3 manually and restart. Aborting.")
    process.exit(1)
  }
}

let pythonPromise: Promise<void> | undefined
export function ensurePythonAvailable(): Promise<void> {
  return (pythonPromise ??= doEnsurePythonAvailable().catch((error) => {
    pythonPromise = undefined
    throw error
  }))
}
