"""
x86_mixed_2core.py - Extension de x86_mixed.py a 2 nucleos fisicos, cada uno
en SMT-2 (2 hilos logicos), 4 hilos en total.

Como en x86_mixed.py, gem5 instala los 4 checkpoints en Process.initState()
(restauracion directa, ver rmc_common.py) y las DerivO3CPU arrancan en el
tick 0 ya en el ROI, con los 4 ROI empezando a la vez. Diferencia clave: cada
aplicacion puede tener un ritmo de ejecucion muy distinto (mismo numero de
instrucciones de ROI, duraciones de reloj nativas muy distintas), asi que
scheduleInstStop() por si solo detendria TODA la simulacion en cuanto el
primer hilo (de cualquiera de los 2 nucleos) alcance su objetivo, truncando
la ROI del resto (bug detectado y documentado en la Seccion "Resultados
obtenidos" del TFM para el caso de 2 hilos).

Aqui, en su lugar, se captura cada evento scheduleInstStop individualmente y
se suspende (system.o3_N.suspendContext(tid)) solo ESE hilo -- deja de
competir por recursos de despacho, pero el resto de hilos activos (en el
mismo nucleo o en el otro) siguen ejecutando con normalidad -- hasta que los
4 han alcanzado su propio maxinsts. Cada hilo obtiene asi su ROI completa de
maxinsts instrucciones, bajo el nivel de contencion real del escenario
(decreciente segun otros hilos van terminando y suspendiendose).

Requiere suspendContext/activateContext expuestos a Python en BaseCPU.py
(cxx_exports) -- no vienen expuestos de fabrica en gem5; parcheado en este
arbol.

Cada proceso arranca en el cwd de su checkpoint (con los remapeos de
--loader-opts). --no-switch (diagnostico) ejecuta el ROI en CPUs simples.

Uso:
  gem5.opt --outdir=DIR x86_mixed_2core.py --loader build/loader \
      --ckpts0 A.ckpt B.ckpt --ckpts1 C.ckpt D.ckpt \
      [--maxinsts 10000000] [--pmu] [--no-switch [--load-cpu timing|atomic]] \
      [--loader-opts "OLD=NEW ..."]
"""
import argparse
import os
import re
import sys
import m5
from m5.objects import *

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rmc_common import make_process, require_direct_restore

parser = argparse.ArgumentParser(description="RMC: 2 nucleos SMT-2, restauracion directa, ROI en DerivO3CPU")
parser.add_argument("--loader",   type=str, required=True,
                    help="Binario estatico x86-64 que solo elige ISA/SO (build/loader); no se carga")
parser.add_argument("--loader-opts", type=str, default="",
                    help="Remapeos de rutas OLD=NEW para todos los checkpoints")
parser.add_argument("--ckpts0",   type=str, nargs="+", required=True, help="Checkpoints del nucleo 0")
parser.add_argument("--ckpts1",   type=str, nargs="+", required=True, help="Checkpoints del nucleo 1")
parser.add_argument("--maxinsts", type=int, default=10_000_000,
                    help="Instrucciones de ROI por hilo (cada hilo alcanza SU propio objetivo)")
parser.add_argument("--load-cpu", type=str, default="timing", choices=["timing", "atomic"],
                    help="CPU simple de --no-switch")
parser.add_argument("--maxinsts-list", type=str, default=None,
                    help="Objetivo de instrucciones POR HILO, separado por comas y en el orden "
                         "ckpts0...,ckpts1... Ajustarlo al IPC de cada aplicacion hace que todas "
                         "terminen a la vez y la ROI entera transcurra con contencion plena.")
parser.add_argument("--calib-ticks", type=int, default=0,
                    help="Modo calibracion: simular N ticks con los 4 hilos activos y reportar "
                         "cuantas instrucciones completa cada uno (da su IPC bajo contencion real).")
parser.add_argument("--no-caches", action="store_true")
parser.add_argument("--no-switch", action="store_true",
                    help="Diagnostico: ejecutar la ROI en la CPU simple, sin conmutar a O3")
parser.add_argument("--mem",      type=str, default="8GiB")
parser.add_argument("--clock",    type=str, default="2GHz")
parser.add_argument("--pmu", action="store_true",
                    help="Instrumentacion PMU del GAP en ambos nucleos (CPU_0_* y CPU_1_*).")
args = parser.parse_args()

NT0, NT1 = len(args.ckpts0), len(args.ckpts1)
require_direct_restore()
# Las O3 son las CPUs activas desde el tick 0, salvo --no-switch, que deja el
# ROI en CPUs simples para diagnostico.
O3_ACTIVE = not args.no_switch
REMAPS = args.loader_opts.split()
# Rutas absolutas: cada proceso arranca en el cwd de su checkpoint
args.ckpts0 = [os.path.abspath(ck) for ck in args.ckpts0]
args.ckpts1 = [os.path.abspath(ck) for ck in args.ckpts1]

system = System()
# Siempre True: es lo que usa el x86_mixed.py validado. Con 2 CPUs y 1 hilo
# cada una quedaba en False, que es una de las diferencias de configuracion
# frente al caso que funciona.
system.multi_thread = True
system.clk_domain   = SrcClockDomain(clock=args.clock, voltage_domain=VoltageDomain())
# Modo de memoria de las CPUs que arrancan (switchCpus pasa a timing para O3)
system.mem_mode     = "atomic" if not O3_ACTIVE and args.load_cpu == "atomic" else "timing"
system.mem_ranges   = [AddrRange(args.mem)]
system.membus       = SystemXBar()
system.mem_ctrl            = MemCtrl()
system.mem_ctrl.dram       = DDR4_2400_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port       = system.membus.mem_side_ports
system.system_port         = system.membus.cpu_side_ports


def build_o3(idx, nt, switched_out=True):
    cpu_o3 = DerivO3CPU(cpu_id=idx, numThreads=nt, switched_out=switched_out)
    cpu_o3.max_insts_any_thread = 0
    if args.pmu:
        cpu_o3.pmuDispatchActive = True
        cpu_o3.pmuIssueActive    = True
        cpu_o3.pmuInitCycle      = 0
    return cpu_o3


def build_core(idx, nt, ckpts):
    """Crea la CPU activa de un nucleo, con su propia jerarquia privada
    L1I/L1D/L2: la O3 o, con --no-switch, una CPU simple."""
    if O3_ACTIVE:
        cpu = build_o3(idx, nt, switched_out=False)
    else:
        cpu = (TimingSimpleCPU(cpu_id=idx, numThreads=nt) if args.load_cpu == "timing"
                      else AtomicSimpleCPU(cpu_id=idx, numThreads=nt))
        cpu.max_insts_any_thread = 0

    if not args.no_caches:
        class L1ICache(Cache):
            size = "32kB"; assoc = 8
            tag_latency = 1; data_latency = 1; response_latency = 1
            mshrs = 4; tgts_per_mshr = 20

        class L1DCache(Cache):
            size = "32kB"; assoc = 8
            tag_latency = 2; data_latency = 2; response_latency = 2
            mshrs = 16; tgts_per_mshr = 20

        class L2Cache(Cache):
            size = "256kB"; assoc = 8
            tag_latency = 10; data_latency = 10; response_latency = 10
            mshrs = 20; tgts_per_mshr = 12

        icache, dcache, l2cache = L1ICache(), L1DCache(), L2Cache()
        l2bus = L2XBar()
        cpu.icache_port = icache.cpu_side
        cpu.dcache_port = dcache.cpu_side
        icache.mem_side  = l2bus.cpu_side_ports
        dcache.mem_side  = l2bus.cpu_side_ports
        l2cache.cpu_side = l2bus.mem_side_ports
        l2cache.mem_side = system.membus.cpu_side_ports
    else:
        cpu.icache_port = system.membus.cpu_side_ports
        cpu.dcache_port = system.membus.cpu_side_ports
        icache = dcache = l2cache = l2bus = None

    cpu.createInterruptController()
    for j in range(len(cpu.interrupts)):
        cpu.interrupts[j].pio           = system.membus.mem_side_ports
        cpu.interrupts[j].int_requestor = system.membus.cpu_side_ports
        cpu.interrupts[j].int_responder = system.membus.mem_side_ports

    # El entorno del proceso restaurado es el del checkpoint (gen_ckpt.sh ya
    # lo genera con el GLIBC_TUNABLES sin AVX).
    env_list = [f"{k}={v}" for k, v in os.environ.items()]
    procs = []
    for i, ck in enumerate(ckpts):
        proc, cwd = make_process(ck, args.loader, REMAPS, pid=100 + idx * 10 + i, env=env_list)
        print(f"  core{idx} thread{i}: {os.path.basename(ck)} -> direct  cwd={cwd}", flush=True)
        procs.append(proc)

    cpu.workload = procs

    return cpu, (icache, dcache, l2cache, l2bus)


# Orden de registro: primero las CPUs que ejecutan (cpu0/cpu1 u o3_0/o3_1) y
# despues las switched-out, como configs/common/Simulation.py. gem5 asigna los
# context id en el orden en que las CPUs se cuelgan del System y cada Process
# queda ligado a ellos.
if O3_ACTIVE:
    system.o3_0, caches0 = build_core(0, NT0, args.ckpts0)
    system.o3_1, caches1 = build_core(1, NT1, args.ckpts1)
else:
    system.cpu0, caches0 = build_core(0, NT0, args.ckpts0)
    system.cpu1, caches1 = build_core(1, NT1, args.ckpts1)
if not args.no_caches:
    system.icache0, system.dcache0, system.l2cache0, system.l2bus0 = caches0
    system.icache1, system.dcache1, system.l2cache1, system.l2bus1 = caches1

if not O3_ACTIVE:
    system.o3_0 = build_o3(0, NT0)
    system.o3_1 = build_o3(1, NT1)

# Solo elige ISA/SO: el ejecutable (build/loader) no se carga
system.workload = SEWorkload.init_compatible(args.loader)

if O3_ACTIVE:
    system.o3_0.createThreads()
    system.o3_1.createThreads()
else:
    # --no-switch: createThreads() de la CPU simple primero (crea los objetos
    # ISA por hilo) y despues se comparten con la O3 switched-out.
    for cpu_simple, cpu_o3 in ((system.cpu0, system.o3_0), (system.cpu1, system.o3_1)):
        cpu_simple.createThreads()
        cpu_o3.isa      = cpu_simple.isa
        cpu_o3.workload = cpu_simple.workload
        cpu_o3.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()

CORES = [(0, None if O3_ACTIVE else system.cpu0, system.o3_0, NT0, args.ckpts0),
         (1, None if O3_ACTIVE else system.cpu1, system.o3_1, NT1, args.ckpts1)]
NT_TOTAL = NT0 + NT1

print(f"**** {NT_TOTAL} checkpoints installed in initState: 0 load cycles ****",
      flush=True)

if args.no_switch:
    # Diagnostico: ejecutar la ROI en una CPU simple. Sirve para separar "el
    # montaje de 2 nucleos esta mal" de "el fallo esta en la O3".
    print("**** NO SWITCH: the ROI runs on the simple CPU ****", flush=True)
    ROI_CPUS = [(idx, cpu_simple, nt, ckpts) for idx, cpu_simple, _, nt, ckpts in CORES]
else:
    print("**** ROI on DerivO3CPU from tick 0 (direct restoration) ****", flush=True)
    ROI_CPUS = [(idx, cpu_o3, nt, ckpts) for idx, _, cpu_o3, nt, ckpts in CORES]
m5.stats.reset()

if args.calib_ticks:
    print(f"**** CALIBRATION: {args.calib_ticks} ticks with all {NT_TOTAL} threads active ****", flush=True)
    m5.simulate(args.calib_ticks)
    print("**** Instructions completed per thread (full contention) ****", flush=True)
    for core_idx, roi_cpu, nt, ckpts in ROI_CPUS:
        for t in range(nt):
            n = roi_cpu.getCurrentInstCount(t)
            print(f"  core{core_idx} thread{t} ({os.path.basename(ckpts[t])}): {n}", flush=True)
    raise SystemExit(0)

TARGETS = ([int(x) for x in args.maxinsts_list.split(",")]
           if args.maxinsts_list else [args.maxinsts] * NT_TOTAL)
if len(TARGETS) != NT_TOTAL:
    raise SystemExit(f"[rmc] FATAL: --maxinsts-list needs {NT_TOTAL} values, got {len(TARGETS)}")

k = 0
for core_idx, roi_cpu, nt, ckpts in ROI_CPUS:
    for t in range(nt):
        roi_cpu.scheduleInstStop(
            t, TARGETS[k],
            f"ROI core{core_idx} thread{t} reached {TARGETS[k]} instructions")
        k += 1

print(f"**** ROI on {'the simple CPU' if args.no_switch else 'O3'}, "
      f"{args.maxinsts} instructions per thread, "
      f"{NT_TOTAL} threads on 2 cores ****", flush=True)

CAUSE_RE = re.compile(r"^ROI core(\d+) thread(\d+) reached")
remaining = NT_TOTAL
while remaining > 0:
    ev    = m5.simulate()
    cause = ev.getCause()
    m = CAUSE_RE.match(cause)
    if not m:
        print(f"[rmc] ERROR: unexpected event during the ROI: '{cause}' @ tick {m5.curTick()}", flush=True)
        raise SystemExit(1)
    core_idx, tid = int(m.group(1)), int(m.group(2))
    if args.no_switch:
        cpu_o3 = system.cpu0 if core_idx == 0 else system.cpu1
    else:
        cpu_o3 = system.o3_0 if core_idx == 0 else system.o3_1
    insts_now = cpu_o3.getCurrentInstCount(tid)
    cpu_o3.suspendContext(tid)
    remaining -= 1
    print(f"  -> {cause} (tick {m5.curTick()}, {insts_now} insts) -- thread suspended, "
          f"{remaining} still active", flush=True)

print(f"**** ROI finished: all {NT_TOTAL} threads reached their target. final tick {m5.curTick()} ****",
      flush=True)
for core_idx, _, cpu_o3, nt, ckpts in CORES:
    for t in range(nt):
        insts = cpu_o3.getCurrentInstCount(t)
        print(f"     core{core_idx} thread{t} ({os.path.basename(ckpts[t])}): "
              f"{insts} ROI instructions", flush=True)
