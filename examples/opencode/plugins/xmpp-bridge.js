/**
 * OpenCode XMPP Bridge Plugin
 *
 * Integrace OpenCode s claude-xmpp-bridge:
 *  - při startu pluginu       → přejmenuje okno na 🧠projekt (ANSI escape)
 *                               + zaregistruje aktivní session (odloženě, po server startu)
 *  - session.created          → registrace nové top-level session (při /new)
 *  - session.deleted          → odhlásí session z bridge
 *  - session.idle             → titul 🧠❓projekt + XMPP notifikace s poslední odpovědí
 *                               + okamžitý MCP inbox poll
 *  - session.status (running) → titul 🧠projekt
 *  - permission.asked         → informativní XMPP notifikace (co se chystá spustit); potvrzení přes TUI
 *  - server.instance.disposed → unregister + obnova původního screen titulu
 *
 * MCP inbox polling:
 *   - Při session.idle: okamžitý poll
 *   - Každých 30 s (IDLE_POLL_INTERVAL_MS): periodický poll, pouze pokud je agent idle.
 *     Zajišťuje doručení zpráv bez nutnosti "probudit" agenta dalším dotazem.
 *     Platí pouze pro screen sessions (STY musí být nastaveno).
 *
 * Zapínání/vypínání:
 *   touch ~/.config/xmpp-notify/notify-enabled   # notifikace (idle)
 *
 * Vyžaduje: claude-xmpp-bridge démon + claude-xmpp-client v $PATH
 */

export const XmppBridgePlugin = async ({ client, directory, $ }) => {
  const PLUGIN_VERSION = "0.7.10"

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
  const IDLE_POLL_INTERVAL_MS = 30_000
  let isIdle = false
  let pollTimer = null
  let polling = false  // guard against concurrent pollInbox calls

  // ---------------------------------------------------------------------------
  // messageBuffer: lokální fronta zpráv čekajících na doručení.
  // Zprávy se vybírají po jedné per poll cycle, aby se předešlo race condition
  // kdy druhá zpráva dorazí dřív než model zpracuje první (→ "assistant prefill" chyba).
  // ---------------------------------------------------------------------------
  let messageBuffer = []

  // ---------------------------------------------------------------------------
  // pollInbox(): zkontroluje MCP inbox a doručí čekající zprávy do terminálu.
  // Vždy injektuje nejvýše JEDNU zprávu — zbytek jde do messageBuffer.
  // Volá se okamžitě při session.idle a periodicky každých 30s pokud isIdle.
  // Funguje jen pro screen sessions (STY musí být nastaveno).
  // Guard: `polling` flag zabrání concurrent spuštění (session.idle + interval).
  // ---------------------------------------------------------------------------
  const dbg = (msg) => client.app.log({ body: { service: "xmpp-bridge", level: "info", message: msg } }).catch(() => {})

  // ---------------------------------------------------------------------------
  // rawRelay(): posílá zprávu přes claude-xmpp-client relay BEZ bun shell.
  // Důvod: bun shell $`...` interpretuje shell metaznaky ($, |, ', >) v obsahu
  // zprávy, čímž ji poškodí. Bun.spawn předá argumenty přímo (exec, ne shell).
  // "--" před msg zajistí, že zprávy začínající "-" nejsou interpretovány jako
  // přepínače CLI. stdout: "ignore" zabrání zablokování při přeplnění pipe bufferu.
  // ---------------------------------------------------------------------------
  const rawRelay = async (to, msg) => {
    try {
      const proc = Bun.spawn(["claude-xmpp-client", "relay", "--to", to, "--", msg], {
        stdout: "ignore", stderr: "pipe",
      })
      const exitCode = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      return { exitCode, stderr }
    } catch (err) {
      return { exitCode: -1, stderr: String(err) }
    }
  }

  const pollInbox = async () => {
    if (!registeredSessionID || !STY || polling) return
    polling = true
    try {
      // Nejdřív zkusit lokální buffer — pokud tam je zpráva, injektovat ji
      // a nechodit vůbec na MCP (model ještě zpracovává předchozí).
      if (messageBuffer.length > 0) {
        const msg = messageBuffer.shift()
        await dbg("relaying buffered msg to " + registeredSessionID + ": " + msg.slice(0, 80))
        const relayRes = await rawRelay(registeredSessionID, msg)
        await dbg("relay exit=" + relayRes.exitCode + " stderr=" + relayRes.stderr.slice(0, 200))
        return
      }

      // Step 1: initialize — get mcp-session-id
      const initRes = await fetch("http://127.0.0.1:7878/mcp", {
        method:  "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" },
        body: JSON.stringify({
          jsonrpc: "2.0", id: 1, method: "initialize",
          params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "opencode-plugin", version: "1.0" } },
        }),
      }).catch((e) => { dbg("MCP init fetch error: " + e); return null })

      const mcpSessionId = initRes?.headers?.get("mcp-session-id")
      await dbg("MCP init status=" + initRes?.status + " mcp-session-id=" + mcpSessionId)
      if (!mcpSessionId) throw new Error("no mcp-session-id")

      // Step 2: tools/call with session header
      const mcpRes = await fetch("http://127.0.0.1:7878/mcp", {
        method:  "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json, text/event-stream",
          "Mcp-Session-Id": mcpSessionId,
        },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id:      2,
          method:  "tools/call",
          params:  {
            name:      "receive_messages",
            arguments: { session_id: registeredSessionID },
          },
        }),
      }).catch((e) => { dbg("MCP tools/call fetch error: " + e); return null })

      await dbg("MCP tools/call status=" + mcpRes?.status + " ok=" + mcpRes?.ok)
      if (mcpRes && mcpRes.ok) {
        const text = await mcpRes.text().catch(() => null)
        const dataLine = text?.split('\n').find(l => l.startsWith('data:'))
        const body = dataLine ? JSON.parse(dataLine.slice(5).trim()) : null
        await dbg("MCP body contentItems=" + JSON.stringify(body?.result?.content))
        // receive_messages returns each message as a separate content item (type=text)
        const contentItems = body?.result?.content
        if (Array.isArray(contentItems) && contentItems.length > 0) {
          // Injektovat pouze PRVNÍ zprávu; zbytek do lokálního bufferu.
          // Každá další zpráva se injektuje až po session.idle (model zpracoval předchozí).
          const first = contentItems[0]?.text
          if (first) {
            // Přidat zbytek do bufferu (budou injektovány postupně při dalších poll cycles)
            for (const item of contentItems.slice(1)) {
              if (item?.text) messageBuffer.push(item.text)
            }
            await dbg("relaying msg to " + registeredSessionID + ": " + first.slice(0, 80)
              + (messageBuffer.length ? " (+" + messageBuffer.length + " buffered)" : ""))
            // Inject into session via raw exec (ne bun shell — chrání metaznaky ve zprávách)
            const relayRes = await rawRelay(registeredSessionID, first)
            await dbg("relay exit=" + relayRes.exitCode + " stderr=" + relayRes.stderr.slice(0, 200))
          }
        }
      }
    } catch (err) {
      await dbg("MCP poll error: " + err)
    } finally {
      polling = false
    }
  }

  // ---------------------------------------------------------------------------
  // Pomocník pro nastavení titulu okna.
  // Mimo sandbox: screen -X title (přímý přístup k socket démonovi).
  // Uvnitř sandboxu nebo bez STY: ANSI escape na /dev/tty.
  // ---------------------------------------------------------------------------
  const setTitle = async (title) => {
    if (STY) {
      const res = await $`screen -S ${STY} -p ${WINDOW} -X title ${title}`.nothrow()
      if (res.exitCode === 0) return
    }
    // Fallback: ANSI escape na /dev/tty (funguje v sandboxu i v tmux)
    await $`printf '\x1bk%s\x1b\\' ${title}`.nothrow()
    await $`printf '\x1b]2;%s\x07' ${title}`.nothrow()
  }

  // ---------------------------------------------------------------------------
  // 1. Přejmenovat okno na 🧠⏸projekt při startu (idle — čeká na vstup).
  //    Stav se mění dle událostí:
  //      🧠⏸ = idle (session.idle, startup, permission.replied)
  //      🧠▶ = running (session.status: running)
  //      🧠❓ = vyžaduje interakci (permission.asked)
  // ---------------------------------------------------------------------------
  await setTitle("🧠⏸" + projectName)

  // ---------------------------------------------------------------------------
  // 2. Registrace aktivní session do bridge — ODLOŽENA přes setImmediate()
  //    Důvod: client.session.list() volá HTTP na server, který v tento moment
  //    teprve načítá pluginy → synchronní volání způsobí deadlock a zamrznutí.
  //    setImmediate() naplánuje kód na příští iteraci event loop, kdy je server
  //    již plně připraven a schopen odpovídat.
  // ---------------------------------------------------------------------------
  setImmediate(async () => {
    try {
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
      if (STY) {
        const listRes = await $`claude-xmpp-client list`.nothrow()
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

      registeredSessionID = bid
      ocToBridge.set(active.id, bid)
      // Export identity do env — bash tools agenta vidí $BRIDGE_SESSION_ID
      process.env.BRIDGE_SESSION_ID = bid

      const reg = JSON.stringify({
        session_id:     bid,
        sty:            STY,
        window:         WINDOW,
        project:        active.directory,
        backend:        BACKEND,
        source:         "opencode",
        plugin_version: PLUGIN_VERSION,
      })
      await $`claude-xmpp-client register ${reg}`.nothrow()
      await $`${process.env.HOME}/claude-home/agent-notify.sh start ${bid} ${active.directory}`.nothrow()
      // Report initial state (agent is idle at startup)
      await $`claude-xmpp-client state ${JSON.stringify({session_id: bid, state: "idle"})}`.nothrow()

      // Spustit periodický inbox polling po registraci
      if (STY && !pollTimer) {
        isIdle = true  // agent je při startu idle (čeká na vstup)
        pollTimer = setInterval(async () => {
          if (isIdle) await pollInbox()
        }, IDLE_POLL_INTERVAL_MS)
      }
    } catch (_) {
      // Bridge neběží nebo session list selhal — tiše přeskočit
    }
  })

  return {
    // -------------------------------------------------------------------------
    // Události session + lifecycle
    // -------------------------------------------------------------------------
    event: async ({ event }) => {

      // --- SERVER INSTANCE DISPOSED: OpenCode se ukončuje ---
      if (event.type === "server.instance.disposed") {
        isIdle = false
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
        messageBuffer = []
        if (registeredSessionID) {
          await $`${process.env.HOME}/claude-home/agent-notify.sh end ${registeredSessionID} ${directory}`.nothrow()
          await $`claude-xmpp-client unregister ${registeredSessionID}`.nothrow()
        }
        // Resetovat titul okna
        await setTitle("")
        return
      }

      // --- SESSION CREATED: nová top-level session (při /new) ---
      if (event.type === "session.created") {
        const info = event.properties.info
        // Ignorovat sub-session (subagenti mají parentID)
        if (info.parentID) return

        const name = info.directory.split("/").pop() || info.directory
        await setTitle("🧠⏸" + name)

        const bid = bridgeID(info.id)
        const reg = JSON.stringify({
          session_id:     bid,
          sty:            STY,
          window:         WINDOW,
          project:        info.directory,
          backend:        BACKEND,
          source:         "opencode",
          plugin_version: PLUGIN_VERSION,
        })
        await $`claude-xmpp-client register ${reg}`.nothrow()
        await $`${process.env.HOME}/claude-home/agent-notify.sh start ${bid} ${info.directory}`.nothrow()
        await $`claude-xmpp-client state ${JSON.stringify({session_id: bid, state: "idle"})}`.nothrow()
        registeredSessionID = bid
        ocToBridge.set(info.id, bid)
        process.env.BRIDGE_SESSION_ID = bid
        return
      }

      // --- SESSION DELETED: odhlásit session z bridge ---
      if (event.type === "session.deleted") {
        const info = event.properties.info
        if (info.parentID) return
        const bid = ocToBridge.get(info.id) ?? bridgeID(info.id)
        await $`${process.env.HOME}/claude-home/agent-notify.sh end ${bid} ${info.directory}`.nothrow()
        await $`claude-xmpp-client unregister ${bid}`.nothrow()
        ocToBridge.delete(info.id)
        return
      }

      // --- SESSION STATUS: indikace stavu v titulu ---
      if (event.type === "session.status") {
        const status = event.properties.status
        if (status === "running") {
          isIdle = false
          await setTitle("🧠▶" + projectName)
          if (registeredSessionID) {
            await $`claude-xmpp-client state ${JSON.stringify({session_id: registeredSessionID, state: "running"})}`.nothrow()
          }
        }
        return
      }

      // --- SESSION IDLE: titul 🧠⏸ + XMPP notifikace + MCP inbox poll ---
      if (event.type === "session.idle") {
        isIdle = true
        // Titul: přepnout na 🧠⏸ (čeká na vstup, bez potřeby interakce)
        await setTitle("🧠⏸" + projectName)
        if (registeredSessionID) {
          await $`claude-xmpp-client state ${JSON.stringify({session_id: registeredSessionID, state: "idle"})}`.nothrow()
        }

        const sessionID = event.properties.sessionID

        // --- MCP inbox poll: check for pending inter-agent messages ---
        // Calls the xmpp-bridge MCP tool receive_messages() to drain any messages
        // that other agents sent via send_message() or broadcast_message().
        // Each pending message is injected into this session via screen relay.
        // Delay 1.5s: session.idle fires immediately after model finishes — OpenCode
        // needs a moment to fully transition to "awaiting user input" state before
        // we inject a new message, otherwise the message arrives while the conversation
        // still ends with an assistant turn → "assistant message prefill" API error.
        await dbg("session.idle fired — registeredSessionID=" + registeredSessionID + " STY=" + STY + " WINDOW=" + WINDOW)
        await new Promise(resolve => setTimeout(resolve, 1500))
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

        const textPart = lastAssistant.parts.find(p => p.type === "text")
        if (!textPart) return

        const text = textPart.text.slice(0, 500) || "dokončeno"

        const payload = JSON.stringify({
          session_id: sessionID,
          project:    lastAssistant.info.path?.cwd ?? directory,
          message:    text,
        })
        await $`claude-xmpp-client response ${payload}`.nothrow()
        return
      }

      // --- PERMISSION ASKED: titul 🧠❓ + informativní XMPP notifikace ---
      // OpenCode nečeká na výsledek event handlerů — TUI dialog nelze zavřít
      // z pluginu přes permission.asked event. Posíláme tedy jen notifikaci
      // co se chystá spustit; potvrzení musí jít přes TUI.
      if (event.type === "permission.asked") {
        // Titul: přepnout na 🧠❓ (vyžaduje interakci uživatele — potvrzení v TUI)
        await setTitle("🧠❓" + projectName)

        const notifyEnabled =
          await $`test -f ${process.env.HOME}/.config/xmpp-notify/notify-enabled`.nothrow()
        if (notifyEnabled.exitCode !== 0) return

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
        await $`claude-xmpp-client notify ${payload}`.nothrow()
        return
      }

      // --- PERMISSION REPLIED: obnovit titul 🧠▶ (dialog uzavřen, model pokračuje) ---
      if (event.type === "permission.replied") {
        await setTitle("🧠▶" + projectName)
        return
      }
    },
  }
}
