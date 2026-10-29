# Trace walkthrough

Excerpts copied unchanged from [`artifacts/demo_trace.sml`](../artifacts/demo_trace.sml),
the host-side SML trace of the committed demo run (1474 lines, 155 messages).
`H -> E` is host to equipment, `SB` the HSMS system bytes. Lines starting with
`// ----` are annotations written by the scenario. Equipment-initiated
transactions use system bytes from 0x00010001 up, host-initiated ones from 1 up.

## Start-up

The equipment sends S1F13 right after Select.rsp, the host sends its own S1F13
as well, and both get COMMACK 0. The tool starts in EQUIPMENT OFF-LINE, so it
answers the host's S1F1 with S1F0. The operator's ON-LINE switch makes the
equipment send S1F1 (ATTEMPT ON-LINE), and the host's S1F2 takes it ON-LINE.

```text
// ---- 1. HSMS connect + Select, establish communications (S1F13)
14:20:56.170  H -> E  Select.req                   SB=0x00000001
14:20:56.171  E -> H  Select.rsp status=0          SB=0x00000001
14:20:56.171  E -> H  S1F13 W                      SB=0x00010001
  <L [2]
    <A "GEMSIM-PC1">
    <A "0.1.0">
  >
.
14:20:56.171  H -> E  S1F14                        SB=0x00010001
  <L [2]
    <B [1] 0x00>
    <L [0]>
  >
.
14:20:56.171  H -> E  S1F13 W                      SB=0x00000002
  <L [0]>
.
14:20:56.171  E -> H  S1F14                        SB=0x00000002
  <L [2]
    <B [1] 0x00>
    <L [2]
      <A "GEMSIM-PC1">
      <A "0.1.0">
    >
  >
.

// ---- 2. S1F1 while the tool is still OFF-LINE -> equipment must abort (S1F0)
14:20:56.171  H -> E  S1F1 W                       SB=0x00000003
.
14:20:56.171  E -> H  S1F0                         SB=0x00000003
.

// ---- 3. operator turns the ON-LINE switch -> equipment sends S1F1, host answers S1F2
14:20:56.172  E -> H  S1F1 W                       SB=0x00010002
.
14:20:56.172  H -> E  S1F2                         SB=0x00010002
  <L [0]>
.
```

## Fault and recovery

A maintenance client sets `Sim.Faults = VACUUM_LEAK` when wafer 2 is loaded.
The pump-down stalls at the 2 Torr leak floor. After `PumpDownTimeout` (set to
60 s with S2F15 earlier in the run) the controller latches the alarm bit and
holds. The GEM layer sends S5F1 with ALCD `0x84` (bit 8 set = alarm on,
category 4 = parameter control error), then CE 150 AlarmSet, CE 110
ProcessingStateChange (EXECUTING 3 -> PAUSE 4) and CE 160 ProcessHeld
(HeldReason 2 = fault). Each S6F11 waits for its S6F12 before the next is sent.

The host reads the pressure, the leak is fixed, and the host sends RESUME. The
controller clears the latched alarm and restarts the pump-down: S5F1 with ALCD
`0x04` (cleared), CE 151, CE 110 back to EXECUTING, CE 161, then
PumpDownComplete (CE 141), where the excerpt ends.

The alarm-cleared S5F1 arrives before the S2F42 that answers RESUME. The
controller acts in its next scan while the command acknowledge is still on its
way back through the OPC UA handshake, so a host must not assume that a reply
comes before the events it causes. In CE 141, report 3 carries `<U4 [0]>` and
`<A "">`: the step DVs are not valid for that event, so they are sent as
zero-length items rather than stale values. HeldReason in CE 161 is `<U1 [0]>`
for the same reason.

```text
// ---- 12. [maintenance] vacuum leak injected on wafer 2; waiting for the alarm
14:21:06.028  E -> H  S5F1 W                       SB=0x00010014
  <L [3]
    <B [1] 0x84>
    <U4 [1] 1>
    <A "Pump-down did not reach base pressure">
  >
.
14:21:06.029  H -> E  S5F2                         SB=0x00010014
  <B [1] 0x00>
.
14:21:06.032  E -> H  S6F11 W                      SB=0x00010015
  <L [3]
    <U4 [1] 18>
    <U4 [1] 150>
    <L [1]
      <L [2]
        <U4 [1] 5>
        <L [4]
          <U4 [1] 1>
          <A "Pump-down did not reach base pressure">
          <F4 [1] 2>
          <F4 [1] 25>
        >
      >
    >
  >
.
14:21:06.032  H -> E  S6F12                        SB=0x00010015
  <B [1] 0x00>
.
14:21:06.034  E -> H  S6F11 W                      SB=0x00010016
  <L [3]
    <U4 [1] 19>
    <U4 [1] 110>
    <L [1]
      <L [2]
        <U4 [1] 1>
        <L [4]
          <U1 [1] 5>
          <U1 [1] 4>
          <U1 [1] 3>
          <A "ANNEAL_STD">
        >
      >
    >
  >
.
14:21:06.035  H -> E  S6F12                        SB=0x00010016
  <B [1] 0x00>
.
14:21:06.037  E -> H  S6F11 W                      SB=0x00010017
  <L [3]
    <U4 [1] 20>
    <U4 [1] 160>
    <L [1]
      <L [2]
        <U4 [1] 6>
        <L [3]
          <U1 [1] 2>
          <U1 [1] 6>
          <U4 [1] 2>
        >
      >
    >
  >
.
14:21:06.037  H -> E  S6F12                        SB=0x00010017
  <B [1] 0x00>
.

// ---- 13. host reads chamber pressure while HELD, technician fixes the leak
14:21:06.038  H -> E  S1F3 W                       SB=0x00000017
  <L [3]
    <U4 [1] 1020>
    <U4 [1] 1006>
    <U4 [1] 1008>
  >
.
14:21:06.040  E -> H  S1F4                         SB=0x00000017
  <L [3]
    <F4 [1] 2>
    <L [1]
      <U4 [1] 1>
    >
    <U1 [1] 4>
  >
.

// ---- 14. RESUME -> controller clears the latched alarm and retries the pump-down
14:21:06.044  H -> E  S2F41 W                      SB=0x00000018
  <L [2]
    <A "RESUME">
    <L [0]>
  >
.
14:21:06.123  E -> H  S5F1 W                       SB=0x00010018
  <L [3]
    <B [1] 0x04>
    <U4 [1] 1>
    <A "Pump-down did not reach base pressure">
  >
.
14:21:06.123  H -> E  S5F2                         SB=0x00010018
  <B [1] 0x00>
.
14:21:06.126  E -> H  S2F42                        SB=0x00000018
  <L [2]
    <B [1] 0x04>
    <L [0]>
  >
.
14:21:06.127  E -> H  S6F11 W                      SB=0x00010019
  <L [3]
    <U4 [1] 21>
    <U4 [1] 151>
    <L [1]
      <L [2]
        <U4 [1] 5>
        <L [4]
          <U4 [1] 1>
          <A "Pump-down did not reach base pressure">
          <F4 [1] 1.34671>
          <F4 [1] 25>
        >
      >
    >
  >
.
14:21:06.128  H -> E  S6F12                        SB=0x00010019
  <B [1] 0x00>
.
14:21:06.131  E -> H  S6F11 W                      SB=0x0001001A
  <L [3]
    <U4 [1] 22>
    <U4 [1] 110>
    <L [1]
      <L [2]
        <U4 [1] 1>
        <L [4]
          <U1 [1] 5>
          <U1 [1] 3>
          <U1 [1] 4>
          <A "ANNEAL_STD">
        >
      >
    >
  >
.
14:21:06.131  H -> E  S6F12                        SB=0x0001001A
  <B [1] 0x00>
.
14:21:06.132  E -> H  S6F11 W                      SB=0x0001001B
  <L [3]
    <U4 [1] 23>
    <U4 [1] 161>
    <L [1]
      <L [2]
        <U4 [1] 6>
        <L [3]
          <U1 [0]>
          <U1 [1] 2>
          <U4 [1] 2>
        >
      >
    >
  >
.
14:21:06.133  H -> E  S6F12                        SB=0x0001001B
  <B [1] 0x00>
.
14:21:06.570  E -> H  S6F11 W                      SB=0x0001001C
  <L [3]
    <U4 [1] 24>
    <U4 [1] 141>
    <L [1]
      <L [2]
        <U4 [1] 3>
        <L [4]
          <U4 [0]>
          <A "">
          <F4 [1] 0.0368095>
          <F4 [1] 25>
        >
      >
    >
  >
.
14:21:06.572  H -> E  S6F12                        SB=0x0001001C
  <B [1] 0x00>
.
```

## Shutdown

The host takes the tool off-line with S1F15, waits for CE 100
EquipmentOffline, and ends the session with Separate.req. Separating straight
after S1F16 would cut off that last S6F11.

```text
// ---- 16. host takes the tool off-line (S1F15), waits for CE EquipmentOffline, then Separate
14:21:19.328  H -> E  S1F15 W                      SB=0x0000001A
.
14:21:19.328  E -> H  S1F16                        SB=0x0000001A
  <B [1] 0x00>
.
14:21:19.328  E -> H  S6F11 W                      SB=0x00010033
  <L [3]
    <U4 [1] 47>
    <U4 [1] 100>
    <L [1]
      <L [2]
        <U4 [1] 1>
        <L [4]
          <U1 [1] 3>
          <U1 [1] 2>
          <U1 [1] 3>
          <A "ANNEAL_STD">
        >
      >
    >
  >
.
14:21:19.329  H -> E  S6F12                        SB=0x00010033
  <B [1] 0x00>
.
14:21:19.329  H -> E  Separate.req                 SB=0x0000001B
```
