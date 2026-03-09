# Nálezy a návrhy na vylepšení bridge

*Zpracováno z multi-agent session 2026-03-08, návrhy pocházejí z diskuse agentů w4/w5 (claude-home) s TTH.*

---

## Nalezené bugy

### Bug 1: `assistant message prefill` API error ✅ opraveno v 0.7.5
**Příčina:** Plugin injektoval zprávu přes screen relay ihned po `session.idle` — model ještě nebyl plně v "čeká na vstup" stavu.
**Oprava:** 1.5s delay před `pollInbox()` v `session.idle` handleru.

### Bug 2: Double-delivery při broadcast/send_message ✅ opraveno v 0.7.5
**Příčina:** `broadcast_message` i socket `broadcast` cmd po úspěšném screen relay *zároveň* enqueovaly zprávu do MCP inboxu. Plugin pak zprávu injektoval podruhé přes `pollInbox()`.
**Oprava:** Enqueue do MCP inboxu jen při *selhání* screen relay (jako fallback), ne při úspěchu.

### Bug 3: Ztráta injektované zprávy když je agent busy ✅ opraveno v 0.7.7 (nudge pattern)
**Příčina:** Screen inject (`at N# stuff`) vloží text do readline bufferu. Pokud je agent zrovna zpracovávání tool callů (není v readline), text se ztratí nebo se zpracuje neočekávaně.
**Oprava:** Nudge pattern — bridge uloží zprávu do SQLite inbox a pošle jen CR; agent si zprávu přečte sám přes `receive_messages` MCP tool při příštím `session.idle`.

### Bug 4: w4/w5 window identity mismatch ✅ opraveno v 0.7.10
**Příčina:** `process.env.WINDOW` bylo zděděno z jiného kontextu — agent v okně 4 se registroval jako `_w5`.
**Oprava:** Plugin čte `$WINDOW` z `/proc/${process.ppid}/environ` (rodičovský bash shell má správnou hodnotu nastavenou GNU Screen).

### Bug 5: Shell metaznaky poškozovaly zprávy ✅ opraveno v 0.7.10–0.7.11
**Příčina 1:** Bun shell template `$\`...\`` interpretoval `|`, `'`, `>` v obsahu zprávy.
**Oprava 1:** `rawRelay()` používá `Bun.spawn()` — přímý exec bez shell interpretace.
**Příčina 2:** GNU Screen `stuff` expandoval `$VAR` v textu zprávy.
**Oprava 2:** `_screen_stuff_escape()` escapuje `$` → `\$` a `\` → `\\` před předáním do `stuff`.

### Bug 6: permission.asked ignoroval `ask-enabled` switch ✅ opraveno v 0.7.13
**Příčina:** Handler kontroloval `notify-enabled` místo `ask-enabled`.
**Oprava:** Správný switch pro každý handler — `notify-enabled` pro `session.idle`, `ask-enabled` pro `permission.asked`.

---

## Návrhy na vylepšení (od agentů w4/w5)

### Návrh #1: Perzistentní inbox v SQLite ✅ implementováno v 0.7.6
Inbox přesunut z `asyncio.Queue` do SQLite tabulky `inbox` v `bridge.db`. Zprávy přežijí restart bridge i re-registraci session.

### Návrh #2: Strukturovaný protokol zpráv (JSON envelope)
**Problém:** Agent nerozliší strojovou zprávu od lidského vstupu — vše je prostý text.
**Řešení:** Volitelný JSON wrapper:
```json
{"from": "ses_..._w5", "type": "task|ping|result", "payload": "...", "reply_to": "ses_..._w6"}
```
**Status:** Nice-to-have, nezávisí na bridge změně, agenti si mohou sami dohodnout formát.
**Priorita:** Nízká.

### Návrh #3: Polling + nudge pattern ✅ implementováno v 0.7.7
Bridge `send_message(nudge=True)` uloží zprávu do SQLite inbox a pošle jen CR. Agent si zprávu přečte sám přes `receive_messages` MCP tool při `session.idle` — zpráva nikdy nepřeruší agenta při práci.

### Návrh #4: Session-level audit log do JSONL
**Problém:** Bridge audit log loguje vše dohromady, těžko se filtruje per-session.
**Řešení:** Bridge zapisuje kopii zpráv do `~/.claude/agent-log/<session_id>.jsonl`
**Priorita:** Nízká — není blokující.

### Návrh #5: Sandbox agent s vlastním SSH klíčem místo mountu
**Problém:** Sandbox (bwrap) aktuálně mountuje SSH klíč z hostu dovnitř. To je bezpečnostní riziko — kompromitovaný sandbox má přístup ke klíči.
**Řešení:** Místo mountu klíče vytvořit extra agenta (samostatný Claude/OpenCode proces) který běží *mimo* sandbox a má přístup ke klíči. Sandbox komunikuje s tímto agentem přes XMPP bridge (`send_message`/`receive_messages`). Agent provede SSH operaci a vrátí výsledek.
**Výhody:** Klíč nikdy nevstoupí do sandboxu. Sandbox může být plně izolovaný (no-net nebo omezený net).
**Priorita:** Medium — závisí na tom jak moc se sandbox používá pro SSH operace.

---

## Architekturální pozorování

- **MCP reconnect po restartu bridge:** OpenCode ztratí `xmpp-bridge_*` MCP nástroje po restartu bridge service. Je potřeba buď hot-reload bez restartu, nebo OpenCode MCP reconnect mechanismus.
- **screen relay spolehlivost:** `at N# stuff` je synchronní (exit 0 = success), ale nezaručuje zpracování pokud readline není aktivní. To je systémové omezení GNU Screen — řeší nudge pattern.
- **pollInbox() HTTP overhead:** Plugin vytváří nové HTTP spojení pro každý poll (initialize + tools/call). Bylo by efektivnější persistent SSE spojení, ale to vyžaduje změny v MCP server logice.
