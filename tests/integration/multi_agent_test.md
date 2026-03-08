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
w4  ses_332172680ffetPq8Ia6XkqXDVI_w4   ~/claude-home
w5  ses_332172680ffetPq8Ia6XkqXDVI_w5   ~/claude-home
w6  ses_33559cd9dffecr6KTs6AS103JJ_w6   ~/projects/claude-xmpp-bridge  ← orchestrátor
```

Session ID se po restartu OpenCode mění — vždy zjistit aktuální přes `list_sessions`.

---

## TEST 1 — Bidirectionální nudge (w6 ↔ w1)

**Cíl:** Ověřit že agent umí přijmout zprávu a odpovědět zpět.

**Zpráva na w1:**
```
BIDIR TEST: Ahoj w1! Pošli zpět na <W6_SESSION_ID> přes MCP send_message
nudge=true zprávu "BIDIR ACK od w1".
```

**Očekávaná odpověď v inbox w6:**
```
BIDIR ACK od w1
```

**Výsledek (v0.7.8):** ✅ ~15s

---

## TEST 2 — Fan-out (w6 → w1, w4, w5 paralelně)

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

**Očekávané odpovědi (v inbox nebo přes XMPP):**
```
FANOUT w1: 5050
FANOUT w4: 3628800
FANOUT w5: 15
```

**Správné hodnoty:**
- součet 1..100 = 5050 (Gaussův vzorec: n*(n+1)/2)
- 10! = 3 628 800
- prvočísla < 50: 2,3,5,7,11,13,17,19,23,29,31,37,41,43,47 → 15 čísel

**Výsledek (v0.7.8):** ✅ ~30s (odpovědi přicházejí přes XMPP notify, ne inbox)

---

## TEST 3 — Chain (w6 → w1 → w4 → w6)

**Cíl:** Ověřit přeposílání zprávy přes řetěz agentů.

**Zpráva na w1:**
```
CHAIN TEST hop 1/3: Přepošli tuto zprávu na <W4_SESSION_ID> přes MCP
send_message nudge=true s textem přesně:
"CHAIN TEST hop 2/3: Přepošli na <W6_SESSION_ID> přes MCP send_message
nudge=true s textem přesně: CHAIN ACK: w6→w1→w4→w6 OK"
```

**Očekávaná odpověď v inbox w6:**
```
CHAIN ACK: w6→w1→w4→w6 OK
```

**Výsledek (v0.7.8):** ✅ ~30s

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
- Zpráva ze `┃ ...` bloku přišla správně (přesný text)
- Agent zavolal `xmpp-bridge_send_message` s očekávaným `to=` a `message=`
- MCP tool call se zobrazuje jako `⚙ xmpp-bridge_send_message [...]`
- Čas zpracování (`▣ Build · claude-sonnet-4-6 · Xs`) — typicky 4–8s

**Hardcopy výstup z testu (2026-03-08, v0.7.8):**

w1 — správně zpracoval všechny 3 zprávy (BIDIR, FANOUT, CHAIN hop 1/3):
```
⚙ xmpp-bridge_send_message [to=ses_33559..._w6, message=BIDIR ACK od w1, nudge=true]
⚙ xmpp-bridge_send_message [to=ses_33559..._w6, message=FANOUT w1: 5050, nudge=true]
⚙ xmpp-bridge_send_message [to=ses_332172..._w4, message=CHAIN TEST hop 2/3: ..., nudge=true]
```

w4 screen window — zprávy zpracoval agent identifikující se jako **w5** (viz bug níže):
```
⚙ xmpp-bridge_send_message [to=ses_33559..._w6, message=FANOUT w4: 3628800, nudge=true]
⚙ xmpp-bridge_send_message [to=ses_33559..._w6, message=FANOUT w5: 15, nudge=true]
⚙ xmpp-bridge_send_message [to=ses_33559..._w6, message=CHAIN ACK: w6→w1→w4→w6 OK, nudge=true]
```

w5 screen window — zobrazoval starší PARALLEL TEST zprávy (z předchozího testu):
```
⚙ xmpp-bridge_send_message [to=ses_33559..._w6, message=w4 result: 3628800, nudge=true]
⚙ xmpp-bridge_send_message [to=ses_33559..._w6, message=FANOUT w5: 15, nudge=true]
```

---

## Poznámky

- Odpovědi agentů přicházejí **přes XMPP notify** (viditelné v chatu), ne vždy
  přes `receive_messages` inbox — závisí na tom jak agent provede send_message.
- Instrukce v jedné zprávě musí být **jednoznačné** — session ID uvést explicitně,
  očekávaný formát odpovědi specifikovat přesně.
- `nudge=true` je preferovaný mód — nevyrušuje agenta při práci, zpráva se
  doručí při příštím idle cyklu (typicky do 15s).
- Plugin verze `0.7.5` je kompatibilní s bridge `0.7.8`.

## Známé problémy

### w4/w5 session identity mismatch

`ses_332172680ffe..._w4` a `ses_332172680ffe..._w5` sdílejí stejný prefix session ID
(stejný `sty`, stejný projekt `~/claude-home`). Agent v screen window 4 zpracovává
zprávy určené w4, ale identifikuje se jako w5 — pravděpodobně proto, že plugin
zaregistroval session pod jiným window číslem než je aktuální screen window.

Zprávy jsou doručeny správně (podle session ID, ne window čísla), ale agent
si neuvědomuje své skutečné window číslo. Nefunkční je pouze self-identifikace.

Dopad na testy: **žádný** — výsledky jsou správné, jen hlášení agenta je matoucí.
