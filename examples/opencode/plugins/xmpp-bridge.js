/**
 * OpenCode XMPP Bridge Plugin
 *
 * Integrace OpenCode s claude-xmpp-bridge:
 *  - při startu pluginu       → přejmenuje okno na 🧠projekt
 *                               + detekuje zda bridge běží (claude-xmpp-client ping)
 *                               + pokud bridge běží: zaregistruje aktivní session (odloženě)
 *  - session.created          → titul 🧠projekt; registrace do bridge (pokud běží)
 *  - session.deleted          → odhlásí session z bridge (pokud bridge běží)
 *  - session.idle             → titul 🧠❓projekt + XMPP notifikace s poslední odpovědí (pokud bridge)
 *  - session.status (running) → titul 🧠projekt
 *  - permission.asked         → titul 🧠❓projekt + informativní XMPP notifikace (pokud bridge)
 *  - permission.replied       → titul 🧠projekt
 *  - server.instance.disposed → unregister (pokud bridge) + reset titulu
 *
 * Plugin funguje i bez bridge démon — správa titulků funguje vždy.
 * XMPP funkce se aktivují automaticky pokud bridge běží při startu OpenCode.
 *
 * Zapínání/vypínání XMPP notifikací:
 *   touch ~/.config/xmpp-notify/notify-enabled   # notifikace (idle, permission)
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
  const WINDOW  = process.env.WINDOW ?? "0"
  const BACKEND = STY
    ? "screen"
    : process.env.TMUX
      ? "tmux"
      : "none"

  const projectName = directory.split("/").pop() || directory

  // Sledovaná session ID — nastavena při registraci, použita při ukončení
  let registeredSessionID = null

  // ---------------------------------------------------------------------------
  // Pomocník pro nastavení titulu okna.
  // Mimo sandbox: screen -X title (přímý přístup k socket démonovi).
  // Uvnitř sandboxu nebo bez STY: ANSI escape přímo na /dev/tty.
  //   - Sandbox bind-mountuje /dev/tty z hostitele → zápis funguje.
  //   - Redirect >/dev/tty 2>/dev/null zajistí že výstup jde na terminál
  //     i když OpenCode subprocess nemá stdout připojený na tty.
  // ---------------------------------------------------------------------------
  const setTitle = async (title) => {
    if (STY) {
      const res = await $`screen -S ${STY} -p ${WINDOW} -X title ${title}`.nothrow()
      if (res.exitCode === 0) return
    }
    // Fallback: ANSI escape na /dev/tty (funguje v sandboxu i v tmux)
    await $`printf '\x1bk%s\x1b\\' ${title} >/dev/tty 2>/dev/null`.nothrow()
    await $`printf '\x1b]2;%s\x07' ${title} >/dev/tty 2>/dev/null`.nothrow()
  }

  // ---------------------------------------------------------------------------
  // 1. Přejmenovat okno na 🧠projekt při startu.
  // ---------------------------------------------------------------------------
  await setTitle("🧠" + projectName)

  // ---------------------------------------------------------------------------
  // 2. Detekce bridge: zkusit ping přes claude-xmpp-client.
  //    Pokud bridge neběží, XMPP funkce jsou deaktivovány — titulky fungují dál.
  //    Detekce probíhá jednou při startu; pokud bridge nastartuje později,
  //    je třeba OpenCode restartovat.
  // ---------------------------------------------------------------------------
  const pingRes = await $`claude-xmpp-client ping`.nothrow()
  const bridgeAvailable = pingRes.exitCode === 0

  // ---------------------------------------------------------------------------
  // 3. Registrace aktivní session do bridge — ODLOŽENA přes setImmediate()
  //    Důvod: client.session.list() volá HTTP na server, který v tento moment
  //    teprve načítá pluginy → synchronní volání způsobí deadlock a zamrznutí.
  //    setImmediate() naplánuje kód na příští iteraci event loop, kdy je server
  //    již plně připraven a schopen odpovídat.
  // ---------------------------------------------------------------------------
  if (bridgeAvailable) {
    setImmediate(async () => {
      try {
        const sessionsRes = await client.session.list()
        if (!sessionsRes.data || sessionsRes.data.length === 0) return

        // Seřadit podle time.updated — nejnovější = aktivní
        const sorted = [...sessionsRes.data].sort(
          (a, b) => b.time.updated - a.time.updated
        )
        // Zaregistrovat jen top-level session (bez parentID) — ne subagenty
        const active = sorted.find(s => !s.parentID)
        if (!active) return

        registeredSessionID = active.id

        const reg = JSON.stringify({
          session_id: active.id,
          sty:        STY,
          window:     WINDOW,
          project:    active.directory,
          backend:    BACKEND,
          source:     "opencode",
        })
        await $`claude-xmpp-client register ${reg}`.nothrow()
      } catch (_) {
        // Bridge neběží nebo session list selhal — tiše přeskočit
      }
    })
  }

  return {
    // -------------------------------------------------------------------------
    // Události session + lifecycle
    // -------------------------------------------------------------------------
    event: async ({ event }) => {

      // --- SERVER INSTANCE DISPOSED: OpenCode se ukončuje ---
      if (event.type === "server.instance.disposed") {
        if (bridgeAvailable && registeredSessionID) {
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

        if (bridgeAvailable) {
          const reg = JSON.stringify({
            session_id: info.id,
            sty:        STY,
            window:     WINDOW,
            project:    info.directory,
            backend:    BACKEND,
            source:     "opencode",
          })
          await $`claude-xmpp-client register ${reg}`.nothrow()
          registeredSessionID = info.id
        }
        return
      }

      // --- SESSION DELETED: odhlásit session z bridge ---
      if (event.type === "session.deleted") {
        const info = event.properties.info
        if (info.parentID) return
        if (bridgeAvailable) {
          await $`claude-xmpp-client unregister ${info.id}`.nothrow()
        }
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

      // --- SESSION IDLE: titul 🧠❓ + XMPP notifikace ---
      if (event.type === "session.idle") {
        // Titul: přepnout na 🧠❓ (čeká na vstup)
        await setTitle("🧠❓" + projectName)

        if (!bridgeAvailable) return

        const notifyEnabled =
          await $`test -f ${process.env.HOME}/.config/xmpp-notify/notify-enabled`.nothrow()
        if (notifyEnabled.exitCode !== 0) return

        const sessionID = event.properties.sessionID

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

        if (!bridgeAvailable) return

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
