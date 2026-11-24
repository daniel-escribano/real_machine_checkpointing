#!/bin/bash
# gen_ckpt.sh - UNICA forma de generar un checkpoint RMC.
#
# Todos los lanzadores (regenerate_ckpt_noavx.sh, e2e_altek.sh, test/,
# run_example.sh) generan a traves de este script, para que un checkpoint
# salga siempre en las mismas condiciones:
#
#   - ASLR desactivado: setarch -R, envolviendo a env. (Al reves, libckpt.so
#     se cargaria en setarch, quitaria LD_PRELOAD del entorno y el programa
#     arrancaria sin ella.)
#   - GLIBC_TUNABLES=$RMC_NOAVX_TUNABLES (_common.sh): la glibc elige rutas
#     SSE2, que gem5 SE implementa.
#   - LD_BIND_NOW=1: todos los simbolos se resuelven al arrancar, asi el
#     resolvedor perezoso de ld.so (que salva registros con xsave/xsavec
#     segun la CPU) nunca se ejecuta dentro del ROI.
#   - libckpt.so por LD_PRELOAD. Los binarios estaticos se rechazan (la
#     ignorarian); un PIE que llame a ckpt_dump() enlaza src/dumper.c y
#     src/dumper_asm.S.
#   - Se comprueba que el binario no contiene AVX/BMI2.
#   - Se espera a que el volcado este COMPLETO (el dumper escribe <out>.tmp
#     y lo renombra al terminar) y se valida con tools/ckpt_inspect.py.
#
# Uso:
#   gen_ckpt.sh -o SALIDA.ckpt [-C DIR] [-t NS | -s SIMBOLO[:N]] [-w]
#               [--timeout S] -- comando [args...]
#
#   -o SALIDA   checkpoint a generar. Junto a el quedan SALIDA.log (stderr:
#               mensajes del volcado y del programa), SALIDA.stdout,
#               SALIDA.inspect y SALIDA.meta (condiciones de generacion)
#   -C DIR      directorio de trabajo del programa (por defecto el actual);
#               el checkpoint lo guarda y gem5 lo restaura
#   -t NS       volcado a los NS nanosegundos (CKPT_AFTER_NS)
#   -s SIM[:N]  volcado en la N-esima llamada a SIM (CKPT_AT_SYMBOL)
#               Sin -t ni -s: el programa llama a ckpt_dump() o se le envia
#               SIGUSR1 a mano.
#   -i FICHERO  entrada estandar del programa (por defecto /dev/null, para
#               que no dependa de desde donde se lance). Si es un fichero,
#               el loader la restaura con su offset.
#   -w          esperar a que el programa termine (por defecto se mata en
#               cuanto el volcado esta completo)
#   --timeout S segundos maximos (900 por defecto)
#
# RMC_ALLOW_ASLR=1 permite generar con ASLR si setarch -R no funciona (p.ej.
# con el seccomp por defecto de Docker). Por defecto es un error: el
# checkpoint no saldria como los demas.
#
# Sale con 0 si el checkpoint es valido, 1 si no.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
LOG_COMPONENT=gen

OUT=""; DIR="$PWD"; NS=""; SYM=""; WAIT=0; TIMEOUT=900; STDIN=/dev/null
while [ $# -gt 0 ]; do
    case "$1" in
        -o) OUT=$2; shift 2 ;;
        -i) STDIN=$2; shift 2 ;;
        -C) DIR=$2; shift 2 ;;
        -t) NS=$2; shift 2 ;;
        -s) SYM=$2; shift 2 ;;
        -w) WAIT=1; shift ;;
        --timeout) TIMEOUT=$2; shift 2 ;;
        --) shift; break ;;
        -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (the command goes after --)" ;;
    esac
done
[ -n "$OUT" ] || die "missing -o OUTPUT.ckpt"
[ $# -gt 0 ]  || die "missing the command (after --)"
[ -f "$LIBCKPT" ] || die "$LIBCKPT not found (run: make -C $REPO)"
DIR="$(cd "$DIR" 2>/dev/null && pwd)" || die "working directory not found"
STDIN="$(realpath -e "$STDIN" 2>/dev/null)" || die "stdin file given with -i not found"
mkdir -p "$(dirname "$OUT")"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
NAME="$(basename "$OUT" .ckpt)"

# ---- ASLR -----------------------------------------------------------------
NORAND="setarch -R"
ASLR=off
if ! setarch -R true 2>/dev/null; then
    [ "${RMC_ALLOW_ASLR:-0}" = 1 ] || die "setarch -R does not work on this machine (Docker?). Generate outside the container, or set RMC_ALLOW_ASLR=1 knowing the checkpoint will not match the others."
    log_warn "generating with ASLR enabled (RMC_ALLOW_ASLR=1)"
    NORAND=""; ASLR=on
fi

# ---- Binario ----------------------------------------------------------------
case "$1" in
    */*) BINPATH="$(cd "$DIR" && realpath -e "$1" 2>/dev/null)" ;;
    *)   BINPATH="$(command -v "$1" 2>/dev/null)" ;;
esac
[ -n "$BINPATH" ] && [ -f "$BINPATH" ] || die "binary $1 not found (relative to $DIR)"
if readelf -lW "$BINPATH" 2>/dev/null | grep -q "program interpreter"; then
    KIND=dynamic
    # Solo la columna de la instruccion: en el texto completo tambien salen
    # nombres de simbolos (gcc_r tiene gen_avx_vzeroupper, vzeroupper_operation...)
    n=$(objdump -d --no-show-raw-insn "$BINPATH" 2>/dev/null | awk -F'\t' 'NF >= 2 {print $2}' \
        | grep -cE '%ymm|%zmm|^(vpbroadcast|vzeroupper|vmovdq|bextr|shlx|sarx|shrx)')
    [ "${n:-0}" -eq 0 ] || die "$BINPATH contains $n AVX/BMI2 instructions that gem5 SE does not implement (rebuild without AVX, e.g. specs/config/gem5_noavx.cfg)"
else
    die "$BINPATH is a static binary: LD_PRELOAD does not apply, so no checkpoint can be taken. Link it dynamically (if it calls ckpt_dump(), link src/dumper.c and src/dumper_asm.S into a PIE)."
fi

# ---- Ejecucion --------------------------------------------------------------
ENVV=(GLIBC_TUNABLES="$RMC_NOAVX_TUNABLES" LD_BIND_NOW=1
      LD_PRELOAD="$LIBCKPT" CKPT_OUTPUT="$OUT")
TRIGGER="manual (ckpt_dump / SIGUSR1)"
if [ -n "$NS" ]; then
    ENVV+=(CKPT_AFTER_NS="$NS"); TRIGGER="after_ns=$NS"
fi
if [ -n "$SYM" ]; then
    ENVV+=(CKPT_AT_SYMBOL="${SYM%%:*}")
    [[ "$SYM" == *:* ]] && ENVV+=(CKPT_AT_SYMBOL_CALL="${SYM##*:}")
    TRIGGER="symbol=$SYM"
fi

rm -f "$OUT" "$OUT.tmp" "$OUT.log" "$OUT.stdout" "$OUT.inspect" "$OUT.meta"
log_info "$NAME: $* (in $DIR, $TRIGGER)"
# exec: el PID de fondo es el propio programa (setarch y env hacen exec)
( cd "$DIR" && exec $NORAND env "${ENVV[@]}" "$@" ) < "$STDIN" > "$OUT.stdout" 2> "$OUT.log" &
PID=$!

ticks=0; limit=$((TIMEOUT * 5)); timed_out=0
while kill -0 "$PID" 2>/dev/null; do
    [ "$WAIT" = 0 ] && [ -f "$OUT" ] && break       # volcado completo
    if [ "$ticks" -ge "$limit" ]; then timed_out=1; break; fi
    sleep 0.2; ticks=$((ticks + 1))
done
if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null
fi
wait "$PID" 2>/dev/null
rm -f "$OUT.tmp"

fail() {
    log_error "$NAME: $*"
    tail -n 15 "$OUT.log" | sed 's/^/        /' >&2
    exit 1
}
[ "$timed_out" = 0 ] || fail "no checkpoint after ${TIMEOUT}s"
[ -f "$OUT" ]        || fail "the program exited without writing the checkpoint"
grep -q "Dump complete" "$OUT.log" || fail "the dump did not complete"

python3 "$REPO/tools/ckpt_inspect.py" "$OUT" > "$OUT.inspect" 2>&1 \
    || fail "tools/ckpt_inspect.py cannot read it"
if grep -q '!!' "$OUT.inspect"; then
    grep '!!' "$OUT.inspect" >&2
    fail "invalid checkpoint"
fi
LOADER_REC="$(sed -n 's/.*loader recomendado: build\/\([a-z_]*\).*/\1/p' "$OUT.inspect")"
HEAP_END="$(sed -n 's/^heap_end=\(0x[0-9a-f]*\).*/\1/p' "$OUT.inspect")"

{
    printf 'RMC_CKPT=%q\n'     "$OUT"
    printf 'RMC_CWD=%q\n'      "$DIR"
    printf 'RMC_CMD=%q\n'      "$*"
    printf 'RMC_STDIN=%q\n'    "$STDIN"
    printf 'RMC_KIND=%q\n'     "$KIND"
    printf 'RMC_TRIGGER=%q\n'  "$TRIGGER"
    printf 'RMC_ASLR=%q\n'     "$ASLR"
    printf 'RMC_TUNABLES=%q\n' "$RMC_NOAVX_TUNABLES"
    printf 'RMC_BIND_NOW=1\n'
    printf 'RMC_HEAP_END=%q\n' "$HEAP_END"
    printf 'RMC_LOADER=%q\n'   "$LOADER_REC"
    printf 'RMC_HOST=%q\n'     "$(hostname)"
    printf 'RMC_DATE=%q\n'     "$(date -Is)"
    printf 'RMC_COMMIT=%q\n'   "$(git -C "$REPO" describe --always --dirty 2>/dev/null || echo unknown)"
} > "$OUT.meta"

log_info "OK $NAME: $(du -h "$OUT" | cut -f1), $KIND, heap_end=$HEAP_END -> $LOADER_REC"
exit 0
