# Multi-agent integration test

Manuální end-to-end test ověřující inter-agent komunikaci přes XMPP bridge.
Spouštěn z agenta na **w6** (`/home/xhercet/projects/claude-xmpp-bridge`).

Všechny zprávy jdou přes MCP tool `xmpp-bridge_send_message` s `nudge=true`,
pokud není uvedeno jinak.

## Prerekvizity

- Bridge service běží: `systemctl --user is-active claude-xmpp-bridge`
- Alespoň 3 agenti registrovaní v bridge DB
- Session ID zjistit přes MCP tool `xmpp-bridge_list_sessions`

## Sessions (příklad)

```
w1  ses_33ade13e0ffepJK5MMnNntTmNp_w1   ~/llm_trader
w4  ses_33139cf6effemiwJtcO2nivQDb_w4   ~/claude-home
w5  ses_3313be51effeUTGLWGv4KoyhcA_w5   ~/claude-home
w6  ses_33559cd9dffecr6KTs6AS103JJ_w6   ~/projects/claude-xmpp-bridge  (orchestrator)
```

Session ID se po restartu OpenCode mění — vždy zjistit aktuální přes `list_sessions`.

---

## TEST 1 — Bidirectional nudge (w6 <-> w4)

**Cíl:** Ověřit že agent umí přijmout zprávu a odpovědět zpět.

**Zpráva na w4:**
```
BIDIR TEST: Ahoj w4! Pošli zpět na <W6_SESSION_ID> přes MCP send_message
nudge=true zprávu "BIDIR ACK od w4".
```

**Očekávaná odpověď v inbox w6:**
```
BIDIR ACK od w4
```

| Verze  | Výsledek | Čas  |
|--------|----------|------|
| v0.7.8 | PASS     | ~15s |
| v0.7.10| PASS     | ~30s |

---

## TEST 2 — Fan-out (w6 -> w1, w4, w5 paralelně)

**Cíl:** Ověřit paralelní distribuci úkolů a sběr výsledků.

**Zprávy (odeslat najednou):**

Na w1:
```
FANOUT TEST: Tvůj úkol (w1): součet čísel 1 až 100. Pošli výsledek na
<W6_SESSION_ID> přes MCP send_message nudge=true. Zpráva musí být přesně:
"FANOUT w1: 5050"
```

Na w4:
```
FANOUT TEST: Tvůj úkol (w4): 10 faktoriál (10!). Pošli výsledek na
<W6_SESSION_ID> přes MCP send_message nudge=true. Zpráva musí být přesně:
"FANOUT w4: 3628800"
```

Na w5:
```
FANOUT TEST: Tvůj úkol (w5): počet prvočísel menších než 50. Pošli výsledek na
<W6_SESSION_ID> přes MCP send_message nudge=true. Zpráva musí být přesně:
"FANOUT w5: 15"
```

**Očekávané odpovědi:**
```
FANOUT w1: 5050
FANOUT w4: 3628800
FANOUT w5: 15
```

**Správné hodnoty:**
- součet 1..100 = 5050 (Gaussův vzorec: n*(n+1)/2)
- 10! = 3 628 800
- prvočísla < 50: 2,3,5,7,11,13,17,19,23,29,31,37,41,43,47 -> 15 čísel

| Verze  | Výsledek | Čas  |
|--------|----------|------|
| v0.7.8 | PASS     | ~30s |
| v0.7.10| PASS     | ~45s |

---

## TEST 3 — Chain (w6 -> w4 -> w5 -> w6)

**Cíl:** Ověřit přeposílání zprávy přes řetěz agentů.

**Zpráva na w4:**
```
CHAIN TEST hop 1/3: Přepošli tuto zprávu na <W5_SESSION_ID> přes MCP
send_message nudge=true s textem přesně:
"CHAIN TEST hop 2/3: Přepošli na <W6_SESSION_ID> přes MCP send_message
nudge=true s textem přesně: CHAIN ACK: w6->w4->w5->w6 OK"
```

**Očekávaná odpověď v inbox w6:**
```
CHAIN ACK: w6->w4->w5->w6 OK
```

| Verze  | Výsledek | Čas  |
|--------|----------|------|
| v0.7.8 | PASS     | ~30s |
| v0.7.10| PASS     | ~60s |

---

## Ověření přes screen hardcopy

Po každém testu (nebo na závěr) ověřit výstup agentů přes screen:

```bash
STY=5757.pts-0.black-arch   # zjistit: screen -ls
screen -S $STY -p 1 -X hardcopy /tmp/hc_w1.txt && tail -40 /tmp/hc_w1.txt
screen -S $STY -p 4 -X hardcopy /tmp/hc_w4.txt && tail -40 /tmp/hc_w4.txt
screen -S $STY -p 5 -X hardcopy /tmp/hc_w5.txt && tail -40 /tmp/hc_w5.txt
```

**Co kontrolovat:**
- Zpráva ze `| ...` bloku přišla správně (přesný text)
- Agent zavolal `xmpp-bridge_send_message` s očekávaným `to=` a `message=`
- MCP tool call se zobrazuje jako `xmpp-bridge_send_message [...]`

## Poznámky

- Odpovědi agentů přicházejí **přes XMPP notify** (viditelné v chatu), ne vždy
  přes `receive_messages` inbox — závisí na tom jak agent provede send_message.
- Instrukce v jedné zprávě musí být **jednoznačné** — session ID uvést explicitně,
  očekávaný formát odpovědi specifikovat přesně.
- `nudge=true` je preferovaný mód — nevyrušuje agenta při práci, zpráva se
  doručí při příštím idle cyklu.

## Changelog

### v0.7.10 (2026-03-08) — window identity fix + message integrity

**Opravené bugy:**
- Plugin čte $WINDOW z /proc/ppid/environ (ne z nespolehlivého process.env)
- Agent zná svou identitu: $BRIDGE_SESSION_ID, $BRIDGE_WINDOW, $WINDOW v bash
- `_handle_list()` vrací `sty` pole (bridge list lookup v pluginu funguje)
- `rawRelay()` (Bun.spawn místo bun shell) — chrání |, ', > ve zprávách
- `_screen_stuff_escape()` — escapuje $ a \ v screen stuff příkazu
- `pollInbox` concurrent guard (polling flag)

**Všechny testy prošly:** BIDIR, FANOUT, CHAIN

### v0.7.8 (2026-03-08) — první úspěšný běh

Všechny testy prošly, ale s window identity bugem (w4 se identifikoval jako w5).

### Známé problémy (vyřešeny ve v0.7.10)

~~w4/w5 session identity mismatch~~ — opraveno přes readWindowFromPpid().
