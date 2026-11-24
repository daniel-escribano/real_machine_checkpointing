"""
x86_st_timing.py - Arnes RAPIDO de depuracion para checkpoints RMC: todo en
una CPU simple (timing o atomic), sin O3.

gem5 instala el checkpoint en Process.initState() (restauracion directa, ver
rmc_common.py) y la CPU arranca ya en el ROI: se simulan --maxinsts
instrucciones y se reportan.

Uso:
  gem5.opt --outdir=DIR x86_st_timing.py --cmd build/loader --options "CKPT [remaps]" \
           [--cpu timing|atomic] [--caches] [--maxinsts 1000000]
"""
import argparse
import os
import sys
import m5
from m5.objects import *

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rmc_common import make_process, require_direct_restore

parser = argparse.ArgumentParser(description="Arnes rapido (CPU simple) para restaurar checkpoints RMC")
parser.add_argument("--cmd",      type=str, required=True,
                    help="Binario estatico x86-64 que solo elige ISA/SO (build/loader); no se carga")
parser.add_argument("--options",  type=str, default="",    help="Checkpoint y remapeos OLD=NEW")
parser.add_argument("--maxinsts", type=int, default=1000000, help="Instrucciones a simular en el ROI")
parser.add_argument("--cpu",      type=str, default="timing", choices=["timing", "atomic"],
                    help="Modelo de CPU (timing = TimingSimpleCPU, atomic = AtomicSimpleCPU)")
parser.add_argument("--caches",   action="store_true", help="Anadir jerarquia L1/L2 (mas lento)")
parser.add_argument("--mem",      type=str, default="8GiB", help="Tamano de memoria fisica simulada")
parser.add_argument("--trace-roi", action="store_true",
                    help="Activar el flag de depuracion Exec SOLO durante el ROI")
args = parser.parse_args()
require_direct_restore()

# -- Sistema ----------------------------------------------------------------
system = System()
system.clk_domain = SrcClockDomain(clock="2GHz", voltage_domain=VoltageDomain())
system.mem_mode   = "timing" if args.cpu == "timing" else "atomic"
system.mem_ranges = [AddrRange(args.mem)]

system.cpu = TimingSimpleCPU() if args.cpu == "timing" else AtomicSimpleCPU()
system.cpu.max_insts_any_thread = 0

system.membus = SystemXBar()

if args.caches:
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

    system.cpu.icache = L1ICache()
    system.cpu.dcache = L1DCache()
    system.l2bus      = L2XBar()
    system.l2cache    = L2Cache()

    system.cpu.icache_port  = system.cpu.icache.cpu_side
    system.cpu.dcache_port  = system.cpu.dcache.cpu_side
    system.cpu.icache.mem_side = system.l2bus.cpu_side_ports
    system.cpu.dcache.mem_side = system.l2bus.cpu_side_ports
    system.l2cache.cpu_side    = system.l2bus.mem_side_ports
    system.l2cache.mem_side    = system.membus.cpu_side_ports
else:
    # Sin caches: la CPU habla directamente con el bus de memoria.
    system.cpu.icache_port = system.membus.cpu_side_ports
    system.cpu.dcache_port = system.membus.cpu_side_ports

system.system_port = system.membus.cpu_side_ports

system.cpu.createInterruptController()
system.cpu.interrupts[0].pio           = system.membus.mem_side_ports
system.cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
system.cpu.interrupts[0].int_responder = system.membus.mem_side_ports

system.mem_ctrl            = MemCtrl()
system.mem_ctrl.dram       = DDR4_2400_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port       = system.membus.mem_side_ports

# -- Proceso ----------------------------------------------------------------
env_list = [f"{k}={v}" for k, v in os.environ.items()]
opts = args.options.split()
if not opts:
    raise SystemExit("[rmc] FATAL: --options must start with the checkpoint")
# Absoluta: el proceso arranca en el cwd del checkpoint, no en el de gem5
ckpt = os.path.abspath(opts[0])
process, cwd = make_process(ckpt, args.cmd, remaps=opts[1:], env=env_list)
print(f"**** Direct restoration   cwd: {cwd} ****", flush=True)
system.workload     = SEWorkload.init_compatible(args.cmd)
system.cpu.workload = process
system.cpu.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()

print("**** Checkpoint installed in initState: 0 load cycles ****", flush=True)

# -- ROI --------------------------------------------------------------------
m5.stats.reset()
if args.trace_roi:
    from m5.debug import flags
    flags["Exec"].enable()
    print("**** Exec trace enabled for the ROI ****", flush=True)
system.cpu.scheduleInstStop(0, args.maxinsts, f"ROI: {args.maxinsts} instrucciones alcanzadas")

print(f"**** ROI: simulating {args.maxinsts} ROI instructions ****", flush=True)
exit_event = m5.simulate()
roi_ins    = system.cpu.getCurrentInstCount(0)

print(f"**** ROI finished: '{exit_event.getCause()}' ****", flush=True)
print(f"**** ROI instructions executed: {roi_ins} / {args.maxinsts} ****", flush=True)
print(f"Exited @ tick {m5.curTick()} because {exit_event.getCause()}")
