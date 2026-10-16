import { randomBytes } from "node:crypto"
import { closeSync, fstatSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs"
import { createEffect } from "solid-js"

const SCHEMA = "tower-opencode-tui-binding/v1"
const INTERVAL_MS = 500

function processStarttime() {
  const raw = readFileSync("/proc/self/stat", "utf8")
  const fields = raw.slice(raw.lastIndexOf(")") + 2).trim().split(/\s+/)
  return fields[19]
}

function sessionRoute(route) {
  if (route?.name === "session" && typeof route.params?.sessionID === "string") {
    return { routeName: "session", sessionID: route.params.sessionID }
  }
  return { routeName: route?.name === "home" ? "home" : "other", sessionID: null }
}

function publishBinding(api, state) {
  const route = sessionRoute(api.route.current)
  const key = `${route.routeName}\0${route.sessionID ?? ""}`
  if (key !== state.key) {
    state.key = key
    state.generation += 1
  }

  const record = JSON.stringify({
    schema: SCHEMA,
    pid: process.pid,
    process_starttime: state.processStarttime,
    route_name: route.routeName,
    session_id: route.sessionID,
    generation: state.generation,
    nonce: state.nonce,
    observed_at_ms: Date.now(),
  })
  const tempPath = `${state.path}.${randomBytes(8).toString("hex")}.tmp`
  let fd
  try {
    fd = openSync(tempPath, "wx", 0o600)
    writeFileSync(fd, record, { encoding: "utf8" })
    if ((fstatSync(fd).mode & 0o777) !== 0o600) throw new Error("unsafe binding permissions")
    closeSync(fd)
    fd = undefined
    renameSync(tempPath, state.path)
  } catch {
    if (fd !== undefined) {
      try {
        closeSync(fd)
      } catch {}
    }
    try {
      unlinkSync(tempPath)
    } catch {}
    try {
      unlinkSync(state.path)
    } catch {}
  }
}

export default {
  tui: async (api) => {
    if (process.platform !== "linux" || api.app.version !== "1.19.0") return

    let starttime
    try {
      starttime = processStarttime()
    } catch {
      return
    }
    if (!/^\d+$/.test(starttime ?? "")) return

    const state = {
      path: `/tmp/tower-opencode-session-${process.pid}.json`,
      processStarttime: starttime,
      generation: 0,
      key: undefined,
      nonce: randomBytes(16).toString("hex"),
    }
    const monitor = () => {
      createEffect(() => publishBinding(api, state))
      return null
    }

    const interval = setInterval(() => publishBinding(api, state), INTERVAL_MS)
    api.slots.register({
      slots: { app_bottom: monitor },
    })
    api.lifecycle.onDispose(() => {
      clearInterval(interval)
      try {
        const current = JSON.parse(readFileSync(state.path, "utf8"))
        if (current?.nonce === state.nonce) unlinkSync(state.path)
      } catch {}
    })
  },
}
