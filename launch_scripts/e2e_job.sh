#!/bin/bash
# e2e_job.sh - Trabajo SLURM de la prueba de extremo a extremo.
#
# Lo crea y lo lanza launch_scripts/e2e_altek.sh desde el PC; no hace falta
# lanzarlo a mano:
#
#   sbatch --array=1-N ... e2e_job.sh RUN_DIR   una prueba por tarea (linea
#                                               SLURM_ARRAY_TASK_ID de tests.tsv)
#   e2e_job.sh RUN_DIR                          todas, una tras otra, y resumen
#   e2e_job.sh --summary RUN_DIR                solo el resumen
#
# RUN_DIR contiene repo/ (el repo compilado en el PC), ckpt/, work/,
# run.env (GEM5_BIN, REMAPS) y tests.tsv, una prueba por linea con campos
# separados por tabuladores:
#
#   nombre  modo(st|smt|genfail)  ckpts(a,b)  maxinsts  regex_esperada  minimo  regex_fallo  timeout_s
#
# Un campo vacio se escribe "-" (read fusiona tabuladores consecutivos).
# genfail = el checkpoint no se pudo generar en el PC: la prueba falla con el
# motivo que trae en regex_esperada.
#
# Cada prueba lanza gem5 y pasa si:
#   - gem5 termina con codigo 0 y antes del timeout,
#   - gem5 instala los checkpoints en initState (restauracion directa),
#   - no aparece ningun error (panic/fatal de gem5, ERROR/FATAL de la config,
#     corrupcion de malloc, violacion de segmento, regex_fallo),
#   - regex_esperada aparece al menos `minimo` veces en la salida.
#
# Resultados: RUN_DIR/results/<prueba>/{gem5.log,m5out/,result} y
# RUN_DIR/results/summary.txt (lo que e2e_altek.sh trae y muestra).
set -u
SUMMARY_ONLY=0
[ "${1:-}" = --summary ] && { SUMMARY_ONLY=1; shift; }
RUN="${1:?uso: e2e_job.sh [--summary] RUN_DIR}"
REPO="$RUN/repo"
RES="$RUN/results"
mkdir -p "$RES"
# shellcheck disable=SC1091
source "$RUN/run.env"

GENERIC_FAIL='^(panic|fatal):|\[rmc\] (ERROR|FATAL)|Segmentation fault|double free|corrupted|malloc\(\)|Aborted|Illegal instruction'

# Ejecuta una prueba y deja su veredicto en results/<nombre>/result:
# primera linea "PASS ..." o "FAIL ...", despues el detalle.
run_test() {   # run_test nombre modo ckpts maxinsts expect min failre tmo
    local name=$1 mode=$2 ckpts=$3 maxinsts=$4 expect=$5 min=$6 failre=$7 tmo=$8
    [ "$failre" = - ] && failre=""
    local d="$RES/$name"
    rm -rf "$d"; mkdir -p "$d"
    local -a cks cmd
    IFS=',' read -r -a cks <<< "$ckpts"
    local i
    for i in "${!cks[@]}"; do cks[$i]="$RUN/${cks[$i]}"; done

    case "$mode" in
        st)
            cmd=("$GEM5_BIN" --outdir="$d/m5out" "$REPO/gem5_configs/x86_st_timing.py"
                 --cmd="$REPO/build/loader" --options="${cks[0]} ${REMAPS:-}"
                 --cpu=atomic --maxinsts="$maxinsts")
            ;;
        smt)
            cmd=("$GEM5_BIN" --outdir="$d/m5out" "$REPO/gem5_configs/x86_mixed.py"
                 --loader="$REPO/build/loader" --ckpts "${cks[@]}"
                 --maxinsts="$maxinsts" --loader-opts="${REMAPS:-}")
            ;;
        genfail)
            printf 'FAIL  %s (0s): the checkpoint was not generated on the PC: %s\n' "$name" "$expect" > "$d/result"
            return
            ;;
        *)
            printf 'FAIL  %s (0s): unknown mode %s\n' "$name" "$mode" > "$d/result"
            return
            ;;
    esac

    local t0 rc secs log="$d/gem5.log"
    t0=$(date +%s)
    ( cd "$d" && timeout "$tmo" "${cmd[@]}" ) > "$log" 2>&1
    rc=$?
    secs=$(( $(date +%s) - t0 ))

    local reason="" bad got
    if [ "$rc" -eq 124 ]; then
        reason="timeout (${tmo}s)"
    else
        bad=$(grep -m1 -E "$GENERIC_FAIL" "$log")
        [ -z "$bad" ] && [ -n "$failre" ] && bad=$(grep -m1 -E "$failre" "$log")
        got=$(grep -cE "$expect" "$log")
        if ! grep -q "installed in initState" "$log"; then
            reason="gem5 did not install the checkpoint(s) in initState"
            [ -n "$bad" ] && reason="$reason: $bad"
        elif [ -n "$bad" ]; then
            reason="error in the output: $bad"
        elif [ "$rc" -ne 0 ]; then
            reason="gem5 exited with code $rc"
        elif [ "$got" -lt "$min" ]; then
            reason="'$expect' appears $got times (minimum $min)"
        fi
    fi

    local roi
    roi=$(grep -oE "ROI instructions executed: [0-9]+|thread [0-9]+ \([^)]*\): [0-9]+ ROI instructions" "$log" | tr '\n' ' ')
    if [ -z "$reason" ]; then
        printf 'PASS  %s (%ss) %s\n' "$name" "$secs" "$roi" > "$d/result"
    else
        {
            printf 'FAIL  %s (%ss): %s\n' "$name" "$secs" "$reason"
            printf '      log: %s\n' "$log"
            tail -n 8 "$log" | sed 's/^/      | /'
        } > "$d/result"
    fi
}

# summary.txt a partir de los result de cada prueba, en el orden de tests.tsv
summarize() {
    local sum="$RES/summary.txt" pass=0 fail=0 name rest
    {
        echo "rmc e2e: job ${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID:-local}} on $(hostname) $(date -Is)"
        echo "gem5: $GEM5_BIN"
        echo "remaps: ${REMAPS:-(none)}"
        echo ""
        while IFS=$'\t' read -r name rest; do
            [ -z "${name:-}" ] && continue
            [[ "$name" == \#* ]] && continue
            if [ -s "$RES/$name/result" ]; then
                cat "$RES/$name/result"
                if head -1 "$RES/$name/result" | grep -q '^PASS'; then pass=$((pass + 1)); else fail=$((fail + 1)); fi
            else
                echo "FAIL  $name: no result (the SLURM task did not finish: cancelled, out of memory or out of time; see results/slurm-*_N.out)"
                fail=$((fail + 1))
            fi
        done < "$RUN/tests.tsv"
        echo ""
        echo "RESULT: $pass passed, $fail failed"
    } > "$sum"
    touch "$RES/DONE"
    [ "$fail" -eq 0 ]
}

if [ "$SUMMARY_ONLY" = 1 ]; then
    summarize
    exit
fi

if [ -n "${SLURM_ARRAY_TASK_ID:-}" ]; then
    line=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$RUN/tests.tsv")
    [ -n "$line" ] || { echo "[e2e_job] FATAL: tests.tsv has no line $SLURM_ARRAY_TASK_ID" >&2; exit 1; }
    IFS=$'\t' read -r name mode ckpts maxinsts expect min failre tmo <<< "$line"
    echo "test $SLURM_ARRAY_TASK_ID: $name on $(hostname)"
    run_test "$name" "$mode" "$ckpts" "$maxinsts" "$expect" "$min" "$failre" "$tmo"
    cat "$RES/$name/result"
    head -1 "$RES/$name/result" | grep -q '^PASS'
    exit
fi

while IFS=$'\t' read -r name mode ckpts maxinsts expect min failre tmo; do
    [ -z "${name:-}" ] && continue
    [[ "$name" == \#* ]] && continue
    run_test "$name" "$mode" "$ckpts" "$maxinsts" "$expect" "$min" "$failre" "$tmo" < /dev/null
    head -1 "$RES/$name/result"
done < "$RUN/tests.tsv"
summarize
cat "$RES/summary.txt"
