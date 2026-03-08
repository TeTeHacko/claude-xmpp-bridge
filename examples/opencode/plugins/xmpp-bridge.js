/**
 * OpenCode XMPP Bridge Plugin
 *
 * Integrace OpenCode s claude-xmpp-bridge:
 *  - při startu pluginu       → přejmenuje okno na 🧠projekt (ANSI escape)
 *                               + zaregistruje aktivní session (odloženě, po server startu)
 *  - session.created          → registrace nové top-level session (při /new)
 *  - session.deleted          → odhlásí session z bridge
 *  - session.idle             → titul 🧠❓projekt + XMPP notifikace s poslední odpovědí
 *  - session.status (running) → titul 🧠projekt
 *  - permission.asked         → informativní XMPP notifikace (co se chystá spustit); potvrzení přes TUI
 *  - server.instance.disposed → unregister + obnova původního screen titulu
 *
 * Zapínání/vypínání:
 *   touch ~/.config/xmpp-notify/notify-enabled   # notifikace (idle)
 *
 * Vyžaduje: claude-xmpp-bridge démon + claude-xmpp-client v $PATH
 */

/** Zkrátí absolutní cestu — nahradí $HOME za ~ */
function shortPath(dir) {
  const home = process.env.HOME ?? ""
  if (!dir) return "?"
  if (dir === home) return "~"
  if (dir.startsWith(home + "/")) return "~" + dir.slice(home.length)
  return dir
}

export const XmppBridgePlugin = async ({ client, directory, $ }) => {
  const STY     = process.env.STY    ?? ""
  const BACKEND = STY
    ? "screen"
    : process.env.TMUX
      ? "tmux"
      : "none"

  // $WINDOW z env je nastaven screenem pro každé okno zvlášť — spolehlivý zdroj.
  // screen -Q info vrací aktivní okno (ne okno pluginu) → nelze použít.
  const WINDOW = process.env.WINDOW ?? "0"

  const projectName = directory.split("/").pop() || directory

  // ---------------------------------------------------------------------------
  // bridgeID(): přidá ":wWINDOW" suffix pro screen backend.
  // Důvod: OpenCode sessions jsou sdílené přes instance — dvě okna ve stejném
  // projektu vidí stejné session ID. Suffix zaručuje unikátnost per screen okno.
  // Příklad: "ses_abc123" → "ses_abc123:w4" (v okně 4 screen session)
  // ---------------------------------------------------------------------------
  const bridgeID = (opencodeID) =>
    (STY && opencodeID) ? `${opencodeID}_w${WINDOW}` : (opencodeID ?? "")

  // Sledovaná session ID — nastavena při registraci, použita při ukončení.
  // Ukládáme bridge ID (s :wWINDOW suffixem), ne raw OpenCode ID.
  let registeredSessionID = null

  // Mapa opencode_id → bridge_id pro session.deleted handler
  const ocToBridge = new Map()

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
  // 1. Přejmenovat okno na 🧠projekt při startu.
  //    Původní titulek nelze přečíst bez screen socketu, takže ho neobnovujeme
  //    — po ukončení opencode zůstane titulek 🧠projekt, dokud ho nepřepíše
  //    další session-start-title.sh hook nebo manuálně uživatel.
  // ---------------------------------------------------------------------------
  await setTitle("🧠" + projectName)

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

      const bid = bridgeID(active.id)
      registeredSessionID = bid
      ocToBridge.set(active.id, bid)

      const reg = JSON.stringify({
        session_id: bid,
        sty:        STY,
        window:     WINDOW,
        project:    active.directory,
        backend:    BACKEND,
        source:     "opencode",
      })
      await $`claude-xmpp-client register ${reg}`.nothrow()
      await $`${process.env.HOME}/claude-home/agent-notify.sh start ${bid} ${active.directory}`.nothrow()
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
        await setTitle("🧠" + name)

        const bid = bridgeID(info.id)
        const reg = JSON.stringify({
          session_id: bid,
          sty:        STY,
          window:     WINDOW,
          project:    info.directory,
          backend:    BACKEND,
          source:     "opencode",
        })
        await $`claude-xmpp-client register ${reg}`.nothrow()
        await $`${process.env.HOME}/claude-home/agent-notify.sh start ${bid} ${info.directory}`.nothrow()
        registeredSessionID = bid
        ocToBridge.set(info.id, bid)
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
          await setTitle("🧠" + projectName)
        }
        return
      }

      // --- SESSION IDLE: titul ⌨ + XMPP notifikace + MCP inbox poll ---
      if (event.type === "session.idle") {
        // Titul: přepnout na 🧠❓ (čeká na vstup)
        await setTitle("🧠❓" + projectName)

        const sessionID = event.properties.sessionID

        // --- MCP inbox poll: check for pending inter-agent messages ---
        // Calls the xmpp-bridge MCP tool receive_messages() to drain any messages
        // that other agents sent via send_message() or broadcast_message().
        // Each pending message is injected into this session via screen relay.
        // MCP streamable-http requires: 1) POST initialize → get mcp-session-id header
        //                               2) POST tools/call with that header
        const dbg = (msg) => client.app.log({ body: { service: "xmpp-bridge", level: "info", message: msg } }).catch(() => {})
        await dbg("session.idle fired — registeredSessionID=" + registeredSessionID + " STY=" + STY + " WINDOW=" + WINDOW)
        if (registeredSessionID && STY) {
          try {
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
                for (const item of contentItems) {
                  const msg = item?.text
                  if (msg) {
                    await dbg("relaying msg to " + registeredSessionID + ": " + msg.slice(0, 80))
                    // Inject into session via screen stuff (same mechanism as socket relay)
                    const reg = JSON.stringify({ session_id: registeredSessionID, message: msg })
                    await $`claude-xmpp-client relay ${reg}`.nothrow()
                  }
                }
              }
            }
          } catch (err) {
            await dbg("MCP poll error: " + err)
          }
        }

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
        // Titul: přepnout na 🧠❓ (čeká na potvrzení)
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

      // --- PERMISSION REPLIED: obnovit titul 🧠 (dialog uzavřen) ---
      if (event.type === "permission.replied") {
        await setTitle("🧠" + projectName)
        return
      }
    },
  }
}
