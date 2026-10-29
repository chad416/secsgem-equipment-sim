# gemsim design notes

This document explains how the simulator is put together and why. The README
covers what it does and how to run it.

## 1. Goals and non-goals

Goals

* Model the software split found on real process tools: a machine controller
  (PLC) owns the sequence, interlocks and I/O; an equipment PC owns the SECS/GEM
  interface to the factory host. The two talk over an industrial protocol.
* Implement a coherent subset of SEMI E5 (SECS-II), E37/E37.1 (HSMS-SS) and E30
  (GEM) well enough that a scripted host can run a complete lot, including a
  fault and its recovery, and every exchange can be read in an SML trace.
* Make behaviour testable and deterministic where it matters (state machines,
  acknowledge codes, controller sequence).

Non-goals

* Certification or claim of compliance. No GEM compliance statement in the
  E30 sense is made. The table in the README lists what is implemented.
* Carrier, substrate and job management (E87, E90, E40, E94), spooling,
  traces (S2F23), limits monitoring and terminal services (S10).
* Real process physics. The chamber model is first order and only exists to
  produce plausible, time-varying values and failure modes.

## 2. Components

```
 Factory host                    Equipment PC                          Machine controller (PLC)
 ------------                    ------------                          ------------------------
 GemHost                         GemEquipment                           ChamberController
  - scenario / shell              - HSMS passive server                  - scan(dt): sequencer,
  - SML trace          HSMS/TCP   - comm + control state      OPC UA     physics, interlocks
  - S6F11/S5F1 sink  <=========>  - variables, reports, alarms <=======> - alarm word (latched)
                      port 5000   - remote commands, PP store   4840     - event ring buffer
                                  - ControllerLink (client)              ControllerServer
                                                                          - address space Chamber1.*
                                                                          - command handshake
```

| Module | Role |
|---|---|
| `gemsim.secs` | SECS-II item codec (`L`, `A`, `B`, `BOOLEAN`, `I1..I8`, `U1..U8`, `F4`, `F8`), SML rendering |
| `gemsim.hsms` | HSMS-SS framing, connection state machine, T3/T5/T6/T7/T8, passive/active endpoints |
| `gemsim.controller` | interface contract, controller model, scan loop, OPC UA server, GEM-side links |
| `gemsim.gem` | E30 state models, variables, reports, alarms, remote commands, process programs, `GemEquipment` |
| `gemsim.host` | scriptable host and the lot scenario |
| `gemsim.trace` | SML trace hook used by the host (and usable on the equipment side) |
| `gemsim.demo` | wires all three together for `make demo` |
| `gemsim.coverage` | message coverage table generated from the equipment's dispatch table |

The GEM layer never imports the controller model (a test checks this). It depends on the abstract
`ControllerLink`, which has two implementations:

* `OpcUaLink`: the real path (asyncua client, subscription + handshake,
  reconnect with backoff, section 3.5).
* `DirectLink`: the same controller model in-process, no sockets. Used by
  GEM unit tests so that report and HCACK tests run in milliseconds and do not
  depend on OPC UA start-up time.

## 3. PLC ↔ GEM interface contract

`gemsim/controller/interface.py` is the single source of truth for both sides,
in the same role an interface specification has between a controls team and an
equipment-software team.

### 3.1 Address space

All nodes live under the object `Chamber1` in namespace
`urn:gemsim:chamber-controller` with string node ids `ns=<idx>;s=Chamber1.<path>`.

| Group | Tags | Direction |
|---|---|---|
| Sequencer | `Seq.State`, `Seq.HeldReason`, `Seq.Aborting`, `Seq.WaferNo`, `Seq.StepNo`, `Seq.StepName`, `Seq.StepTimeLeft` | PLC → GEM |
| Chamber | `Chamber.Pressure` [Torr], `Chamber.PressureSP`, `Chamber.Temperature` [°C], `Chamber.TemperatureSP`, `Chamber.WaferPresent` | PLC → GEM |
| Robot / lot | `Robot.Busy`, `Lot.Id`, `Lot.WaferCount`, `Lot.WafersDone`, `Recipe.Loaded` | PLC → GEM |
| Alarms | `Alarms.Word` (UInt32, latched bits) | PLC → GEM |
| Events | `Events.Code[64]`, `Events.Arg[64]`, `Events.WriteIdx` | PLC → GEM |
| Watchdog | `Heartbeat` (incremented every scan) | PLC → GEM |
| Command | `Cmd.Code`, `Cmd.StrArg`, `Cmd.IntArg`, `Cmd.ReqSeq` → `Cmd.AckSeq`, `Cmd.Result` | handshake |
| Recipe download | `RecipeIn.Name`, `RecipeIn.StepNames[]`, `RecipeIn.TempSP[]`, `RecipeIn.PressureSP[]`, `RecipeIn.Duration[]` | GEM → PLC |
| Parameters | `Param.PumpDownTimeout`, `Param.BasePressure`, `Param.TempHighLimit`, `Param.RobotTimeout` | GEM → PLC |
| Simulation | `Sim.Faults` (bit mask, maintenance client only), `Sim.TimeScale` | external |

### 3.2 Command handshake

```
GEM layer (OpcUaLink.command)                 controller (next scan, read_inputs)
-----------------------------                 -----------------------------------
write Cmd.Code, Cmd.StrArg, Cmd.IntArg
write Cmd.ReqSeq = n                ------>   ReqSeq changed -> execute command
                                              write Cmd.Result
wait for Cmd.AckSeq == n (3 s)      <------   write Cmd.AckSeq = n
read Cmd.Result
```

One command is in flight at a time (a lock in the link). If the controller does
not acknowledge within 3 s the result is `NO_RESPONSE`, which the GEM layer
reports to the host as HCACK 2. This is the same request/acknowledge-counter
pattern commonly used for PLC command interfaces (for example DB-based
handshakes on S7 controllers), chosen over OPC UA methods because it maps onto
any PLC that can expose plain tags.

### 3.3 Why an event ring and not state polling

The first idea was to derive collection events from `Seq.State` data changes.
That loses events: an OPC UA subscription reports the latest value per
publishing interval, so a state that lasts less than one interval (for example
`STEP_COMPLETED` immediately followed by `STEP_STARTED`) is never seen. The
controller therefore writes every event into a 64-entry ring
(`Events.Code[]`, `Events.Arg[]`) and then increments `Events.WriteIdx`. The
GEM side subscribes to the index only and, on change, reads the ring and
replays every entry between its last index and the new one, in order. The ring
is written before the index in the same scan, so a reader that sees the new
index also sees the data. An overrun (more than 64 events between two reads) is
detected and logged.

Status values (pressure, temperature) are still sampled through the
subscription. A report attached to an event therefore contains status values
that may be up to one publishing interval (50 ms) old; event-specific data
(wafer number, step number) travels in the ring and is exact.

### 3.4 Alarms

The controller latches alarm bits in `Alarms.Word`. The GEM layer detects
edges on the word and maps bit → ALID. Latching matters: a fault that appears
and disappears within one publishing interval would otherwise never be seen by
the host. Latched alarms are cleared by the host's RESUME (retry) or ABORT,
which is the controller's acknowledge.

The GEM layer adds one alarm of its own, ALID 10 *controller link lost*, set
when the link is down (section 3.5) or when `Heartbeat` has not changed for
`watchdog_s` (3 s by default; covers a controller whose scan has stopped while
its OPC UA server still answers). It clears when both conditions are gone.

### 3.5 Link loss and reconnect

`OpcUaLink` reads `Heartbeat` once a second as a health check. A failed read,
or a bad status reported by the subscription, marks the link down: ALID 10 is
set, S2F41 answers HCACK 2, and S2F15 answers EAC 2 for constants that map to
controller parameters. A command in flight when the session drops returns
`NO_RESPONSE` (HCACK 2) instead of raising.

While down, the link reconnects with exponential backoff from 0.5 s to 10 s.
On reconnect it reads the full status image first: the alarm word goes through
the normal edge detection, so an alarm latched during the outage is reported.
If the event write index moved forward, the missed ring entries are replayed
(at most 64, an overrun is logged). If it went back, the controller restarted:
the index is resynchronised and a warning says that events from the outage may
be missing. `test_e2e.py` stops the controller, checks ALID 10 and HCACK 2,
starts a new controller on the same endpoint and checks that ALID 10 clears and
a recipe can be selected again.

## 4. State models

### 4.1 HSMS connection (E37)

```
NOT CONNECTED --TCP accept/connect--> NOT SELECTED --Select.req/rsp(0)--> SELECTED
      ^                                  |      ^                            |
      |                       T7 expiry  |      +----- Deselect.req/rsp -----+
      +------- Separate.req, T6 expiry, T8 expiry, TCP loss -----------------+
```

| Timer | Default | Implemented as |
|---|---|---|
| T3 reply | 45 s | `request()` waits on a future per system bytes; expiry raises `T3Timeout`; equipment then sends S9F9 with the header of the unanswered message |
| T5 connect separation | 10 s | `connect_active()` sleeps T5 between TCP connect attempts |
| T6 control transaction | 5 s | Select/Deselect/Linktest response wait; expiry closes the TCP connection (communication failure) |
| T7 not selected | 10 s | passive side starts T7 on accept and after Deselect; closes if not SELECTED in time |
| T8 inter-character | 5 s | once the first byte of a message has arrived, each further TCP chunk must arrive within T8 |

T8 is checked per received chunk, not per byte. A stream socket does not expose
byte timing, so chunk granularity is the closest available.

HSMS-SS (E37.1) ends a session with Separate.req; the host does that. The
passive side still answers Deselect.req for hosts that send it.

Other E37 behaviour: data messages before SELECTED get Reject.req reason 4;
unknown SType reason 1; PType ≠ 0 reason 2; a response control message with
no open transaction reason 3. A second TCP connection while a session is
active is answered with Select.rsp status 3 (connection exhaust) and closed.
Select.req on an already selected connection gets status 1. If handling a
received frame raises unexpectedly, the reader logs it and closes the
connection rather than leaving a session that no longer reads.

A detail that showed up in testing: the active side must switch to SELECTED
in its reader, the moment Select.rsp arrives, not later in the coroutine that
waits for it. The equipment sends S1F13 immediately after Select.rsp, and the
host's reader reached that S1F13 before the waiting coroutine ran, so the host
rejected it with reason 4.

### 4.2 Communication state (E30 4.2.1)

On SELECTED the equipment enters NOT COMMUNICATING / WAIT CRA and sends S1F13.
COMMACK 0 → COMMUNICATING. A failed attempt (T3 or COMMACK ≠ 0) → WAIT DELAY
for EstablishCommunicationsTimeout (EC 3001), then back to WAIT CRA with a new
S1F13. A host S1F13 received in any state is answered with COMMACK 0 and also
establishes communication.
Losing the HSMS session returns to WAIT CRA. DISABLED is not modelled.

While not communicating, any primary other than S1F13 is answered with SxF0.
E30 lets the equipment ignore such messages; answering SxF0 was chosen so the
host is not left waiting for T3.

### 4.3 Control state (E30 4.2.2)

```
                     +---------------------------- OFF-LINE -----------------------------+
   start ----------> | EQUIPMENT OFF-LINE --(4) operator ON-LINE--> ATTEMPT ON-LINE        |
                     |        ^                                  (5) S1F0 / T3 |          |
                     |        +---- (10) operator OFF-LINE ---- HOST OFF-LINE <-+          |
                     +-----------------------------------------------^-------|------------+
                         (7) operator OFF-LINE      (8) S1F15         |       | (9) S1F17
                     +---------------------------- ON-LINE ----------|-------v------------+
   (6) S1F2 -------> |         LOCAL  <------ (11) operator switch ------>  REMOTE         |
                     +--------------------------------------------------------------------+
```

| # | From | Trigger | To | Notes |
|---|---|---|---|---|
| 1 | - | start-up | EQUIPMENT OFF-LINE or ON-LINE | `EquipmentConfig.initial_online` |
| 4 | EQUIPMENT OFF-LINE | operator ON-LINE | ATTEMPT ON-LINE | equipment sends S1F1 |
| 5 | ATTEMPT ON-LINE | S1F0, T3, or not communicating | HOST OFF-LINE (configurable: EQUIPMENT OFF-LINE) | |
| 6 | ATTEMPT ON-LINE | S1F2 | ON-LINE LOCAL/REMOTE | substate from the LOCAL/REMOTE switch |
| 7 | ON-LINE | operator OFF-LINE | EQUIPMENT OFF-LINE | CE 100 |
| 8 | ON-LINE | S1F15 | HOST OFF-LINE | OFLACK 0, CE 100 |
| 9 | HOST OFF-LINE | S1F17 | ON-LINE | ONLACK 0, CE 101/102 |
| - | EQUIPMENT OFF-LINE | S1F17 | (no change) | ONLACK 1 |
| - | ON-LINE | S1F17 | (no change) | ONLACK 2 |
| 10 | HOST OFF-LINE | operator OFF-LINE | EQUIPMENT OFF-LINE | |
| 11 | LOCAL ↔ REMOTE | operator switch | REMOTE ↔ LOCAL | CE 101/102; position remembered while off-line |

Off-line, the equipment answers every primary except S1F13 and S1F17 with SxF0
and sends no S6F11/S5F1. CE 100 *EquipmentOffline* is the one exception: it
is queued as the last message while going off-line.

### 4.4 Processing state (equipment specific)

E30 leaves the processing state model to the equipment. This tool uses four
states, driven by controller events (from the ring, so no transition is lost):

```
          PP-SELECT ok            START accepted (LOT_STARTED)
  IDLE ---------------> READY ----------------------------------> EXECUTING
   ^                      ^   LOT_COMPLETED / STOPPED / ABORTED       |  ^
   |  PP deleted          +-------------------------------------------+  | RESUMED
   +----------------------                                    HELD       |
                                                     EXECUTING -----> PAUSE
```

| ProcessState (SV 1008) | Meaning |
|---|---|
| 1 IDLE | no process program selected |
| 2 READY | PP selected and loaded into the controller, sequencer idle |
| 3 EXECUTING | lot running (any of LOADING, PUMPDOWN, PROCESSING, VENTING, UNLOADING) |
| 4 PAUSE | sequencer HELD, either by PAUSE or by a fault (`HeldReason` DV says which) |

Every change raises CE 110 *ProcessingStateChange*; SV 1008 and SV 1009
(previous state) are read at event time.

### 4.5 Controller sequencer

```
 IDLE --START--> LOADING --robot done--> PUMPDOWN --p <= base--> PROCESSING (step 1..n)
   ^                                                                   | last step done
   |                                                                   v
   +--- last wafer / STOP <-- UNLOADING <--robot done-- VENTING <------+
              (next wafer: back to LOADING)

 any active state --fault or PAUSE--> HELD --RESUME--> state it was held in (phase timer restarted)
 ABORT: PUMPDOWN/PROCESSING/VENTING -> VENTING -> UNLOADING -> IDLE (LOT_ABORTED)
        LOADING before the wafer is placed -> IDLE (LOT_ABORTED)
```

* PAUSE during a robot move is deferred until the move completes; a robot
  move is never interrupted.
* PAUSE in PROCESSING freezes the step timer; setpoints are held.
* RESUME after a fault clears the latched alarms and restarts the phase that
  failed (for example the pump-down timer). If the cause is still present the
  alarm comes back after the timeout.
* RESUME is refused (INTERLOCK → HCACK 2) while the chuck is within 20 °C of
  the over-temperature limit. An ABORT keeps OVER_TEMPERATURE latched while the
  chuck is that hot, and START is interlocked while any alarm is latched. In
  IDLE, RESUME acts as the alarm reset. This is the one place where RESUME
  means "reset" rather than "continue"; a dedicated RESET command would be
  cleaner on a real tool.

### 4.6 Time

`ScanLoop` runs `scan(dt)` every 50 ms of wall time (10 ms for `DirectLink`),
with `dt = elapsed × time_scale`. The demo uses ×25, so a 60 s anneal step
takes 2.4 s. The physics use exact discrete solutions
(`p ← p_floor + (p − p_floor)·e^(−dt/τ)`), which stay stable for any `dt`. `dt`
is capped at five scan periods so a stalled event loop cannot make the process
jump ahead. Elapsed time is measured with `time.perf_counter()`: on Windows,
`time.monotonic()` advances in ~15.6 ms steps, which is a third of a scan.

## 5. Identifiers

### 5.1 Variables (one VID space)

| VID | Name | Kind | Format | Source |
|---|---|---|---|---|
| 1001 | Clock | SV | A[12/16] | PC clock + S2F31 offset, TimeFormat EC |
| 1002 / 1003 | ControlState / PreviousControlState | SV | U1 | GEM |
| 1004 / 1005 / 1006 | EventsEnabled / AlarmsEnabled / AlarmsSet | SV | L of U4 | GEM |
| 1007 | PPExecName | SV | A | GEM |
| 1008 / 1009 | ProcessState / PreviousProcessState | SV | U1 | GEM |
| 1020 / 1021 | ChamberPressure / ChamberPressureSP | SV | F4 Torr | PLC |
| 1022 / 1023 | ChuckTemperature / ChuckTemperatureSP | SV | F4 °C | PLC |
| 1024 / 1025 | WaferPresent / RobotBusy | SV | BOOLEAN | PLC |
| 1026 | SequencerState | SV | U1 | PLC |
| 1027 / 1028 / 1029 | StepNumber / StepName / StepTimeLeft | SV | U4 / A / F4 s | PLC |
| 1030 to 1033 | LotID, LotWaferCount, WafersProcessed, WaferNumber | SV | A / U4 | PLC |
| 1040 | ControllerLinkUp | SV | BOOLEAN | GEM |
| 1041 / 1042 | MDLN / SOFTREV | SV | A | GEM |
| 2001 | EventWaferNumber | DV | U4 | event arg |
| 2002 / 2003 | EventStepNumber / EventStepName | DV | U4 / A | event arg + selected recipe |
| 2004 / 2005 | AlarmID / AlarmText | DV | U4 / A | alarm edge |
| 2006 | LotResultWafers | DV | U4 | lot end event arg |
| 2007 | HeldReason | DV | U1 | 1 paused, 2 fault |
| 2008 | PPSelected | DV | A | PP-SELECT |
| 3001 | EstablishCommunicationsTimeout | EC | U2 s, 1..120, 10 | GEM |
| 3002 | TimeFormat | EC | U1, 0..1, 1 | GEM |
| 3003 | PumpDownTimeout | EC | F4 s, 10..600, 90 | → `Param.PumpDownTimeout` |
| 3004 | BasePressure | EC | F4 Torr, 0.01..1, 0.05 | → `Param.BasePressure` |
| 3005 | TempHighLimit | EC | F4 °C, 100..500, 450 | → `Param.TempHighLimit` |
| 3006 | RobotTimeout | EC | F4 s, 5..120, 20 | → `Param.RobotTimeout` |

A DV that is not valid for the event being reported is sent as a zero-length
item of its format (for example `<U4 [0]>`).

ECs that map to controller parameters are written to the PLC by S2F15 and
can only be changed while the sequencer is IDLE (EAC 2 otherwise). S2F15 is
all-or-nothing.

### 5.2 Collection events

| CEID | Name | DVs |
|---|---|---|
| 100 / 101 / 102 | EquipmentOffline / ControlStateLocal / ControlStateRemote | - |
| 110 | ProcessingStateChange | - (SV 1008/1009) |
| 120 | PPSelected | 2008 |
| 130 / 131 / 132 / 133 | LotStarted / LotCompleted / LotStopped / LotAborted | 2006 (end events) |
| 140 / 141 | WaferLoaded / PumpDownComplete | 2001 |
| 142 / 143 | StepStarted / StepCompleted | 2002, 2003 |
| 144 / 145 / 146 | WaferProcessed / VentComplete / WaferUnloaded | 2001 |
| 150 / 151 | AlarmSet / AlarmCleared | 2004, 2005 |
| 160 / 161 | ProcessHeld / ProcessResumed | 2007 (held) |

### 5.3 Alarms

| ALID | Text (ALTX) | ALCD category | Source |
|---|---|---|---|
| 1 | Pump-down did not reach base pressure | 4 parameter control error | PLC bit 0 |
| 2 | Wafer handling robot move timeout | 6 equipment status warning | PLC bit 1 |
| 3 | Chuck over-temperature, heater tripped | 2 equipment safety | PLC bit 2 |
| 10 | Machine controller link lost | 8 data integrity | GEM watchdog |

ALCD = `0x80 | category` when set, `category` when cleared. Each edge sends
S5F1 (if the alarm is enabled with S5F3 and the tool is on-line) and raises
CE 150/151 (subject only to S2F37). Disabling an alarm does not stop it from
being tracked: S5F5 and SV 1006 still show it.

## 6. Remote commands (S2F41)

Checks run in this order; the first failure decides the answer:

1. RCMD unknown → HCACK 1.
2. Parameter checks, all parameters reported together → HCACK 3 with
   CPACK 1 (name unknown), 2 (illegal value) or 3 (illegal format).
   A missing required parameter is reported as its CPNAME with CPACK 2.
   E5 has no CPACK for "missing"; this choice is documented, not standard.
3. Not ON-LINE REMOTE → HCACK 2.
4. Controller link down → HCACK 2.
5. Command-specific checks, then the controller handshake.

| RCMD | Parameters | Result |
|---|---|---|
| PP-SELECT | PPID (A, required) | unknown PPID → 6; sequencer not idle → 2; body fails to parse or controller rejects a setpoint → 3 / CPACK 2; ok → 0 + CE 120 |
| START | LOTID (A ≤ 40, required), WAFERS (1..25 as U* or ASCII digits, default 1) | no PP selected → 2; already running → 5; interlock (wafer present, alarm latched) → 2; ok → 4 |
| STOP | - | idle → 2; already requested → 5; ok → 4 (lot ends after the current wafer, CE 132) |
| PAUSE | - | idle → 2; already held → 5; ok → 4 (CE 160) |
| RESUME | - | not held → 5 (2 when idle with no alarm); interlock → 2; ok → 4 (CE 161) |
| ABORT | - | idle → 2; already aborting → 5; ok → 4 (CE 133) |

Accepted sequencer commands answer HCACK 4 ("acknowledged, completion
signalled later") because completion is reported by collection events.

## 7. Process programs (stream 7)

Unformatted text recipes, one step per line
(`STEP <name> TEMP=<°C> PRESSURE=<Torr> TIME=<s>`). Bodies are stored verbatim
(A or B accepted in S7F3, and S7F6 returns the body in the same format) and
validated on PP-SELECT, the point where they are
turned into controller setpoints. The selected PP is locked while a lot runs:
S7F1 answers PPGNT 4 (busy), S7F3 and S7F17 answer ACKC7 1. Limits: 32
programs, 64 KiB per body. The store is in memory.

## 8. Error handling summary

| Situation | Equipment response |
|---|---|
| Device ID ≠ configured session ID | S9F1 (MHEAD = received header) |
| Unknown stream | S9F3 |
| Unknown function in a known stream | S9F5 |
| Text not decodable, or wrong structure for the function | S9F7 |
| Host does not answer an equipment primary within T3 | S9F9 (SHEAD = header of the unanswered message) |
| Off-line, any primary except S1F13/S1F17 | SxF0 |
| HSMS session lost while S6F11/S5F1 are queued | discarded and logged (no spooling) |
| Controller link down: S2F41 / S2F15 with machine constants | HCACK 2 / EAC 2, ALID 10 set |

## 9. Decisions and alternatives

| Decision | Alternative considered | Why |
|---|---|---|
| Own asyncio HSMS/SECS-II/GEM stack | `secsgem` library | Full control of timer behaviour, state machines and acknowledge codes, which are the point of the exercise. `secsgem` is used in the tests as an independent oracle: byte-level comparisons of item and header encodings catch codec mistakes that self-round-trip tests cannot. |
| OPC UA between GEM and controller | Modbus TCP | OPC UA carries typed values, strings and arrays (recipe download, event ring) without a hand-made register map, and is what current PLCs and equipment PCs commonly use. Modbus would need string/float packing and a register map; the `ControllerLink` interface keeps that swap local. |
| Tag handshake for commands | OPC UA methods | Works with any PLC that exposes tags; matches how PLC command interfaces are usually built. |
| Event ring buffer | deriving events from state changes | State sampling loses short-lived states (section 3.3). |
| Latched alarm word | live alarm bits | Short faults are not lost; host acknowledgement is explicit. |
| One serial sender for S6F11/S5F1 | concurrent sends | Keeps event order on the wire equal to the order of occurrence; each message waits for its reply before the next is sent. |
| Validate recipes at PP-SELECT | at download | ACKC7 has no code for "body invalid"; PP-SELECT can report CPACK 2 against PPID. |
| Answer SxF0 while not communicating | silently discard | Host does not stall on T3. |

## 10. Testing approach

* Codec: round trips, malformed input, and byte equality against `secsgem`.
* HSMS: every timer and reject path against real localhost sockets with short
  timers (`t3 = 0.5 s`, `t7 = 0.5 s`, ...).
* Controller: deterministic, `scan(0.5)` driven directly, no event loop.
* GEM: `DirectLink` bench (equipment + host over real HSMS, controller in-process
  at ×100) for reports, HCACK/CPACK, alarms, PP management, stream 9.
* End to end: controller OPC UA server + `OpcUaLink` + HSMS + host in one
  process, running the same scenario as `make demo`, plus a controller loss
  and reconnect test.
* Coverage: `docs/message_coverage.md` is generated from the dispatch table
  and a test fails when the two disagree.

Waits in tests use wall-clock deadlines. An earlier version counted iterations
of `asyncio.sleep(0.01)` and failed intermittently: on Windows the real length
of a short sleep depends on the system timer resolution, which other processes
change, so "1000 iterations" was sometimes well under a second. Timing-sensitive
assertions that cannot be made deterministic at ×100 (for example "chuck still
hot after the trip") are tested in the controller unit tests instead.
