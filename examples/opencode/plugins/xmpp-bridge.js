/**
 * OpenCode XMPP Bridge Plugin
 *
 * Integrace OpenCode s claude-xmpp-bridge:
 *  - při startu pluginu       → uloží původní screen titul, přejmenuje na 🧠projekt
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
  // Pomocník pro nastavení screen titulu
  // ---------------------------------------------------------------------------
  const setTitle = async (title) => {
    if (!STY) return
    await $`screen -S ${STY} -p ${WINDOW} -X title ${title}`.nothrow()
  }

  // ---------------------------------------------------------------------------
  // 1. Uložit původní screen titul a přejmenovat na 🧠projekt
  //    originalTitle se ukládá i do tmp souboru — záloha pro případ, že
  //    server.instance.disposed event nepřijde (OpenCode ukončí event loop
  //    dříve než handler doběhne).
  // ---------------------------------------------------------------------------
  let originalTitle = null
  if (STY) {
    const res = await $`screen -S ${STY} -p ${WINDOW} -Q title`.nothrow()
    originalTitle = res.stdout?.toString().trim() || null
    await setTitle("🧠" + projectName)

    // Uložit do tmp souboru (záloha pro restore po ukončení)
    const titleFile = `/tmp/opencode-title-${STY.replace(/\./g, "-")}-${WINDOW}`
    try { Bun.write(titleFile, originalTitle ?? "") } catch (_) {}
  }

  // Synchronní restore při ukončení procesu (SIGTERM/SIGINT/exit)
  // Bun.spawnSync je synchronní → funguje i v exit handleru.
  // originalTitle je zachycen v closure — nepotřebujeme fs.readFile.
  const _restoreTitle = () => {
    if (!STY) return
    const titleFile = `/tmp/opencode-title-${STY.replace(/\./g, "-")}-${WINDOW}`
    const title = originalTitle ?? ""
    try {
      Bun.spawnSync(["screen", "-S", STY, "-p", WINDOW, "-X", "title", title])
    } catch (_) {}
    try { Bun.spawnSync(["rm", "-f", titleFile]) } catch (_) {}
  }
  process.once("exit",    _restoreTitle)
  process.once("SIGTERM", () => { _restoreTitle(); process.exit(0) })
  process.once("SIGINT",  () => { _restoreTitle(); process.exit(0) })

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

  return {
    // -------------------------------------------------------------------------
    // Události session + lifecycle
    // -------------------------------------------------------------------------
    event: async ({ event }) => {

      // --- SERVER INSTANCE DISPOSED: OpenCode se ukončuje ---
      if (event.type === "server.instance.disposed") {
        if (registeredSessionID) {
          await $`claude-xmpp-client unregister ${registeredSessionID}`.nothrow()
        }
        // Obnovit původní titul (nebo prázdný string = screen default)
        if (STY) {
          await setTitle(originalTitle ?? "")
        }
        return
      }

      // --- SESSION CREATED: nová top-level session (při /new) ---
      if (event.type === "session.created") {
        const info = event.properties.info
        // Ignorovat sub-session (subagenti mají parentID)
        if (info.parentID) return

        const name = info.directory.split("/").pop() || info.directory
        await setTitle("🧠" + name)

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
        return
      }

      // --- SESSION DELETED: odhlásit session z bridge ---
      if (event.type === "session.deleted") {
        const info = event.properties.info
        if (info.parentID) return
        await $`claude-xmpp-client unregister ${info.id}`.nothrow()
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

      // --- SESSION IDLE: titul ⌨ + XMPP notifikace ---
      if (event.type === "session.idle") {
        // Titul: přepnout na 🧠❓ (čeká na vstup)
        await setTitle("🧠❓" + projectName)

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
