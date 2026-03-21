/**
 * OpenCode XMPP Bridge Plugin
 *
 * Integrace OpenCode s claude-xmpp-bridge:
 *  - při startu pluginu       → přejmenuje okno + zaregistruje aktivní session
 *  - session.created          → registrace nové top-level session (při /new)
 *  - session.deleted          → odhlásí session z bridge
 *  - session.idle             → push delivery: MCP inbox drain → TUI inject (appendPrompt + submitPrompt)
 *                               + XMPP notifikace + detekce question (🔴)
 *  - session.status (busy)    → model začal generovat (stav 🔵, agent se nemění)
 *  - message.updated          → detekce aktivního agenta (pole info.agent)
 *  - tool.execute.before      → report state=running do bridge (bez title update)
 *  - permission.asked         → informativní XMPP notifikace (co se chystá spustit); potvrzení přes TUI
 *  - permission.replied       → aktualizace titulu
 *  - server.instance.disposed → unregister + reset titulu
 *
 * Titulky oken — semafor: {agentKolečko}{stavKruh} projekt
 *
 *   Agent (levý symbol — barevné kolečko odpovídající barvě agenta v OpenCode TUI):
 *     ⚪ neznámý      — před první odpovědí nebo po /new
 *     🟢 coder        — coding agent (success = zelená)
 *     🔴 architect    — orchestrátor (error = červená)
 *     🟠 monitor      — monitoring (custom #ff6b35 = oranžová)
 *     🩵 home         — Home Assistant (custom #03a9f4 = světle modrá)
 *     🔵 google       — Google Workspace (custom #4285f4 = modrá)
 *     🟡 reviewer     — code review (warning = žlutá)
 *     ⚪ researcher   — research (secondary = šedá)
 *     🧠 cml          — Centrální Mozek Lidstva (orchestrátor)
 *
 *   Ikony jsou konfigurovatelné přes env proměnné BRIDGE_AGENT_<JMÉNO> (uppercase):
 *     export BRIDGE_AGENT_CODER=🟢
 *     export BRIDGE_AGENT_ARCHITECT=🔴
 *     export BRIDGE_AGENT_MONITOR=🟠
 *     export BRIDGE_AGENT_HOME=🩵
 *     export BRIDGE_AGENT_GOOGLE=🔵
 *     export BRIDGE_AGENT_REVIEWER=🟡
 *     export BRIDGE_AGENT_RESEARCHER=⚪
 *     export BRIDGE_AGENT_CML=🧠
 *
 *   Stav (pravý kruh — lifecycle agenta):
 *     🟢 idle        — čeká na vstup (dokončil úkol)
 *     🔵 running     — model generuje výstup nebo pokračuje po permission
 *     🔴 interaction — permission dialog nebo otázka agenta (čeká na odpověď)
 *
 *   Příklady: ⚪🟢 projekt  |  🟠🔵 projekt  |  🔵🔴 projekt
 *
 * Push-based message delivery (v0.9.0+):
 *   - Při session.idle: drain MCP inbox → inject via TUI (appendPrompt + submitPrompt)
 *   - TUI cesta = stejná jako uživatel — zpráva se zobrazí v konverzaci
 *   - Fallback polling (5s) pro idle agenty na prázdném promptu (CR nudge je no-op)
 *   - Žádný messageBuffer — všechny zprávy se concatenují do jednoho promptu
 *   - MCP session ID se cachuje; po výpadku bridge se cache invaliduje.
 *
 * Zapínání/vypínání:
 *   touch ~/.config/xmpp-notify/notify-enabled   # XMPP notifikace při session.idle
 *   touch ~/.config/xmpp-notify/ask-enabled      # XMPP notifikace při permission.asked
 *
 * Vyžaduje: claude-xmpp-bridge démon + claude-xmpp-client v $PATH
 */

export const XmppBridgePlugin = async (input) => {
  const { client, directory, $ } = input
   const PLUGIN_VERSION = "0.9.14"
  const pluginRef = (() => {
    try {
      // eslint-disable-next-line no-undef
      const fs = require("fs")
      // eslint-disable-next-line no-undef
      const crypto = require("crypto")
      const selfPath = import.meta.path ?? new URL(import.meta.url).pathname
      const hash = crypto.createHash("sha1").update(fs.readFileSync(selfPath)).digest("hex").slice(0, 7)
      return `${PLUGIN_VERSION}+${hash}`
    } catch (_) {
      return PLUGIN_VERSION
    }
  })()

  // ---------------------------------------------------------------------------
  // Zjistit absolutní cestu k claude-xmpp-client jednou při startu.
  // V bwrap sandboxu může být $PATH ořezaná a `which` vrátí chybu.
  // Pokud binárka není dostupná, všechna bridge volání se tiše přeskočí —
  // zabrání se tím výpisům "bun: command not found" do terminálu.
  // ---------------------------------------------------------------------------
  const resolveClientBin = () => {
    try {
      // eslint-disable-next-line no-undef
      const { execFileSync } = require("child_process")
      const path = execFileSync("which", ["claude-xmpp-client"], { encoding: "utf8" }).trim()
      return path || null
    } catch (_) {
      return null
    }
  }
  const CLIENT_BIN = resolveClientBin()
  const helperExists = (path) => {
    try {
      // eslint-disable-next-line no-undef
      const fs = require("fs")
      fs.accessSync(path, fs.constants.X_OK)
      return true
    } catch (_) {
      return false
    }
  }
  const BRIDGE_MODE = process.env.XMPP_BRIDGE_MODE ?? "auto"
  const DISABLE_WHEN_MISSING = process.env.XMPP_BRIDGE_DISABLE_WHEN_MISSING === "1"
  let bridgeDisabled = BRIDGE_MODE === "title-only"

  // Read MCP auth token from env or token file (same as socket_token).
  const MCP_AUTH_TOKEN = (() => {
    const envToken = process.env.CLAUDE_XMPP_SOCKET_TOKEN
    if (envToken) return envToken
    try {
      // eslint-disable-next-line no-undef
      const fs = require("fs")
      const tokenPath = `${process.env.HOME}/.config/claude-xmpp-bridge/socket_token`
      return fs.readFileSync(tokenPath, "utf8").trim() || null
    } catch (_) {
      return null
    }
  })()

  // Wrapper: spustí claude-xmpp-client pouze pokud je dostupný.
  // Tiše vrátí { exitCode: 127 } pokud není — žádný výpis do terminálu.
  const runClient = async (...args) => {
    if (!CLIENT_BIN) return { exitCode: 127, stdout: "", stderr: "" }
    try {
      const proc = Bun.spawn([CLIENT_BIN, ...args.map(String)], {
        stdout: "pipe", stderr: "pipe",
      })
      const [exitCode, stdout, stderr] = await Promise.all([
        proc.exited,
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
      ])
      return { exitCode, stdout, stderr }
    } catch (err) {
      return { exitCode: 1, stdout: "", stderr: String(err) }
    }
  }

  const runBridgeClient = async (...args) => {
    if (bridgeDisabled) return { exitCode: 126, stdout: "", stderr: "bridge disabled" }
    if (bridgeSuppressed()) return { exitCode: 125, stdout: "", stderr: "bridge suppressed" }
    const res = await runClient(...args)
    if (isBridgeUnavailableError(res)) {
      await markBridgeUnavailable(args[0])
      if (DISABLE_WHEN_MISSING) {
        bridgeDisabled = true
        stopActiveBridgeTimers()
        if (recoveryTimer) { clearInterval(recoveryTimer); recoveryTimer = null }
        await warn("bridge missing at startup/runtime; switching plugin to title-only mode", "bridge-disabled")
      }
    } else if (res.exitCode === 0) {
      clearBridgeUnavailable()
    }
    return res
  }

  const runQuietCommand = async (argv) => {
    try {
      const proc = Bun.spawn(argv.map(String), {
        stdout: "pipe", stderr: "pipe",
      })
      const [exitCode, stdout, stderr] = await Promise.all([
        proc.exited,
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
      ])
      return { exitCode, stdout, stderr }
    } catch (err) {
      return { exitCode: 1, stdout: "", stderr: String(err) }
    }
  }

  const AGENT_NOTIFY_BIN = `${process.env.HOME}/claude-home/agent-notify.sh`
  const AGENT_NOTIFY_AVAILABLE = helperExists(AGENT_NOTIFY_BIN)

  const runAgentNotify = async (...args) => {
    if (!AGENT_NOTIFY_AVAILABLE) return { exitCode: 127, stdout: "", stderr: "agent-notify unavailable" }
    const res = await runQuietCommand([AGENT_NOTIFY_BIN, ...args])
    if (res.exitCode !== 0) {
      await warn(
        `agent-notify exit=${res.exitCode}${res.stderr ? " stderr=" + res.stderr.slice(0, 200) : ""}`,
        `agent-notify:${args[0] ?? "run"}`
      )
    }
    return res
  }

  const STY     = process.env.STY    ?? ""
  const BACKEND = STY
    ? "screen"
    : process.env.TMUX
      ? "tmux"
      : "none"

  // $WINDOW: čteme z env rodičovského procesu (/proc/ppid/environ).
  // Důvod: OpenCode je spuštěno jako podproces bash shellu screen okna.
  // Screen nastaví $WINDOW v shellu, ale OpenCode může mít přepsané env
  // (např. zděděné z jiného kontextu). Rodičovský bash má vždy správnou hodnotu.
  const readWindowFromPpid = () => {
    try {
      // Node.js synchronní čtení — voláme při inicializaci, async není nutné.
      // eslint-disable-next-line no-undef
      const fs = require("fs")
      const raw = fs.readFileSync(`/proc/${process.ppid}/environ`, "latin1")
      // environ je null-byte oddělený seznam KEY=VALUE\0
      const match = raw.split("\0").find(e => e.startsWith("WINDOW="))
      return match ? match.slice(7) : null
    } catch (_) {
      return null
    }
  }
  const WINDOW = readWindowFromPpid() ?? process.env.WINDOW ?? "0"

  // Opravit $WINDOW v env celého procesu. process.env.WINDOW mohl být zděděn
  // špatně (viz readWindowFromPpid výše). Nastavením správné hodnoty zajistíme,
  // že bash tools spuštěné modelem (subprocesy OpenCode) vidí správný WINDOW.
  // Zároveň nastavíme BRIDGE_WINDOW pro explicitní přístup.
  process.env.WINDOW = WINDOW
  process.env.BRIDGE_WINDOW = WINDOW

  const projectName = directory.split("/").pop() || directory

  // ---------------------------------------------------------------------------
  // bridgeID(): přidá "_wWINDOW" suffix pro screen backend.
  // Důvod: OpenCode sessions jsou sdílené přes instance — dvě okna ve stejném
  // projektu vidí stejné session ID. Suffix zaručuje unikátnost per screen okno.
  // Příklad: "ses_abc123" → "ses_abc123_w4" (v okně 4 screen session)
  // WINDOW je čteno z /proc/ppid/environ (viz výše) — zaručeně správná hodnota.
  // ---------------------------------------------------------------------------
  const bridgeID = (opencodeID) =>
    (STY && opencodeID) ? `${opencodeID}_w${WINDOW}` : (opencodeID ?? "")

  // Sledovaná session ID — nastavena při registraci, použita při ukončení.
  // Ukládáme bridge ID (s _wWINDOW suffixem), ne raw OpenCode ID.
  let registeredSessionID = null

  // Mapa opencode_id → bridge_id pro session.deleted handler
  const ocToBridge = new Map()

  // ---------------------------------------------------------------------------
  // Idle polling state
  // ---------------------------------------------------------------------------
  // Fallback polling: session.idle is the primary trigger for inbox drain, but
  // when an agent is already idle (waiting for user input), a new nudge CR does
  // NOT trigger another session.idle event. This fallback timer ensures messages
  // are picked up even when the agent is parked on an empty prompt.
  const IDLE_POLL_INTERVAL_MS = parseInt(process.env.XMPP_BRIDGE_IDLE_POLL_MS ?? "5000")
  // Periodický re-register: pokud bridge session nezná (restart bridge), re-zaregistruje.
  // Interval 90s — nezávislý na session.idle, zajistí obnovu i pokud agent je long-running.
  // Přepis přes env: XMPP_BRIDGE_REREG_INTERVAL_MS (pro testy nastavit na nízkou hodnotu).
  const REREG_INTERVAL_MS = parseInt(process.env.XMPP_BRIDGE_REREG_INTERVAL_MS ?? "90000")
  const BRIDGE_RECOVERY_POLL_MS = parseInt(process.env.XMPP_BRIDGE_RECOVERY_POLL_MS ?? "300000")
  const BRIDGE_RETRY_MS = parseInt(process.env.XMPP_BRIDGE_RETRY_MS ?? "60000")
  let isIdle = false
  let pollTimer = null
  let reregTimer = null
  let recoveryTimer = null
  let polling = false  // guard against concurrent pollInbox calls
  let bridgeUnavailableUntil = 0
  let cachedMcpSessionId = null
  let desiredBridgeSessionID = null
  let desiredProjectDir = directory

   // Agent ikony — barevné kolečko odpovídající barvě agenta v OpenCode TUI.
   //
   // Výchozí mapování (agent name → emoji):
   //   coder      → 🟢  (success    = zelená)
   //   architect  → 🔴  (error      = červená)
   //   monitor    → 🟠  (#ff6b35    = oranžová)
   //   home       → 🩵  (#03a9f4    = světle modrá)
   //   google     → 🔵  (#4285f4    = modrá)
    //   reviewer   → 🟡  (warning    = žlutá)
    //   researcher → ⚪  (secondary  = šedá)
    //   cml        → 🧠  (brain      = orchestrátor)
    //
    // Přizpůsobení přes env proměnné BRIDGE_AGENT_<JMÉNO> (uppercase):
    //   export BRIDGE_AGENT_CODER=🟢
    //   export BRIDGE_AGENT_ARCHITECT=🔴
    //   export BRIDGE_AGENT_MONITOR=🟠
    //   export BRIDGE_AGENT_HOME=🩵
    //   export BRIDGE_AGENT_GOOGLE=🔵
    //   export BRIDGE_AGENT_REVIEWER=🟡
    //   export BRIDGE_AGENT_RESEARCHER=⚪
    //   export BRIDGE_AGENT_CML=🧠
   // ---------------------------------------------------------------------------
   const DEFAULT_AGENT_ICONS = {
     coder:      "🟢",
     architect:  "🔴",
     monitor:    "🟠",
     home:       "🩵",
     google:     "🔵",
      reviewer:   "🟡",
      researcher: "⚪",
      cml:        "🧠",
    }

  // Vrátí ikonu pro daného agenta — nejdřív env, pak default, pak ⚪.
  const agentIcon = (name) => {
    if (!name) return "⚪"
    const envKey = "BRIDGE_AGENT_" + name.toUpperCase()
    return process.env[envKey] ?? DEFAULT_AGENT_ICONS[name] ?? "⚪"
  }

  // Aktuální agent — null = neznámý (před první odpovědí nebo po /new).
  // Nastavuje se z message.updated (pole info.agent).
  let currentAgent = null

  // Sestaví emoji titulek okna z ikony agenta + stavového kruhu.
  // Volitelný parametr name přepíše projectName (použití při session.created).
  const buildTitle = (stateCircle, name) =>
    `${agentIcon(currentAgent)}${stateCircle} ${name ?? projectName}`

  // Sestaví ASCII fallback titulek (bez emoji) — jen stavový prefix + název.
  // Agent se v ASCII nevyjadřuje (je to záložní cesta pro bwrap sandbox).
  const buildAscii = (statePrefix, name) =>
    `${statePrefix} ${name ?? projectName}`

  // ---------------------------------------------------------------------------
  // pollInbox(): drain MCP inbox a doručí VŠECHNY zprávy přes prompt_async.
  // Volá se při session.idle — žádný polling timer.
  // Všechny zprávy se concatenují do jednoho promptu (žádný messageBuffer).
  // Guard: `polling` flag zabrání concurrent spuštění.
  // ---------------------------------------------------------------------------
  const LOG_THROTTLE_MS = parseInt(process.env.XMPP_BRIDGE_LOG_THROTTLE_MS ?? "30000")
  const lastLogAt = new Map()

  const logPlugin = (level, msg, key = "") => {
    if (key) {
      const now = Date.now()
      const prev = lastLogAt.get(key) ?? 0
      if ((now - prev) < LOG_THROTTLE_MS) return Promise.resolve()
      lastLogAt.set(key, now)
    }
    return client.app.log({ body: { service: "xmpp-bridge", level, message: msg } }).catch(() => {})
  }

  const dbg = (msg, key = "") => logPlugin("info", msg, key)
  const warn = (msg, key = "") => logPlugin("warn", msg, key)
  const errlog = (msg, key = "") => logPlugin("error", msg, key)
  const logCaught = (scope, err, key = "") => errlog(`${scope}: ${err}`, key || `caught:${scope}`)

  // ---------------------------------------------------------------------------
  // injectMessage(): doručí zprávu do OpenCode TUI přes SDK client.
  // Používá TUI appendPrompt + submitPrompt — stejná cesta jako uživatel.
  // Interní fetch přes Hono router, funguje bez HTTP serveru.
  // TUI zůstává v syncu — zpráva se zobrazí v konverzaci.
  // ---------------------------------------------------------------------------

  // Resolve the top-level OpenCode session ID (without _wN suffix).
  // promptAsync needs the raw OpenCode session ID, not the bridge ID.
  let opencodeSessionID = null

   const injectMessage = async (sessionID, text) => {
    if (!text) return { ok: false, error: "empty text" }
    try {
      // TUI injection: appendPrompt inserts text into the TUI textarea,
      // then submitPrompt triggers the submit action (reads textarea + sends to LLM).
      // This is the same path as user typing + pressing Enter — TUI stays in sync.
      // SDK v1 format: { body: { ... } } for both calls.
      if (typeof client.tui?.appendPrompt !== "function") {
        await warn("TUI appendPrompt not available on SDK client", "inject-no-method")
        return { ok: false, error: "no TUI methods" }
      }
      await dbg(`TUI inject → ${text.length} chars`)
      const appendRes = await client.tui.appendPrompt({ body: { text } })
      if (appendRes.error) {
        await warn(`TUI appendPrompt error: ${JSON.stringify(appendRes.error).slice(0, 200)}`, "inject-tui-error")
        return { ok: false, error: "appendPrompt failed" }
      }
      // Small delay to let TUI render the appended text before submitting.
      // PromptAppend handler uses setTimeout(..., 0) internally for layout.
      await new Promise((r) => setTimeout(r, 50))
      const submitRes = await client.tui.submitPrompt()
      if (submitRes.error) {
        await warn(`TUI submitPrompt error: ${JSON.stringify(submitRes.error).slice(0, 200)}`, "inject-tui-error")
        return { ok: false, error: "submitPrompt failed" }
      }
      await dbg(`TUI injected ${text.length} chars`)
      return { ok: true }
    } catch (err) {
      await errlog(`inject error: ${err}`, "inject-error")
      return { ok: false, error: String(err) }
    }
  }

  const bridgeErrorText = (res) => `${res?.stdout ?? ""}\n${res?.stderr ?? ""}`

  const isBridgeUnavailableError = (res) => {
    const text = bridgeErrorText(res)
    return text.includes("bridge not running") || text.includes("Another bridge is already running")
  }

  const isSessionNotFoundError = (res) => bridgeErrorText(res).includes("session not found")

  const bridgeSuppressed = () => bridgeUnavailableUntil > Date.now()

  const markBridgeUnavailable = async (reason) => {
    bridgeUnavailableUntil = Date.now() + BRIDGE_RETRY_MS
    cachedMcpSessionId = null
    await warn(
      `bridge unavailable (${reason}); suppressing bridge calls for ${BRIDGE_RETRY_MS} ms`,
      `bridge-unavailable:${reason}`
    )
  }

  const clearBridgeUnavailable = () => {
    bridgeUnavailableUntil = 0
  }

  const fireAndForget = (promise, label) => {
    promise.catch(err => { errlog(`${label}: ${err}`, `fire-and-forget:${label}`) })
  }

  const stopActiveBridgeTimers = () => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    if (reregTimer) { clearInterval(reregTimer); reregTimer = null }
  }

  const ensureRecoveryTimer = () => {
    if (recoveryTimer) return
    recoveryTimer = setInterval(async () => {
      if (!desiredBridgeSessionID || registeredSessionID || bridgeSuppressed()) return
      await dbg("bridge recovery tick for " + desiredBridgeSessionID, "bridge-recovery-tick")
        const regRes = await runBridgeClient("register", makeRegPayload(desiredBridgeSessionID, desiredProjectDir))
        if (regRes.exitCode === 0) {
          registeredSessionID = desiredBridgeSessionID
          process.env.BRIDGE_SESSION_ID = desiredBridgeSessionID
          if (AGENT_NOTIFY_AVAILABLE) {
            await runAgentNotify("start", desiredBridgeSessionID, desiredProjectDir)
          }
        await reportState(isIdle ? "idle" : "running")
        if (!pollTimer) {
          pollTimer = setInterval(async () => {
            if (isIdle) await pollInbox()
          }, IDLE_POLL_INTERVAL_MS)
        }
        if (!reregTimer) {
          reregTimer = setInterval(async () => {
            if (!registeredSessionID) return
            const failed = await reportState(isIdle ? "idle" : "running")
            if (failed) {
              await reregisterIfNeeded(true)
            }
          }, REREG_INTERVAL_MS)
        }
        clearInterval(recoveryTimer)
        recoveryTimer = null
      }
    }, BRIDGE_RECOVERY_POLL_MS)
  }

  const pollInbox = async () => {
    if (bridgeDisabled || !registeredSessionID || polling || bridgeSuppressed()) return
    polling = true
    try {
      let mcpSessionId = cachedMcpSessionId
      if (!mcpSessionId) {
        // Step 1: initialize — get mcp-session-id
        const initHeaders = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" }
        if (MCP_AUTH_TOKEN) initHeaders["Authorization"] = `Bearer ${MCP_AUTH_TOKEN}`
        const initRes = await fetch("http://127.0.0.1:7878/mcp", {
          method:  "POST",
          headers: initHeaders,
          body: JSON.stringify({
            jsonrpc: "2.0", id: 1, method: "initialize",
            params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "opencode-plugin", version: "1.0" } },
          }),
        }).catch(async (e) => {
          await markBridgeUnavailable("mcp-init")
          await errlog("MCP init fetch error: " + e, "mcp-init-error")
          return null
        })

        mcpSessionId = initRes?.headers?.get("mcp-session-id")
        if (!mcpSessionId) {
          await markBridgeUnavailable("mcp-init-no-session")
          return
        }
        cachedMcpSessionId = mcpSessionId
      }

      // Step 2: tools/call — drain inbox
      const toolHeaders = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": mcpSessionId,
      }
      if (MCP_AUTH_TOKEN) toolHeaders["Authorization"] = `Bearer ${MCP_AUTH_TOKEN}`
      const mcpRes = await fetch("http://127.0.0.1:7878/mcp", {
        method:  "POST",
        headers: toolHeaders,
        body: JSON.stringify({
          jsonrpc: "2.0",
          id:      2,
          method:  "tools/call",
          params:  {
            name:      "receive_messages",
            arguments: { session_id: registeredSessionID },
          },
        }),
      }).catch(async (e) => {
        cachedMcpSessionId = null
        await markBridgeUnavailable("mcp-tools-call")
        await errlog("MCP tools/call fetch error: " + e, "mcp-tools-call-error")
        return null
      })

      if (!mcpRes) return
      if (!mcpRes.ok) {
        cachedMcpSessionId = null
        await markBridgeUnavailable("mcp-tools-call-status")
        await errlog("MCP tools/call HTTP status: " + mcpRes.status, "mcp-tools-call-status")
        return
      }

      const text = await mcpRes.text().catch(() => null)
      // SSE spec: multiple `data:` lines in a single event are joined with newlines.
      // Take the last complete event's data lines to handle multi-line SSE responses.
      const dataLines = text?.split('\n').filter(l => l.startsWith('data:')) ?? []
      const dataPayload = dataLines.map(l => l.slice(5).trim()).join('\n')
      const body = dataPayload ? JSON.parse(dataPayload) : null
      // receive_messages returns each message as a separate content item (type=text).
      // Each item.text is a JSON-encoded dict with keys: text, from_session,
      // source_type, message_type, message_id, ts, type.
      const contentItems = body?.result?.content
      if (!Array.isArray(contentItems) || contentItems.length === 0) return

      // Parse all messages and concatenate into a single prompt.
      const parseItem = (item) => {
        if (!item?.text) return null
        try {
          const parsed = JSON.parse(item.text)
          return parsed?.text || null
        } catch {
          return item.text
        }
      }
      const messages = contentItems.map(parseItem).filter(Boolean)
      if (messages.length === 0) return

      // Inject ALL messages at once via TUI appendPrompt + submitPrompt.
      // All messages concatenated into one prompt — no buffering, no per-cycle limit.
      const combined = messages.join("\n\n---\n\n")
      await dbg(`pollInbox: ${messages.length} message(s), ${combined.length} chars — injecting via TUI`)
      const injectRes = await injectMessage(registeredSessionID, combined)
      if (!injectRes.ok) {
        await warn(`pollInbox inject failed: ${injectRes.error}`, "inject-failed")
      }
    } catch (err) {
      await markBridgeUnavailable("mcp-poll")
      await errlog("MCP poll error: " + err, "mcp-poll-error")
    } finally {
      polling = false
    }
  }

  // ---------------------------------------------------------------------------
  // Detekce bwrap sandboxu — provedena JEDNOU při startu, bez volání screen.
  //
  // bwrap --new-session volá setsid() → proces nemá kontrolující terminál →
  // Screen socket soubor není přístupný (nebo neexistuje v sandboxovaném fs).
  //
  // Detekce: zkontrolujeme zda socket soubor pro $STY existuje na filesystému.
  // Screen ukládá sockety do $SCREENDIR (výchozí: ~/.screen nebo /run/screen/S-USER).
  // Pokud soubor neexistuje → jsme v sandboxu (bind-mount skryl ~/.screen).
  //
  // Proč filesystem místo volání `screen -Q ...`:
  //   screen -Q vypisuje výstup/chyby na stdout/stderr → viditelné v OpenCode TUI.
  //   Čtení filesystému je tiché, synchronní a bez vedlejších efektů.
  //
  // Stdout fallback (ESC k ... ESC \) se smí použít POUZE v sandboxu:
  //   - Mimo sandbox: Screen zachytí sekvenci z pty a překreslí caption/hardstatus
  //     ve špatný moment → zdvojené okno listy, blikání, artefakty.
  //   - V sandboxu: Screen socket není dostupný, stdout je jediná cesta.
  // ---------------------------------------------------------------------------
  const detectSandbox = () => {
    if (!STY) return false
    try {
      // eslint-disable-next-line no-undef
      const fs = require("fs")
      // Screen socket je soubor pojmenovaný podle STY (např. "6385.pts-0.black-arch").
      // Hledáme v $SCREENDIR, pak ve standardních umístěních.
      const screenDir = process.env.SCREENDIR
        || `${process.env.HOME}/.screen`
      const socketPath = `${screenDir}/${STY}`
      return !fs.existsSync(socketPath)
    } catch (_) {
      // Pokud fs selže (neočekávaně), předpokládáme sandbox pro bezpečnost.
      return true
    }
  }
  const inSandbox = detectSandbox()

  // ---------------------------------------------------------------------------
  // Title scheduler.
  //
  // OpenCode/Ink překresluje TUI často. Přímé `screen -X title` z každé události
  // (zejména tool.execute.before) koliduje s překreslováním Screen caption/
  // hardstatus a vede k artefaktům. Proto title aktualizujeme přes debounce:
  //   1. událost jen zapíše desiredTitle
  //   2. skutečný update proběhne později, jednou
  //
  // Startup je zvlášť citlivý: první update je deferred přes setImmediate(), aby
  // neproběhl během inicializace OpenCode TUI.
  // ---------------------------------------------------------------------------
  const TITLE_DEBOUNCE_MS = parseInt(process.env.XMPP_BRIDGE_TITLE_DEBOUNCE_MS ?? "750")
  const HSTATUS_SCRUB_DELAY_MS = parseInt(process.env.XMPP_BRIDGE_HSTATUS_SCRUB_DELAY_MS ?? "250")
  const HSTATUS_SCRUB_PASSES = parseInt(process.env.XMPP_BRIDGE_HSTATUS_SCRUB_PASSES ?? "3")
  let lastTitle = ""
  let desiredTitle = null
  let titleTimer = null
  let hstatusPulseTimers = []

  const applyTitleNow = async (emojiTitle, asciiTitle) => {
    if (STY && !inSandbox) {
      if (emojiTitle === lastTitle) return
      lastTitle = emojiTitle
      await clearScreenHstatus()
      await $`screen -S ${STY} -p ${WINDOW} -X title ${emojiTitle}`.nothrow()
      pulseScreenHstatusCleanup()
      return
    }
    if (STY && inSandbox) {
      if (asciiTitle === lastTitle) return
      lastTitle = asciiTitle
      process.stdout.write('\x1bk' + asciiTitle + '\x1b\\')
      return
    }
    if (emojiTitle === lastTitle) return
    lastTitle = emojiTitle
    process.stdout.write('\x1b]0;' + emojiTitle + '\x07')
  }

  const clearTitleTimer = () => {
    if (titleTimer) {
      clearTimeout(titleTimer)
      titleTimer = null
    }
  }

  const flushScheduledTitle = async () => {
    clearTitleTimer()
    if (!desiredTitle) return
    const next = desiredTitle
    desiredTitle = null
    await applyTitleNow(next.emojiTitle, next.asciiTitle)
  }

  const scheduleTitle = (emojiTitle, asciiTitle, { immediate = false } = {}) => {
    const target = { emojiTitle, asciiTitle }
    const current = STY && inSandbox ? asciiTitle : emojiTitle
    if (current === lastTitle) {
      desiredTitle = null
      clearTitleTimer()
      return
    }
    desiredTitle = target
    clearTitleTimer()
    if (immediate) {
      setImmediate(() => {
        flushScheduledTitle().catch(err => { dbg("flushScheduledTitle error: " + err) })
      })
      return
    }
    titleTimer = setTimeout(() => {
      flushScheduledTitle().catch(err => { dbg("flushScheduledTitle error: " + err) })
    }, TITLE_DEBOUNCE_MS)
  }

  const clearScreenHstatus = async () => {
    if (!STY || inSandbox) return
    await $`screen -S ${STY} -p ${WINDOW} -X hstatus ${" "}`.nothrow()
  }

  const clearHstatusPulseTimers = () => {
    for (const timer of hstatusPulseTimers) clearTimeout(timer)
    hstatusPulseTimers = []
  }

  const pulseScreenHstatusCleanup = () => {
    if (!STY || inSandbox) return
    clearHstatusPulseTimers()
    for (let i = 0; i < HSTATUS_SCRUB_PASSES; i += 1) {
      const timer = setTimeout(() => {
        clearScreenHstatus().catch(err => { dbg("clearScreenHstatus error: " + err) })
      }, i * HSTATUS_SCRUB_DELAY_MS)
      hstatusPulseTimers.push(timer)
    }
  }

  // ---------------------------------------------------------------------------
  // Hlásí stav do bridge s ikonou aktuálního agenta.
  // Pole "mode" obsahuje emoji kolečko agenta (nebo "⚪" pokud neznámý) —
  // bridge ho uloží a zobrazí v /list výstupu před stavovým kruhem.
  // Vrací true pokud bridge session nezná (detekce dle stderr nebo exit code).
  // ---------------------------------------------------------------------------
  const reportState = async (state) => {
    if (!registeredSessionID) return true
    if (bridgeDisabled) return true
    if (bridgeSuppressed()) return true
    const payload = JSON.stringify({ session_id: registeredSessionID, state, mode: agentIcon(currentAgent) })
    const res = await runBridgeClient("state", payload)
    // Detekce selhání: stderr obsahuje "Error:" (robustní — nezávisí na exit code)
    // nebo exit code nenulový (fallback)
    const failed = (res.stderr && res.stderr.includes("Error:")) || (res.exitCode !== null && res.exitCode !== 0)
    if (failed && res.exitCode !== 125 && !isSessionNotFoundError(res) && !isBridgeUnavailableError(res)) {
      await warn(
        "reportState(" + state + ") exit=" + res.exitCode + " failed=" + failed + (res.stderr ? " stderr=" + res.stderr.slice(0, 100) : ""),
        `report-state-failed:${state}`
      )
    }
    return failed
  }

  // ---------------------------------------------------------------------------
  // Sestaví registrační payload pro aktuální session.
  // ---------------------------------------------------------------------------
  const makeRegPayload = (sessionID, projectDir) => JSON.stringify({
    session_id:     sessionID,
    sty:            STY,
    window:         WINDOW,
    project:        projectDir ?? directory,
    backend:        BACKEND,
    source:         "opencode",
    plugin_version: pluginRef,
  })

  // ---------------------------------------------------------------------------
  // Re-registrace — volá se při session.idle pokud bridge session nezná.
  // Stane se po restartu bridge: session v DB zmizí, ale plugin běží dál.
  // Register je idempotentní — bridge zachová agent_state/agent_mode.
  // ---------------------------------------------------------------------------
  const reregisterIfNeeded = async (failed) => {
    if (bridgeDisabled) return
    if (!failed || !desiredBridgeSessionID || bridgeSuppressed()) return
    const regRes = await runBridgeClient("register", makeRegPayload(desiredBridgeSessionID, desiredProjectDir))
    if (regRes.exitCode === 0) {
      registeredSessionID = desiredBridgeSessionID
      process.env.BRIDGE_SESSION_ID = desiredBridgeSessionID
    } else if (regRes.exitCode !== 125 && !isBridgeUnavailableError(regRes)) {
      await warn(
        "register result: exit=" + regRes.exitCode + (regRes.stderr ? " stderr=" + regRes.stderr.slice(0, 100) : ""),
        "register-failed"
      )
    }
  }

  // ---------------------------------------------------------------------------
  // 1. Startup title setup je deferred, aby neproběhl během inicializace TUI.
  //    tool.execute.before title update záměrně NEDĚLÁME — je příliš častý a
  //    triggeruje Screen redraw uprostřed render stormu. Titulek se mění jen na
  //    hrubých stavových přechodech (startup, session.created, session.status,
  //    session.idle, permission.*). Kritické vizuální stavy (busy, permission)
  //    se plánují immediate, ostatní procházejí debounce schedulerem.
  // ---------------------------------------------------------------------------
  setImmediate(async () => {
    try {
      if (STY && !inSandbox) {
        await $`screen -S ${STY} -p ${WINDOW} -X dynamictitle off`.nothrow()
        await clearScreenHstatus()
      }
      pulseScreenHstatusCleanup()
      scheduleTitle(buildTitle("🟢"), buildAscii("AI.", projectName), { immediate: true })
      // Log SDK capabilities for debugging push delivery
      const sessionMethods = Object.keys(client.session ?? {}).filter(k => typeof client.session[k] === "function")
      const tuiMethods = Object.keys(client.tui ?? {}).filter(k => typeof client.tui[k] === "function")
      await dbg(`SDK session methods: ${sessionMethods.join(",")}`)
      await dbg(`SDK tui methods: ${tuiMethods.join(",")}`)
    } catch (err) {
      await logCaught("startup-title", err, "startup-title-error")
    }
  })

  // ---------------------------------------------------------------------------
  // 2. Registrace aktivní session do bridge — ODLOŽENA přes setImmediate()
  //    Důvod: client.session.list() volá HTTP na server, který v tento moment
  //    teprve načítá pluginy → synchronní volání způsobí deadlock a zamrznutí.
  //    setImmediate() naplánuje kód na příští iteraci event loop, kdy je server
  //    již plně připraven a schopen odpovídat.
  // ---------------------------------------------------------------------------
  setImmediate(async () => {
    try {
      if (bridgeDisabled) return
      const sessionsRes = await client.session.list()
      if (!sessionsRes.data || sessionsRes.data.length === 0) return

      // Filtrovat: jen top-level session (bez parentID) v tomto adresáři.
      // Každý OpenCode proces má svůj directory — filtrujeme přesně ten náš,
      // aby dvě instance ve stejném projektu nezaregistrovaly totéž session_id.
      const topLevel = sessionsRes.data.filter(
        s => !s.parentID && s.directory === directory
      )
      // Z kandidátů vzít nejnovější (dle time.updated)
      const active = topLevel.sort(
        (a, b) => b.time.updated - a.time.updated
      )[0]
      if (!active) return

      // ---------------------------------------------------------------------------
      // Zjistit zda bridge již zná session pro toto sty+window.
      // Pokud ano, přijmeme tu identitu místo abychom zaregistrovali duplicitu.
      // Případ: dvě OpenCode instance ve stejném projektu sdílejí opencodeID —
      // každá musí skončit pod svým _wN suffixem. Bridge je pravda o tom, který
      // window má jaké session_id.
      // ---------------------------------------------------------------------------
      let bid = bridgeID(active.id)
      if (STY && !bridgeSuppressed()) {
        const listRes = await runBridgeClient("list")
        if (listRes.exitCode === 0 && listRes.stdout) {
          try {
            const sessions = JSON.parse(listRes.stdout)
            const existing = sessions.find(
              s => s.sty === STY && s.window === WINDOW
            )
            if (existing) {
              // Bridge už zná session pro naše sty+window — přijmeme tu identitu.
              // Tím se vyhneme přepsání správně registrované session jiným agentem.
              bid = existing.session_id
              await dbg(`reusing existing bridge session for w${WINDOW}: ${bid}`)
            }
          } catch (_) {
            // JSON parse selhal — pokračovat se standardní registrací
          }
        }
      }

      desiredBridgeSessionID = bid
      desiredProjectDir = active.directory
      opencodeSessionID = active.id  // raw OpenCode ID for prompt_async
      ocToBridge.set(active.id, bid)
      const regRes = await runBridgeClient("register", makeRegPayload(bid, active.directory))
      if (regRes.exitCode === 0) {
        registeredSessionID = bid
        // Export identity do env — bash tools agenta vidí $BRIDGE_SESSION_ID
        process.env.BRIDGE_SESSION_ID = bid
      } else {
        registeredSessionID = null
        process.env.BRIDGE_SESSION_ID = ""
        stopActiveBridgeTimers()
        if (!bridgeDisabled) ensureRecoveryTimer()
      }
      if (registeredSessionID && AGENT_NOTIFY_AVAILABLE) {
        await runAgentNotify("start", bid, active.directory)
      }
      // Report initial state (agent is idle at startup, mode = planning)
      if (registeredSessionID) await reportState("idle")

      isIdle = true  // agent je při startu idle (čeká na vstup)

      // Fallback polling timer: picks up messages when agent is parked on empty prompt
      // (session.idle won't re-fire, CR nudge is no-op in OpenCode TUI).
      if (!pollTimer) {
        pollTimer = setInterval(async () => {
          if (isIdle) await pollInbox()
        }, IDLE_POLL_INTERVAL_MS)
      }

      // Spustit periodický re-register timer — obnoví registraci po restartu bridge.
      // Nezávislý na session.idle, zajistí obnovu i pokud agent čeká dlouho na vstup.
      if (!reregTimer) {
        reregTimer = setInterval(async () => {
          if (!registeredSessionID) return
          const failed = await reportState(isIdle ? "idle" : "running")
          if (failed) {
            await reregisterIfNeeded(true)
          }
        }, REREG_INTERVAL_MS)
      }
    } catch (err) {
      await logCaught("startup-register", err, "startup-register-error")
    }
  })

  return {
    // -------------------------------------------------------------------------
    // tool.execute.before: úmyslně BEZ title update.
    // Dříve jsme zde přepínali title na 🔵 při každém tool callu, ale to vedlo
    // k častým `screen -X title` redrawům uprostřed OpenCode TUI renderu.
    // Stav `running` do bridge reportujeme dál; title se změní při session.status.
    // -------------------------------------------------------------------------
    "tool.execute.before": async (_input, _output) => {
      try {
        isIdle = false
        await reportState("running")
      } catch (err) {
        await logCaught("tool.execute.before", err, "tool-execute-before-error")
      }
    },

    // -------------------------------------------------------------------------
    // Události session + lifecycle
    // -------------------------------------------------------------------------
    event: async ({ event }) => {
      try {

      // --- SERVER INSTANCE DISPOSED: OpenCode se ukončuje ---
      if (event.type === "server.instance.disposed") {
        isIdle = false
        stopActiveBridgeTimers()
        if (recoveryTimer) { clearInterval(recoveryTimer); recoveryTimer = null }
        clearHstatusPulseTimers()
        clearTitleTimer()
        desiredTitle = null
        if (registeredSessionID) {
          if (AGENT_NOTIFY_AVAILABLE) {
            fireAndForget(
              runAgentNotify("end", registeredSessionID, directory),
              "agent-notify-end"
            )
          }
          fireAndForget(runBridgeClient("unregister", registeredSessionID), "bridge-unregister")
        }
        if (STY && !inSandbox) {
          fireAndForget($`screen -S ${STY} -p ${WINDOW} -X dynamictitle on`.nothrow(), "screen-dynamictitle-on")
        }
        return
      }

      // --- SESSION CREATED: nová top-level session (při /new) ---
      if (event.type === "session.created") {
        const info = event.properties.info
        // Ignorovat sub-session (subagenti mají parentID)
        if (info.parentID) return

        const name = info.directory.split("/").pop() || info.directory
        // Reset agenta na null — nová session, neznámý agent dokud model neodpoví
        currentAgent = null
        await clearScreenHstatus()
        scheduleTitle(buildTitle("🟢", name), buildAscii("AI.", name))

        const bid = bridgeID(info.id)
        desiredBridgeSessionID = bid
        desiredProjectDir = info.directory
        opencodeSessionID = info.id  // raw OpenCode ID for prompt_async
        if (bridgeDisabled) {
          registeredSessionID = null
          process.env.BRIDGE_SESSION_ID = ""
          return
        }
        const regRes = await runBridgeClient("register", makeRegPayload(bid, info.directory))
        if (regRes.exitCode === 0 && AGENT_NOTIFY_AVAILABLE) {
          await runAgentNotify("start", bid, info.directory)
        }
        registeredSessionID = regRes.exitCode === 0 ? bid : null
        ocToBridge.set(info.id, bid)
        process.env.BRIDGE_SESSION_ID = regRes.exitCode === 0 ? bid : ""
        if (!registeredSessionID && !bridgeDisabled) {
          stopActiveBridgeTimers()
          ensureRecoveryTimer()
        }
        if (registeredSessionID) await reportState("idle")
        return
      }

      // --- SESSION DELETED: odhlásit session z bridge ---
      if (event.type === "session.deleted") {
        const info = event.properties.info
        if (info.parentID) return
        const bid = ocToBridge.get(info.id) ?? bridgeID(info.id)
        if (AGENT_NOTIFY_AVAILABLE) {
          await runAgentNotify("end", bid, info.directory)
        }
        await runBridgeClient("unregister", bid)
        ocToBridge.delete(info.id)
        // Resetovat registeredSessionID a zastavit reregTimer — jinak by timer
        // dál volal reportState pro smazanou session a okamžitě ji znovu registroval.
        if (registeredSessionID === bid) {
          registeredSessionID = null
          process.env.BRIDGE_SESSION_ID = ""
        }
        if (desiredBridgeSessionID === bid) desiredBridgeSessionID = null
        if (opencodeSessionID === info.id) opencodeSessionID = null
        stopActiveBridgeTimers()
        ensureRecoveryTimer()
        return
      }

      // --- SESSION STATUS: indikace stavu v titulu ---
      // session.status.type je objekt: { type: "busy" } nebo { type: "idle" }
      // "busy" = model začal generovat — agent se nemění, jen přepneme na 🔵.
      if (event.type === "session.status") {
        const statusType = event.properties.status?.type
        if (statusType === "busy") {
          isIdle = false
          // currentAgent se nemění — zachováme posledního známého agenta
          scheduleTitle(buildTitle("🔵"), buildAscii("AI*", projectName), { immediate: true })
          await reportState("running")
        }
        return
      }

      // --- SESSION IDLE: semafor 🟢/🔴 + XMPP notifikace + MCP inbox poll ---
        if (event.type === "session.idle") {
          isIdle = true
          // Výchozí titulek: 🟢 (idle). Přepíše se na 🔴 pokud agent čeká na odpověď
          // na otázku (question part v poslední assistant zprávě).
          scheduleTitle(buildTitle("🟢"), buildAscii("AI.", projectName))
          if (bridgeDisabled) return
          const stateFailed = await reportState("idle")
          await reregisterIfNeeded(stateFailed)

        const sessionID = event.properties.sessionID

        // --- Push delivery: drain MCP inbox and inject via prompt_async ---
        // prompt_async is fire-and-forget — no need for 1.5s delay.
        // All messages are concatenated and injected in one prompt.
        await dbg("session.idle fired — registeredSessionID=" + registeredSessionID + " opencodeSessionID=" + opencodeSessionID)
        await pollInbox()

        const notifyEnabled =
          await $`test -f ${process.env.HOME}/.config/xmpp-notify/notify-enabled`.nothrow()
        if (notifyEnabled.exitCode !== 0) return

        const res = await client.session.messages({
          path:  { id: sessionID },
          query: { limit: 50 },
        })
        if (!res.data) return

        const lastAssistant = [...res.data]
          .reverse()
          .find(m => m.info.role === "assistant")
        if (!lastAssistant) return

        // --- Detekce question: pokud agent čeká na odpověď, přepnout na 🔴 ---
        // OpenCode Question tool generuje part s type "question" v assistant zprávě.
        // Detekujeme i typ "ask" pro případ budoucích změn v OpenCode SDK.
        const hasQuestion = lastAssistant.parts.some(
          p => p.type === "question" || p.type === "ask"
        )
        if (hasQuestion) {
          scheduleTitle(buildTitle("🔴"), buildAscii("AI?", projectName), { immediate: true })
          await reportState("interaction")
          await dbg("question detected in last assistant message — showing 🔴")
        }

        const textPart = lastAssistant.parts.find(p => p.type === "text")
        if (!textPart) return

        const text = textPart.text.slice(0, 500) || "dokončeno"

        const payload = JSON.stringify({
          session_id: sessionID,
          project:    lastAssistant.info.path?.cwd ?? directory,
          message:    text,
        })
        await runBridgeClient("response", payload)
        return
      }

      // --- MESSAGE UPDATED: detekce aktivního agenta ---
      // AssistantMessage nese pole info.agent s názvem agenta (např. "coder", "build").
      // Aktualizujeme currentAgent a titulek okna — agent kolečko se změní.
      // Toto je jediný spolehlivý způsob detekce agenta (Tab-přepnutí nemá server event).
      if (event.type === "message.updated") {
        const info = event.properties.info
        if (info?.role === "assistant" && info?.agent) {
          const newAgent = info.agent
          if (newAgent !== currentAgent) {
            currentAgent = newAgent
            await dbg("agent → " + currentAgent + " (" + agentIcon(currentAgent) + ")")
            // Titulek neaktualizujeme hned — message.updated může přicházet uprostřed
            // render burstu. Nová agent ikona se promítne při nejbližším hrubém
            // stavovém přechodu (session.status/session.idle/permission.*).
          }
        }
        return
      }

      // --- PERMISSION ASKED: semafor 🔴 + report asking state + XMPP notifikace ---
       // OpenCode nečeká na výsledek event handlerů — TUI dialog nelze zavřít
       // z pluginu přes permission.asked event. Posíláme tedy jen notifikaci
       // co se chystá spustit; potvrzení musí jít přes TUI.
         if (event.type === "permission.asked") {
           // Titulek: zachovat agent ikonu, přepnout stav na 🔴
           scheduleTitle(buildTitle("🔴"), buildAscii("AI!", projectName), { immediate: true })
           // Report asking state — bridge asking guard fallbackne na inbox místo screen inject
           isIdle = false
           await reportState("asking")
           if (bridgeDisabled) return

          const askEnabled =
            await $`test -f ${process.env.HOME}/.config/xmpp-notify/ask-enabled`.nothrow()
        if (askEnabled.exitCode !== 0) return

        const perm = event.properties
        const meta    = perm.metadata ?? {}
        const pattern = (perm.patterns ?? [])[0] ?? ""
        let detail = ""

        switch (perm.permission) {
          case "bash": {
            const desc = meta.description ?? ""
            const cmd  = pattern || String(meta.command ?? "").slice(0, 300)
            detail = desc ? `${desc}\n$ ${cmd}` : `$ ${cmd}`
            break
          }
          case "edit":
          case "write":
          case "multiedit": {
            const file = pattern || (meta.filePath ?? meta.file_path ?? "")
            const old  = String(meta.old_string ?? meta.oldString ?? "").slice(0, 100)
            detail = old ? `${file}\n- ${old}...` : file
            break
          }
          default: {
            detail = pattern || JSON.stringify(meta).slice(0, 200)
          }
        }

        // Použít notify — bridge sestaví prefix přes _session_prefix()
        // (sjednocený formát s Claude Code a dalšími aplikacemi)
        const sessionID = perm.sessionID ?? registeredSessionID ?? ""
        const payload = JSON.stringify({
          cmd:        "notify",
          session_id: sessionID,
          source:     "opencode",
          project:    directory,
          message:    `${perm.permission}\n${detail}`,
        })
        await runBridgeClient("notify", payload)
        return
      }

      // --- PERMISSION REPLIED: obnovit 🔵 (dialog uzavřen, model pokračuje) ---
       if (event.type === "permission.replied") {
         scheduleTitle(buildTitle("🔵"), buildAscii("AI*", projectName))
         // Report running state — agent pokračuje po permission dialogu
         await reportState("running")
         return
       }
      } catch (err) {
        await logCaught(`event:${event.type}`, err, `event-error:${event.type}`)
      }
    },
  }
}
