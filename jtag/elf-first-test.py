#!/usr/bin/env python3
"""ELF first, bitstream later, then debug (Raspberry Pi, raspberrypi-gpio adapter).

Starts one resident OpenOCD and drives it over its loopback telnet port:
  1. PS init, clear the PL, load and start the ELF with no bitstream
  2. program the bitstream while the ELF runs; the UART heartbeat must continue
  3. halt, hardware breakpoint, single-step and memory reads through OpenOCD
  4. attach gdb-multiarch to the OpenOCD gdb port

usage: jtag/elf-first-test.py <bitstream.bit> [khz] [elf]

Use an ELF built with a short heartbeat so the UART shows progress quickly:
  make BUILD_DIR=/tmp/fastbuild EXTRA_CFLAGS=-DHEARTBEAT_DELAY=2000000
Environment: EBAZ_OPENOCD (runner), EBAZ_UART_DEVICE (default /dev/ttyAMA0).
Nothing else may hold the UART or run OpenOCD; any running openocd is killed.
"""
import os, re, socket, subprocess, sys, termios, threading, time

J = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(J)
ELF = sys.argv[3] if len(sys.argv) > 3 else ROOT + "/build/hello.elf"
BIT = sys.argv[1]
KHZ = sys.argv[2] if len(sys.argv) > 2 else "2000"
OOCD = os.environ.get("EBAZ_OPENOCD", "/usr/local/libexec/openocd-ebaz-rpi")
UART = os.environ.get("EBAZ_UART_DEVICE", "/dev/ttyAMA0")

pld = "zynq_pl.pld" if re.search(r"^\s*pld create ", open(J + "/openocd-scripts/target/zynq_7000.cfg").read(), re.M) else "0"
env = dict(os.environ, EBAZ_BUNDLE_DIR=J, EBAZ_PLD_DEVICE=pld, EBAZ_PLD_RETRIES="1",
           EBAZ_JTAG_RATE_LADDER=KHZ, EBAZ_TEST_FAIL_PL_ATTEMPT="", EBAZ_JTAG_ADAPTER="raspberrypi-gpio")

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (" :: " + detail if detail else ""), flush=True)

# ---- UART reader -----------------------------------------------------------
beats = []          # (monotonic time, heartbeat value)
def uart_reader():
    fd = os.open(UART, os.O_RDONLY | os.O_NOCTTY)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    a[6][termios.VMIN] = 0; a[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, a)
    buf = b""
    while True:
        chunk = os.read(fd, 256)
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            m = re.search(rb"heartbeat=(\d+)", line)
            if m:
                beats.append((time.monotonic(), int(m.group(1))))
threading.Thread(target=uart_reader, daemon=True).start()

# ---- OpenOCD over telnet ---------------------------------------------------
subprocess.run(["sudo", "pkill", "openocd"], check=False)
time.sleep(1)
log = open("/tmp/livetest-openocd.log", "w")
proc = subprocess.Popen(["sudo", "-E", OOCD, "-s", J + "/openocd-scripts",
                         "-f", J + "/adapters/raspberrypi-gpio.cfg", "-f", J + "/ebaz4205.cfg",
                         "-c", "adapter speed " + KHZ, "-c", "bindto 127.0.0.1",
                         "-c", "telnet_port 4444", "-c", "gdb_port 3333", "-c", "tcl_port disabled",
                         "-c", "init"], env=env, stdout=log, stderr=subprocess.STDOUT)
sock = None
for _ in range(60):
    try:
        sock = socket.create_connection(("127.0.0.1", 4444), timeout=2); break
    except OSError:
        time.sleep(0.5)
if sock is None:
    sys.exit("OpenOCD telnet did not come up: " + open("/tmp/livetest-openocd.log").read()[-800:])
sock.settimeout(300)

def readuntil_prompt():
    data = b""
    while not data.endswith(b"\n> ") and not data.endswith(b"> "):
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data.decode(errors="replace")

readuntil_prompt()
SENTINEL = b"@@42@@"
NL, CR, NUL = chr(10), chr(13), chr(0)
def cmd(c, show=True):
    sock.sendall((c + NL).encode())
    sock.sendall(("echo \"@@[expr 40+2]@@\"" + NL).encode())
    data = b""
    while SENTINEL not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    text = data.decode(errors="replace").replace(CR, "").replace(NUL, "").split("@@42@@")[0]
    kept = []
    for line in text.split(NL):
        line = line.lstrip("> ").rstrip()
        if line and line != c and "@@[expr 40+2]@@" not in line:
            kept.append(line)
    out = NL.join(kept)
    if show:
        print("  > " + c + ((NL + "    " + out.replace(NL, NL + "    ")) if out else ""), flush=True)
    return out

def last_beat():
    return beats[-1][1] if beats else None
def wait_beats(n, timeout=15):
    start = len(beats); t0 = time.time()
    while len(beats) - start < n and time.time() - t0 < timeout:
        time.sleep(0.2)
    return len(beats) - start >= n
def curstate():
    m = re.search(r"STATE=(\w+)", cmd('echo "STATE=[zynq.cpu0 curstate]"', show=False))
    return m.group(1) if m else "unknown"
def reg(name):
    return int(re.search(r"0x[0-9a-fA-F]+", cmd("reg " + name, show=False)).group(0), 16)
def intsts():
    return int(re.search(r":\s*(0x)?([0-9a-fA-F]{8})", cmd("mdw 0xF800700C", show=False)).group(2), 16)

try:
    print("== 0. attach")
    cmd("targets zynq.cpu0"); cmd("halt")
    for f in ("xsa/openocd-xilinx-compat.tcl", "xsa/ps7_init.tcl", "pl-program.tcl"):
        cmd("source " + J + "/" + f, show=False)

    print("== 1. PS init, clear PL, load ELF, run (no bitstream)")
    cmd("ps7_init", show=False)
    cmd("ebaz_prepare_pl")
    done = intsts() & 0x4
    check("PL is unconfigured before the ELF starts", done == 0, "INT_STS PCFG_DONE=%d" % (1 if done else 0))
    cmd("load_image " + ELF); cmd("verify_image " + ELF)
    cmd("target smp zynq.cpu0"); cmd("targets zynq.cpu0")
    elf_start = len(beats)
    cmd("resume 0x10000")
    ok = wait_beats(4, 30)
    b1 = last_beat()
    check("ELF runs without any bitstream (UART heartbeat)", ok and b1 is not None, "heartbeat=%s" % b1)

    print("== 2. program the bitstream while the ELF is running")
    before = last_beat()
    t_start = time.time()
    cmd("halt", show=False)
    cmd("ebaz_program_pl " + BIT)
    cmd("ps7_post_config", show=False)
    cmd("ebaz_verify_post_config")
    done_after = intsts() & 0x4
    cmd("resume", show=False)
    elapsed = time.time() - t_start
    check("PL configured (PCFG_DONE=1) after the ELF", done_after != 0, "%.1f s with the CPU halted" % elapsed)
    ok = wait_beats(3)
    after = last_beat()
    check("heartbeat continues, counter not reset by the PL load", ok and after > before, "before=%s after=%s" % (before, after))
    seq = [n for _, n in beats[elf_start:]]
    check("heartbeat sequence has no restart", all(b >= a for a, b in zip(seq, seq[1:])), "min step ok, last=%s" % seq[-1])

    print("== 3. OpenOCD-native debugging")
    cmd("halt")
    pc = reg("pc")
    check("halt reads a PC inside the application", 0x10000 <= pc < 0x10200, hex(pc))
    cmd("bp 0x10018 4 hw")          # uart_puts
    cmd("resume")
    time.sleep(2.5)
    state = curstate()
    check("hardware breakpoint at uart_puts is hit", state == "halted" and reg("pc") == 0x10018, "state=%s pc=%s" % (state, hex(reg("pc"))))
    r0 = reg("r0")
    cmd("mdb 0x%x 52" % r0)
    cmd("rbp 0x10018")
    pcs = []
    for _ in range(3):
        cmd("step", show=False); pcs.append(reg("pc"))
    check("single-step advances the PC", len(set(pcs)) == 3 and pcs[0] != 0x10018, " ".join(hex(p) for p in pcs))
    cmd("mdw 0xE0001000 4")           # UART1 control/mode/intr registers
    beat_before_resume = last_beat()
    cmd("resume", show=False)
    ok = wait_beats(3)
    check("program continues after debugging", ok and last_beat() > beat_before_resume, "heartbeat=%s" % last_beat())

    print("== 4. gdb-multiarch over the OpenOCD gdb server")
    gdb = subprocess.run(["gdb-multiarch", "-batch", "-q", ELF,
        "-ex", "set architecture arm", "-ex", "target extended-remote localhost:3333",
        "-ex", "info registers pc sp lr", "-ex", "bt",
        "-ex", "break uart_puts", "-ex", "continue", "-ex", "bt", "-ex", "x/s $r0",
        "-ex", "delete", "-ex", "detach"], capture_output=True, text=True, timeout=120)
    print("    " + (gdb.stdout + gdb.stderr).strip().replace("\n", "\n    "))
    check("gdb attaches, hits the breakpoint and shows the string argument",
          "uart_puts" in gdb.stdout and "Hello world" in gdb.stdout, "exit=%d" % gdb.returncode)
    time.sleep(1)
    state = curstate()
    print("INFO core state right after gdb detach: " + state, flush=True)
    if state == "halted":
        cmd("resume")
    beat_before = last_beat()
    ok = wait_beats(3)
    check("program runs after gdb detach (+resume if it stayed halted)", ok and last_beat() > beat_before, "heartbeat=%s" % last_beat())
finally:
    print("== summary: %d/%d passed" % (sum(results), len(results)))
    try:
        cmd("resume", show=False)
    except Exception:
        pass
    sock.close(); subprocess.run(["sudo", "pkill", "openocd"], check=False)
sys.exit(0 if all(results) else 1)
