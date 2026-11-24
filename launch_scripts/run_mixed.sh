#!/bin/bash
# run_mixed.sh - Restauracion RMC con el ROI en O3.
#
#   ./run_mixed.sh <tag> <maxinsts> <timing|atomic> [--pmu] <ckpt1> [ckpt2 ...]
#
# gem5 instala los checkpoints (restauracion directa) y la O3 arranca ya en el
# ROI. RMC_WARMUP=N: antes, N instrucciones en la CPU simple (timing|atomic)
# para calentar caches y TLB.
#
# Un checkpoint  -> single-thread.
# N checkpoints  -> SMT-N multiprogramado sobre un mismo nucleo O3.
#
# Variables de entorno: GEM5_BIN, LOADER, CKPT_DIR, OUT_BASE, LOADER_OPTS,
# RMC_WARMUP.
# Remapeos de rutas: los <ckpt>.remap que deja regenerate_ckpt_noavx.sh
# --upload junto a cada checkpoint (mas LOADER_OPTS="OLD=NEW ...").
#
# gem5 se ejecuta CON EL CWD EN EL OUTDIR: la instrumentacion PMU del GAP
# escribe los CPU_*_THD_*.csv con rutas relativas, asi que de otro modo
# acabarian tirados en el directorio de ejecucion del benchmark.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
check_built

TAG=${1:?falta el tag}; shift
MAXINSTS=${1:?falta maxinsts}; shift
LOADCPU=${1:?missing the warm-up CPU (timing|atomic)}; shift
PMU=""
if [ "${1:-}" = "--pmu" ]; then PMU="--pmu"; shift; fi
[ $# -eq 0 ] && die "at least one checkpoint is required"
# Rutas absolutas: gem5 se lanza con el CWD en el outdir (ver arriba), asi que
# un checkpoint relativo dejaria de encontrarse.
CKPTS=""
for c in "$@"; do
    [ -f "$c" ] || die "checkpoint $c not found"
    CKPTS="$CKPTS $(realpath "$c")"
done
CKPTS="${CKPTS# }"
# shellcheck disable=SC2086
REMAPS="$(ckpt_remaps $CKPTS)"

OUTDIR="$OUT_BASE/$TAG"
mkdir -p "$OUTDIR"
rm -f "$OUTDIR"/CPU_*.csv

echo "=================================================="
echo " tag        : $TAG"
echo " checkpoints: $CKPTS"
echo " restore    : direct   warm-up: $LOADCPU  (${RMC_WARMUP:-0} insts)"
echo " ROI        : DerivO3CPU + caches L1/L2  $PMU"
echo " maxinsts   : $MAXINSTS (per thread)"
echo " remaps     : ${REMAPS:-(none)}"
echo " outdir     : $OUTDIR"
echo "=================================================="

cd "$OUTDIR"
"$GEM5_BIN" --outdir="$OUTDIR" "$REPO/gem5_configs/x86_mixed.py" \
    --loader="$(realpath "$LOADER")" \
    --ckpts $CKPTS \
    --load-cpu="$LOADCPU" \
    --warmup="${RMC_WARMUP:-0}" \
    --maxinsts="$MAXINSTS" --loader-opts="$REMAPS" $PMU
RC=$?

echo ""
echo "=== ROI (DerivO3CPU) ==="
"$REPO/launch_scripts/parse_roi_stats.py" "$OUTDIR" 2>/dev/null
echo "--- PMU CSVs ---"
ls -la "$OUTDIR"/CPU_*.csv 2>/dev/null || echo "(none)"
exit $RC
