"""
rmc_common.py - Helpers compartidos por las configs de gem5 de RMC.

Restauracion: gem5 instala el checkpoint en Process.initState() (parametros
Process.rmcCheckpoint / rmcRemaps del gem5 del GAP): cero ciclos simulados,
el primer ciclo ya es el ROI. Requiere ese gem5; con otro, las configs
terminan con FATAL. El ejecutable del Process (build/loader) solo le dice a
gem5 la ISA y el SO: no se carga ni se simula.

pick_loader() es para la restauracion NATIVA (en el host, fuera de gem5):
el break inicial de un binario estatico es el final de su segmento mas
alto. build/loader lo tiene en ~0x200xxxxx (objetivos no PIE, heap por
debajo) y build/loader_pie en 0x5555_0000_xxxx (PIE, heap justo encima).

Directorio de trabajo: el checkpoint guarda el cwd del programa (pseudo-FD
CKPT_FD_CWD). Las configs lo pasan a Process(cwd=...), con los mismos
remapeos OLD=NEW que los ficheros, para que las rutas relativas que el
programa abra durante el ROI se resuelvan como en la maquina real.
"""
import os
import struct

HDR_SZ, REGION_SZ, FD_SZ = 4480, 104, 272
CKPT_FLAG_HEAP = 0x04
CKPT_FD_CWD = -100
PIE_HEAP_MIN = 1 << 32   # heaps por encima de 4 GiB: objetivo PIE


def _read_tables(path):
    with open(path, "rb") as f:
        hdr = f.read(HDR_SZ)
        nreg, nfds = struct.unpack_from("<II", hdr, 12)
        regions = [f.read(REGION_SZ) for _ in range(nreg)]
        fds = [f.read(FD_SZ) for _ in range(nfds)]
    return regions, fds


def ckpt_heap_end(path):
    """Final del [heap] del checkpoint (0 si no tiene)."""
    heap_end = 0
    for b in _read_tables(path)[0]:
        _start, end = struct.unpack_from("<QQ", b, 0)
        flags = struct.unpack_from("<I", b, 20)[0]
        if flags & CKPT_FLAG_HEAP:
            heap_end = max(heap_end, end)
    return heap_end


def ckpt_cwd(path):
    """Directorio de trabajo guardado en el checkpoint (None si no hay)."""
    for b in _read_tables(path)[1]:
        fd = struct.unpack_from("<i", b, 0)[0]
        if fd == CKPT_FD_CWD:
            return b[16:272].split(b"\0")[0].decode(errors="replace")
    return None


def apply_remaps(path, remaps):
    """Mismo remapeo OLD=NEW que el loader (por componentes de ruta)."""
    for r in remaps:
        if "=" not in r:
            continue
        old, new = r.split("=", 1)
        if not path.startswith(old):
            continue
        rest = path[len(old):]
        if rest == "" or rest.startswith("/") or old.endswith("/"):
            return new + rest
    return path


def process_cwd(ckpt, remaps):
    """cwd para Process(): el del checkpoint remapeado, si existe aqui."""
    cwd = ckpt_cwd(ckpt)
    if cwd is None:
        return os.getcwd()
    cwd = apply_remaps(cwd, remaps)
    if not os.path.isdir(cwd):
        print(f"[rmc] WARN: the checkpoint cwd does not exist here ({cwd}); "
              f"using {os.getcwd()}. Add an OLD=NEW remap.", flush=True)
        return os.getcwd()
    return cwd


def gem5_has_direct_restore():
    """True si este gem5 tiene Process.rmcCheckpoint."""
    from m5.objects import Process
    return "rmcCheckpoint" in Process._params


def require_direct_restore():
    """Termina con FATAL si este gem5 no puede instalar checkpoints RMC."""
    if not gem5_has_direct_restore():
        raise SystemExit("[rmc] FATAL: this gem5 has no Process.rmcCheckpoint "
                         "(build the GAP gem5 with direct restoration)")


def make_process(ckpt, exe, remaps=(), pid=100, env=()):
    """Process que gem5 restaura desde ckpt. Devuelve (process, cwd).

    exe solo elige ISA/SO (cualquier binario estatico x86-64; build/loader
    porque siempre esta) y NO se carga: gem5 instala el checkpoint con los
    remapeos. El entorno del proceso es el del checkpoint.
    """
    from m5.objects import Process
    cwd = process_cwd(ckpt, remaps)
    proc = Process(pid=pid, executable=exe, cmd=[exe, ckpt], env=list(env),
                   cwd=cwd, rmcCheckpoint=ckpt, rmcRemaps=list(remaps))
    return proc, cwd


def pick_loader(ckpt, loader, loader_pie=None):
    """Loader nativo: build/loader_pie para checkpoints PIE si existe, build/loader si no."""
    if loader_pie is None:
        loader_pie = loader + "_pie"
    if os.path.exists(loader_pie) and ckpt_heap_end(ckpt) >= PIE_HEAP_MIN:
        return loader_pie
    return loader


if __name__ == "__main__":
    # Uso desde scripts: rmc_common.py <ckpt> [build_dir] -> loader nativo
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: rmc_common.py <ckpt> [build_dir]")
    build = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "build")
    print(os.path.normpath(pick_loader(sys.argv[1], os.path.join(build, "loader"))))
