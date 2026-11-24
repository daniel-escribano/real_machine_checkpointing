"""
x86_mixed.py - Restauracion RMC con el ROI en O3.

gem5 instala cada checkpoint en Process.initState() (restauracion directa,
ver rmc_common.py), sin simular nada: la DerivO3CPU arranca en el tick 0 ya
en el ROI. Con --warmup N, antes del ROI se ejecutan N instrucciones en una
CPU simple (calientan caches y TLB) y se conmuta a O3 con m5.switchCpus(); la
O3 hereda la jerarquia L1/L2 por takeOverFrom(). Stats reseteadas al empezar
el ROI.

Soporta 1 checkpoint (single-thread) o N checkpoints en SMT-N sobre un mismo
nucleo (multiprogramado: procesos independientes compartiendo el core). Todos
los hilos empiezan su ROI a la vez.

CLAVE (con --warmup): la CPU switched-out NO debe tener controlador de
interrupciones ni puertos conectados. Crearselos (p.ej. colgados de un dummy
bus) hace que gem5 aborte en el tick 0 con:
    Interrupts::getAddrRanges(): Assertion `tc' failed
Se sigue el patron de configs/common/Simulation.py: a la CPU switched-out solo
se le asignan system, clk_domain, workload e isa (compartida), y createThreads().

Uso:
  gem5.opt --outdir=DIR x86_mixed.py --loader build/loader --ckpts A.ckpt [B.ckpt] \
           [--warmup N] [--load-cpu timing|atomic] [--maxinsts 1000000] \
           [--no-caches] [--loader-opts "OLD=NEW ..."]
"""
import argparse
import os
import sys
import m5
from m5.objects import *

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rmc_common import make_process, require_direct_restore

parser = argparse.ArgumentParser(description="RMC: restauracion directa, ROI en DerivO3CPU")
parser.add_argument("--loader",   type=str, required=True,
                    help="Binario estatico x86-64 que solo elige ISA/SO (build/loader); no se carga")
parser.add_argument("--loader-opts", type=str, default="",
                    help="Remapeos de rutas OLD=NEW para todos los checkpoints")
parser.add_argument("--ckpts",    type=str, nargs="+", required=True,
                    help="1 checkpoint (ST) o N checkpoints (SMT-N sobre un core)")
parser.add_argument("--maxinsts", type=int, default=1000000,
                    help="Instrucciones de ROI (por hilo; para el primero que llegue)")
parser.add_argument("--warmup", type=int, default=0,
                    help="Instrucciones en la CPU simple antes de "
                         "conmutar a O3 (calentar caches). 0 = O3 desde el tick 0")
parser.add_argument("--load-cpu", type=str, default="timing", choices=["timing", "atomic"],
                    help="CPU simple del calentamiento (--warmup)")
parser.add_argument("--no-caches", action="store_true", help="Desactivar L1/L2")
parser.add_argument("--mem",      type=str, default="8GiB")
parser.add_argument("--clock",    type=str, default="2GHz")
parser.add_argument("--trace-roi", action="store_true", help="Flag Exec solo en el ROI")
parser.add_argument("--pmu", action="store_true",
                    help="Instrumentacion PMU del GAP: vuelca CPU_*_THD_*_{DISPATCH_STALLS,"
                         "ISSUE_STALLS,FU_DISTRIBUTION}.csv en el CWD de gem5. Solo mide el "
                         "ROI, porque la CPU O3 no tiene ciclos hasta el switch.")
args = parser.parse_args()

NT = len(args.ckpts)
require_direct_restore()
# CPU simple + conmutacion solo si se pide calentamiento. Si no, la O3 es la
# unica CPU y arranca ya en el ROI.
SWITCH = args.warmup > 0

# -- Sistema ----------------------------------------------------------------
system = System()
system.multi_thread = NT > 1
system.clk_domain   = SrcClockDomain(clock=args.clock, voltage_domain=VoltageDomain())
# El modo de memoria es el de la CPU que arranca: atomic solo si el
# calentamiento va en AtomicSimpleCPU; switchCpus() pasa a timing para O3.
system.mem_mode     = "atomic" if SWITCH and args.load_cpu == "atomic" else "timing"
system.mem_ranges   = [AddrRange(args.mem)]

if SWITCH:
    # CPU activa durante el calentamiento
    system.cpu = (TimingSimpleCPU(cpu_id=0, numThreads=NT) if args.load_cpu == "timing"
                  else AtomicSimpleCPU(cpu_id=0, numThreads=NT))
    system.cpu.max_insts_any_thread = 0

    # CPU de ROI: switched_out. SIN interrupts y SIN puertos (los hereda al switch).
    # OJO: no asignar system.o3.system ni .clk_domain a mano. Como system.o3 es
    # hijo de system, los proxies Parent.any de esos parametros ya los resuelven;
    # asignarlos reparenta `system` bajo `system.o3` y crea un ciclo en el arbol de
    # SimObjects -> RecursionError en SimObject.path() al construir el siguiente
    # SimObject. (Simulation.py si los asigna porque alli las switch_cpus se crean
    # sueltas, fuera del arbol.)
    system.o3 = DerivO3CPU(cpu_id=0, numThreads=NT, switched_out=True)
    active = system.cpu
else:
    # Sin calentamiento: la O3 es la unica CPU y la activa. Se llama
    # system.o3 en ambos casos para que las stats (system.o3.*) sean las
    # mismas (parse_roi_stats.py).
    system.o3 = DerivO3CPU(cpu_id=0, numThreads=NT)
    active = system.o3
system.o3.max_insts_any_thread = 0
if args.pmu:
    system.o3.pmuDispatchActive = True
    system.o3.pmuIssueActive    = True
    system.o3.pmuInitCycle      = 0

system.membus = SystemXBar()

# -- Jerarquia de cache (colgada de la CPU activa; el switch la traspasa) ----
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

    system.icache  = L1ICache()
    system.dcache  = L1DCache()
    system.l2bus   = L2XBar()
    system.l2cache = L2Cache()

    active.icache_port = system.icache.cpu_side
    active.dcache_port = system.dcache.cpu_side
    system.icache.mem_side  = system.l2bus.cpu_side_ports
    system.dcache.mem_side  = system.l2bus.cpu_side_ports
    system.l2cache.cpu_side = system.l2bus.mem_side_ports
    system.l2cache.mem_side = system.membus.cpu_side_ports
else:
    active.icache_port = system.membus.cpu_side_ports
    active.dcache_port = system.membus.cpu_side_ports

system.system_port = system.membus.cpu_side_ports

# Solo la CPU activa lleva controlador de interrupciones.
active.createInterruptController()
for j in range(len(active.interrupts)):
    active.interrupts[j].pio           = system.membus.mem_side_ports
    active.interrupts[j].int_requestor = system.membus.cpu_side_ports
    active.interrupts[j].int_responder = system.membus.mem_side_ports

system.mem_ctrl            = MemCtrl()
system.mem_ctrl.dram       = DDR4_2400_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port       = system.membus.mem_side_ports

# -- Cargas de trabajo: un proceso por hilo, cada uno con su checkpoint -----
env_list = [f"{k}={v}" for k, v in os.environ.items()]
remaps = args.loader_opts.split()
# Rutas absolutas: cada proceso arranca en el cwd de su checkpoint
ckpts = [os.path.abspath(ck) for ck in args.ckpts]
print("**** Restore: direct ****", flush=True)
procs = []
for i, ck in enumerate(ckpts):
    proc, cwd = make_process(ck, args.loader, remaps, pid=100 + i, env=env_list)
    procs.append(proc)
    print(f"  {os.path.basename(ck)} -> direct  cwd={cwd}", flush=True)

system.workload     = SEWorkload.init_compatible(args.loader)
active.workload = procs
active.createThreads()

if SWITCH:
    # La ISA se COMPARTE con la CPU switched-out (igual que Simulation.py), para
    # que el estado arquitectonico (incluido fs_base) sobreviva al traspaso.
    system.o3.isa      = system.cpu.isa
    system.o3.workload = system.cpu.workload
    system.o3.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()

print("**** Checkpoint(s) installed in initState: 0 load cycles ****", flush=True)

# -- Calentamiento (opcional) ----------------------------------------------
if args.warmup:
    print(f"**** WARM-UP: {args.warmup} instructions on {args.load_cpu} ****",
          flush=True)
    for t in range(NT):
        active.scheduleInstStop(t, args.warmup, f"calentamiento: hilo {t}")
    ev = m5.simulate()
    if not ev.getCause().startswith("calentamiento"):
        print(f"[rmc] ERROR: unexpected event during warm-up: '{ev.getCause()}' "
              f"@ {m5.curTick()}", flush=True)
        print(f"Exited @ tick {m5.curTick()} because {ev.getCause()}")
        raise SystemExit(1)
    warm = [active.getCurrentInstCount(t) for t in range(NT)]
    print(f"**** Warm-up complete. Instructions per thread: {warm} ****", flush=True)

# -- Cambio de CPU ----------------------------------------------------------
if SWITCH:
    print("**** Switching to DerivO3CPU"
          f"{'' if args.no_caches else ' (with L1/L2 caches)'} ****", flush=True)
    m5.switchCpus(system, [(system.cpu, system.o3)])

m5.stats.reset()
if args.trace_roi:
    from m5.debug import flags
    flags["Exec"].enable()

# -- ROI en O3 -------------------------------------------------------------
for t in range(NT):
    system.o3.scheduleInstStop(t, args.maxinsts,
                             f"ROI: hilo {t} alcanzo {args.maxinsts} instrucciones")

print(f"**** ROI on O3, {args.maxinsts} instructions per thread ****", flush=True)
ev  = m5.simulate()
roi = [system.o3.getCurrentInstCount(t) for t in range(NT)]

print(f"**** ROI finished: '{ev.getCause()}' ****", flush=True)
for t in range(NT):
    print(f"     thread {t} ({os.path.basename(args.ckpts[t])}): "
          f"{roi[t]} ROI instructions", flush=True)
print(f"Exited @ tick {m5.curTick()} because {ev.getCause()}")
