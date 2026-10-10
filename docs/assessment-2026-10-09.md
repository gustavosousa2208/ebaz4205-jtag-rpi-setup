# Technical Assessment — PiProbe architecture proposal

- Date: 2026-10-09
- Scope: 0 — requirements quality
- Artefacts: client-agent proposal pasted in chat; PiProbe Beads `ebaz4205-3pb`, `.4`, `.6`; discovery and XVC design docs and implementation listed below. No silicon-specific conformance assessment was requested.
- Intent: let agents on the local network use SBC-hosted debug hardware through familiar tools, starting with OpenOCD/JTAG on the EBAZ4205. Future targets/protocols include NXP/STM32 over SWD, RTT, SWO/trace, and later `lstty` discovery/control.
- Optimize for: safety and recoverability first, reuse of proven tools second, then a simple agent-facing workflow.
- Verdict: CONDITIONAL. The proposal has solid architecture instincts, but the generic core, declarative safety language, and reconnect behavior need tighter boundaries and testable contracts before they become implementation requirements.

## PROJECT_INDEX

```text
intent:
  - Same-LAN agent can discover and use a network Pi probe like a local JTAG probe.
  - First vertical slice is the existing EBAZ4205/Zynq JTAG path through OpenOCD.
  - Future scope includes SWD/RTT/SWO/trace and an optional lstty-facing client.
  - Optimize for safety/recovery, existing-tool reuse, and low-friction use.
requirements:
  - REQ-01..REQ-07: seven claims/cautions in the client-agent proposal (chat).
targets:
  - Current: EBAZ4205 Zynq PL + ARM TAPs, JTAG through Banana Pi XVC.
  - Candidate future examples: NXP or STM32 MCU over SWD; exact board/MCU not named.
code_docs_hw:
  - piprobe/discover.py, piprobe/announce.py: discovery and announcement only.
  - jtag/xvc/xvc-server.c: XVC 1.0 JTAG server on Banana Pi GPIO.
  - jtag/ebaz4205.cfg, jtag/openocd-scripts/target/zynq_7000.cfg: target setup.
  - jtag/xvc/README.md, docs/piprobe-discovery.md: transport and security contract.
  - docs/probe-hardening.md, jtag/README.md: existing target-specific safety and workflow constraints.
silicon_docs:
  - Not needed for this requirements-quality scope; no register-level claims are being assessed.
missing:
  - Exact NXP/STM32 target boards, debug adapters, and required host tools.
  - Defined remote-job/reconnect contract and a typed safety-profile schema.
```

## Scope 0 — Requirements quality

| REQ-ID | Defect | Why it blocks | Proposed fix |
|---|---|---|---|
| REQ-01 Generic core and per-target profiles | “Core” owns many unrelated behaviors (locking, remote job lifetime, console capture, results, exit codes, logs), without defining its API, process boundaries, persistence, or what remains in OpenOCD/target workflows. “Everything target-specific” also leaves adapter and tool-specific behavior unclear. | Implementers can create a large framework before proving the first useful remote flow, and target-specific exceptions will leak back into the core. | Define the smallest host/service contract around discover, authenticate, acquire a lease, run a named operation, query/reconnect, cancel, and retrieve a result. Keep target workflows in profiles and tool-specific mechanics in backends. Add a second backend only when a real target requires it. |
| REQ-02 Adapter, protocol, target layers | The categories are useful but their boundaries are imprecise. J-Link is both a product/probe family and a software backend; CMSIS-DAP is a probe protocol/firmware interface; UART can be a target console channel rather than a debug-access protocol. A probe may implement multiple protocols, and one protocol can reach different target classes. | “Any combination the hardware allows” is unbounded and not testable. It risks a cross-product compatibility promise the project cannot verify. | Name separate concepts: physical probe/backend, wire/debug protocol, target/board, host tool, and workflow. Publish a support matrix of tested combinations and capabilities; unsupported combinations fail before starting an operation. |
| REQ-03 Safety as declarations | The example “do not access address range X unless register Y reads Z” omits address space, width, mask, access side effects, target state, ordering, timeout, and what happens if the check becomes stale. A generic rule interpreter is itself safety-critical. The existing PL/PCAP safeguards are workflow-specific and cannot be generalized from one pattern alone. | A profile may appear to declare safety while the engine cannot prove the precondition applies to the exact operation or remains true. Fail-closed wording alone does not define enforcement. | Keep policy data declarative only within a versioned, typed, bounded schema. Implement enforcement in reviewed code with explicit preconditions, action ordering, postconditions, and failure/recovery behavior. Do not allow arbitrary scripts or generic memory predicates as the initial safety language. Preserve target-specific procedures where needed. |
| REQ-04 Capability declaration | A flat list such as “bitstream load, ELF load, reset levels, power control, console” does not say whether a capability belongs to the probe, protocol, target, or workflow, nor its prerequisites and variants. | A node could advertise “reset” or “flash” when the selected target/backend cannot safely perform that operation. | Model versioned operations with required backend/protocol, target conditions, parameters, and verification. Separate advertised hardware capability from a workflow that has actually been qualified. Resolve support before touching hardware. |
| REQ-05 One result schema and exit-code scheme | The intent is good, but the proposal does not define outcome categories, partial completion, unknown completion after disconnect, cancellation, or cleanup failure. A single integer cannot carry the result detail agents and CI need. | Agents may retry an operation that actually completed or misclassify an unsafe partial operation as a simple failure. | Use a versioned structured result plus a small stable process exit-code set. Include operation/job ID, phase, target, verified effects, failure class, recovery state, and `needs_human` action. Specify retry/idempotency per operation. |
| REQ-06 Remote sessions survive Wi-Fi drops | “Survive” is ambiguous: continue running on the Pi, pause, cancel, or let the client reconnect to query status? It also does not say how console bytes/results are retained or how a second client is prevented from taking over. | A disconnected client may not know whether programming/reset is still in progress. Automatic retry can duplicate a destructive action. | Define a job state machine and lease policy: server-side job identity, exclusive ownership, reconnect/query semantics, bounded output retention, explicit cancellation, and an `outcome-unknown` state where completion cannot be proven. Never silently replay a non-idempotent action. |
| REQ-07 Early two-target validation and tool reuse | Both cautions are sound, but “test ... on two targets” does not define what constitutes evidence, and the example targets have no exact board, probe, or backend. “Wrap OpenOCD, pyOCD or probe-rs” is not a compatibility guarantee; each supports a different matrix. | A framework could be shaped around Zynq while claiming generic support, or the project could spend time integrating tools that cannot drive the selected probe. | Keep the tool-reuse principle. Choose one second vertical slice with an actual board and adapter, and prove a minimum operation end-to-end before stabilizing generic abstractions. Track tool/backend/target combinations explicitly. |

## Findings

### Must-fix before implementing a generic engine

1. **[REQ-03] Safety policy semantics are not specified. Severity: Must-fix; confidence: high.** The idea of profile-declared constraints is valuable, but “register gate” is not a complete rule contract. The project already has concrete target-specific safeguards and limits: PCAP-only PL loading and conservative rate/recovery behavior are documented in `docs/probe-hardening.md` and `jtag/README.md`. Preserve those invariants as explicit workflow steps before attempting a generic rule language. The best argument for declarations is that reviewed target data can be inspected and changed without forking orchestration code; that argument wins only with a narrow schema and a trusted enforcement engine.

2. **[REQ-06] Disconnect behavior is safety-critical and underspecified. Severity: Must-fix for remote mutating operations; confidence: high.** A JTAG network session is not equivalent to a stateless RPC. The outcome of a flash, reset, or FPGA configuration can be unknown after transport loss. Require server-side operation IDs and query/reconnect semantics; do not automatically replay non-idempotent operations.

### Should-fix before promising cross-target support

3. **[REQ-02/04] “Any combination” needs a tested support matrix. Severity: Should-fix; confidence: high.** A backend/protocol/target/workflow matrix is more honest and actionable than generic compatibility. The existing project separates target configuration from adapter selection (`jtag/ebaz4205.cfg`, `jtag/adapters/`), and its PiProbe discovery contract already treats capabilities as versioned resources (`docs/piprobe-discovery.md:46`). Extend those patterns rather than promising an unrestricted cross-product.

4. **[REQ-01/05] Define a deliberately small core. Severity: Should-fix; confidence: high.** Keep discovery, trust, locking, remote job lifecycle, result transport, and logging as shared services. Keep target profiles declarative and host-tool backends replaceable. Result detail belongs in structured data; process exit codes should remain a small stable summary.

5. **[REQ-07] Be precise about the tool wrappers. Severity: Should-fix; confidence: high.** Reusing OpenOCD/pyOCD/vendor tools is the right default. However, the first path already has a narrow, concrete route: OpenOCD has an XVC client, and the existing Pi server intentionally stays on loopback behind SSH (`jtag/xvc/README.md:19,53-67`; [OpenOCD XVC driver](https://openocd.org/doc/html/Debug-Adapter-Configuration.html)). That proves a JTAG transport path, not broad SWD/RTT/SWO compatibility.

### Good-as-is

- **Profiles instead of embedding every board hazard in the orchestrator:** Good direction. The EBAZ4205 workflow has board-specific PCAP, reset, and UART behavior; those should stay in a target workflow/profile rather than become assumptions of a universal core (`jtag/README.md:119-131,209-224`).
- **Testing a second target early:** Good architectural caution, with the qualification that the second target must be a real vertical slice and not a checkbox. It should test a materially different backend/protocol, such as SWD, once an exact board and probe are selected.
- **Do not reimplement debug protocols:** Good-as-is. Reuse established host tools and add only the small integration/backend glue needed for remote access and orchestration.
- **Future `lstty` listing, aliases, and reset:** Keep as a separate client/front-end requirement (`ebaz4205-3pb.6`), not a reason to enlarge the first PiProbe/OpenOCD milestone. The existing PiProbe plan explicitly keeps discovery separate from control authorization (`docs/piprobe-discovery.md:3,42,56-64`).

## Architecture notes

The agent demonstrates sound systems-architecture instincts: separation of target data from orchestration, capability reporting, stable results, and reuse of existing protocol implementations are all sensible. The proposal is not yet an implementable specification. Its largest overreach is treating safety rules and reconnect-resilient jobs as generic conveniences; both need explicit state and verification contracts.

Recommended first shape:

```text
CLI / future lstty client
        ↓
PiProbe control + trust + exclusive lease + remote job/result API
        ↓
Host-tool backend (first: OpenOCD XVC) ── secure tunnel ── Pi service
        ↓                                                    ↓
Target workflow/profile                                probe adapter + pins
```

The first milestone should remain narrow: discover/trust the Pi, acquire one exclusive JTAG lease, run host OpenOCD through XVC, identify the expected TAPs, return a structured result, and cleanly release ownership. Add a second target/backend before calling the profile schema stable. Keep the lstty client deferred until the control contract is proven.

## Assumptions / open questions

- The exact NXP/STM32 board and probe are not named, so this assessment does not claim feasibility for a particular SWD, RTT, SWO, or trace path.
- The agent's proposal was assessed as requirements text, not against the original Claude session body. The index was not needed for this assessment.
- Decide whether an interrupted operation continues server-side, is cancellable, or becomes `outcome-unknown`; define what an agent sees after reconnect.
- Select the second vertical-slice board/backend and minimum test evidence before finalizing the cross-target schema.

## Next steps

1. Amend the draft into atomic, versioned requirements for profile schema, operation states, leases/reconnect, results, and safety preconditions.
2. Implement the narrow OpenOCD/XVC flow against the existing Pi server, retaining SSH host-key verification and loopback-only XVC.
3. Select a concrete NXP or STM32 SWD target and test a minimal discovery/identify/read or flash flow before expanding the shared core.
4. Revisit `ebaz4205-3pb.6` after client feedback to map lstty aliases and simple control commands onto the proven PiProbe API.
