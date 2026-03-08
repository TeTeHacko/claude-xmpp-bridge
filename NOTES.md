# Nálezy a návrhy na vylepšení bridge

*Zpracováno z multi-agent session 2026-03-08, návrhy pocházejí z diskuse agentů w4/w5 (claude-home) s TTH.*

---

## Nalezené bugy (opraveno v 0.7.5)

### Bug 1: `assistant message prefill` API error
**Příčina:** Plugin injektoval zprávu přes screen relay ihned po `session.idle` — model ještě nebyl plně v "čeká na vstup" stavu.
**Oprava:** 1.5s delay před `pollInbox()` v `session.idle` handleru.

### Bug 2: Double-delivery při broadcast/send_message
**Příčina:** `broadcast_message` i socket `broadcast` cmd po úspěšném screen relay *zároveň* enqueovaly zprávu do MCP inboxu. Plugin pak zprávu injektoval podruhé přes `pollInbox()`.
**Oprava:** Enqueue do MCP inboxu jen při *selhání* screen relay (jako fallback), ne při úspěchu.

### Bug 3: Ztráta injektované zprávy když je agent busy
**Příčina:** Screen inject (`at N# stuff`) vloží text do readline bufferu. Pokud je agent zrovna zpracovávání tool callů (není v readline), text se ztratí nebo se zpracuje neočekávaně.
**Status:** Neoplaven — viz Návrh #3 níže.

---

## Návrhy na vylepšení (od agentů w4/w5)

### Návrh #1: Perzistentní inbox v SQLite ← IMPLEMENTUJEME JAKO PRVNÍ
**Problém:** Inbox je v RAM bridge procesu → zprávy se ztratí při restartu bridge nebo unregistraci session.
**Řešení:** Přidat tabulku `inbox` do existujícího `bridge.db`:
```sql
CREATE TABLE inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_session   TEXT NOT NULL,
    from_session TEXT,
    message      TEXT NOT NULL,
    created_at   REAL NOT NULL
)
```
- `send_message` / `broadcast_message` → INSERT do inbox (místo asyncio.Queue)
- `receive_messages` → SELECT + DELETE atomicky v transakci
- Restart bridge → inbox přežije, zprávy se neztratí
- Unregistrace session → inbox se *nečistí* (agent se může znovu zaregistrovat a zprávy dostat)

**Implementace:**
- Přesunout inbox logiku z `mcp_server.py` (`_queues: dict[str, asyncio.Queue]`) do `registry.py` (kde je SQLite)
- Nebo: přidat nový modul `inbox.py` s `Inbox` třídou

### Návrh #2: Strukturovaný protokol zpráv (JSON envelope)
**Problém:** Agent nerozliší strojovou zprávu od lidského vstupu — vše je prostý text.
**Řešení:** Volitelný JSON wrapper:
```json
{"from": "ses_..._w5", "type": "task|ping|result", "payload": "...", "reply_to": "ses_..._w6"}
```
**Status:** Nice-to-have, nezávisí na bridge změně, agenti si mohou sami dohodnout formát.
**Priorita:** Nízká.

### Návrh #3: Polling + nudge pattern (neblokující doručení)
**Problém:** Screen inject přeruší agenta uprostřed práce (zpráva může být ztracena nebo zpracována v nevhodný okamžik).
**Řešení:**
- Screen inject slouží jen jako "nudge" — pošle krátký signál (např. speciální token nebo prázdný CR) aby agent věděl že má zprávu
- Agent si *sám* zavolá `receive_messages` MCP tool, zpracuje inbox ve vlastním cyklu
- Výhoda: zpráva nikdy nepřeruší agenta při práci, agent si ji přečte až je idle

**Implementace:**
- Plugin: při `session.idle` zavolat `receive_messages` aktivně (už to tak cca dělá)
- Bridge: `send_message` s `nudge=True` pošle jen `\x07` (BEL) nebo dohodnutý token do screenu + enqueue do inboxu
- Závislost: vyžaduje Návrh #1 (perzistentní inbox), aby zprávy přežily do příštího idle

**Priorita:** Střední — závisí na #1.

### Návrh #4: Session-level audit log do JSONL (volitelné)
**Problém:** Bridge audit log loguje vše dohromady, těžko se filtruje per-session.
**Řešení:** Bridge zapisuje kopii zpráv do `~/.claude/agent-log/<session_id>.jsonl`
- Jen pro debug/audit, agenti ho nečtou přímo
- Race condition při consume řeší bridge socket atomicky (soubor je append-only)

**Priorita:** Nízká.

---

## Architekturální pozorování

- **MCP reconnect po restartu bridge:** OpenCode ztratí `xmpp-bridge_*` MCP nástroje po restartu bridge service. Je potřeba buď hot-reload bez restartu, nebo OpenCode MCP reconnect mechanismus.
- **screen relay spolehlivost:** `at N# stuff` je synchronní (exit 0 = success), ale nezaručuje zpracování pokud readline není aktivní. To je systémové omezení GNU Screen.
- **pollInbox() každých 10s:** Plugin vytváří nové HTTP spojení pro každý poll (initialize + tools/call). Bylo by efektivnější persistent SSE spojení, ale to vyžaduje změny v MCP server logice.
