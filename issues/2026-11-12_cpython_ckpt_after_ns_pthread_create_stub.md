# `CKPT_AFTER_NS` no genera checkpoint en `714.cpython_r` (SPEC CPU2026): el `pthread_create` del ejecutable tapa al de libc

Fecha: 2026-11-12 · Estado: **cerrado (2026-11-18)**, arreglado en `src/libckpt.c` · Afecta a: `src/libckpt.c` (temporizador), `launch_scripts/gen_ckpt.sh -t`

## Síntoma

`gen_ckpt.sh -t <ns>` (y por tanto `regenerate_ckpt_noavx.sh` con `BENCH_NS`) sobre
`714.cpython_r` termina con:

```
[gen] FALLO dump_cpython_r_noavx: el programa terminó sin generar el checkpoint
```

`libckpt` imprime `[libckpt] Timer thread started (N ns).` y el programa corre
hasta el final (~12 s con la entrada `train`) sin volcar nada, para cualquier
instante (se probó 8,4 s, 6,5 s y 3 s). El resto de benchmarks de CPU2026
generados con `-t` (21 de 22) funcionan.

## Causa

El temporizador de `src/libckpt.c` (`timer_thread`, lanzado desde `libckpt_init`
con `pthread_create`) nunca llega a existir:

- El `cpython_r` de SPEC se compila sin hilos y **define y exporta sus propios
  `pthread_create`, `pthread_attr_init`... de pega**
  (`benchspec/CPU/714.cpython_r/src/cpython/Include/cpython/pthread_stubs.h`:
  "pthread_create() fails"; el resto no hacen nada y devuelven 0).
- El ejecutable va antes que `libckpt.so` en el orden de resolución de símbolos,
  así que la llamada a `pthread_create` de `libckpt` cae en esa versión, que
  devuelve error sin crear ningún hilo.
- `libckpt_init` no comprueba el valor devuelto y escribe "Timer thread started"
  igualmente, por eso el fallo es silencioso.

## Evidencia

- `strace -f -e trace=clone,clone3` con `CKPT_AFTER_NS=2000000000`: un `clone3`
  (el hilo del temporizador) en `omnetpp_r`, **ninguno** en `cpython_r`.
- `nm -D cpython_r_base.test_compilacion-m64 | grep pthread_create` →
  `T pthread_create` (definido en el propio ejecutable).
- Con `CKPT_AFTER_NS` en `cpython_r`, `/proc/<pid>/task` tiene un solo hilo y
  `SigCgt` incluye SIGUSR1 (el manejador está instalado).
- El volcado manual funciona: `kill -USR1 <pid>` genera el `.ckpt` (96 MB con
  el volcado de prueba a los 4 s).

## Solución provisional (así se generó el `.ckpt` de cpython)

Lanzar con `gen_ckpt.sh` **sin `-t`** y enviar `SIGUSR1` desde fuera, contando
desde el arranque:

```sh
launch_scripts/gen_ckpt.sh -o <dir>/dump_cpython_r_noavx.ckpt -C <rundir> -i <stdin> \
    -- ./cpython_r_base.test_compilacion-m64 <args...> &
sleep 8.4; kill -USR1 "$(pgrep -x cpython_r_base.)"; wait
```

`pgrep -x` con el nombre truncado a 15 caracteres (`cpython_r_base.`): no usar
`pgrep -f`, que casa también con el `bash` envoltorio. El instante tiene la
precisión del `sleep` (décimas de segundo), suficiente para un ROI en meseta.

## Resolución (2026-11-18)

Arreglo aplicado: temporizador POSIX de un solo disparo en lugar del hilo.
`arm_dump_timer()` (`src/libckpt.c`) hace `timer_create(CLOCK_MONOTONIC, SIGEV_THREAD_ID,
SIGUSR1)` dirigido al TID del kernel del hilo principal (`gettid()`) y
`timer_settime` relativo; se arma en el constructor, después de instalar el
`sigaction(SIGUSR1)`. Ya no se crea ningún hilo, así que un `pthread_create` del
ejecutable no puede afectarlo.

- Si el temporizador no se puede armar, `arm_dump_timer` loguea qué llamada falló y la
  causa (`errno`), y el constructor sale con `_exit(1)`: `gen_ckpt.sh` lo ve como FALLO
  al momento. Ya no es silencioso.
- Se conserva comentado, como referencia, el `timer_thread` antiguo.
- Test de regresión: `test/test_pthread_stub.c` (redefine `pthread_create` para que
  devuelva `EAGAIN`) y la sección «pthread_create falla» de `test/run_native_tests.sh`:
  el `.ckpt` se genera y se restaura (`STUB DONE`).
- Hitos verificados: `strace -f` muestra `rt_sigaction(SIGUSR1)`, `timer_create` y
  `timer_settime` (1 s), ningún `clone`/`clone3` y `SIGUSR1 {si_code=SI_TIMER}`; con
  `CKPT_AFTER_NS` pequeño no mata el proceso; `nm -D --undefined-only build/libckpt.so
  | grep -i pthread` vacío; `make` sin avisos.
- Fuera de alcance de este cierre: regenerar y comparar los checkpoints de SPEC con
  `-t` (cpython, omnetpp) y la decisión sobre los 21 checkpoints `-t` antiguos, que se
  tratan aparte; el `nm -D` del binario de cpython en altek no se ha hecho.
- El fallo intermitente de la sección «signal inside malloc» del runner no es de este
  arreglo: ver `2026-11-16_runner_malloc_ventana_aleatoria_intermitente` y
  `2026-11-16_test_signal_malloc_monohilo_sin_lock`.
- Guía seguida: `test/GUIA_ARREGLO_TEMPORIZADOR.md`.

## Arreglo propuesto (histórico; ver «Resolución»)

No depender de un hilo del programa para el temporizador. Opciones:

1. En `libckpt_init`, armar el volcado con `timer_create(CLOCK_MONOTONIC, SIGEV_SIGNAL
   SIGUSR1)` o `setitimer(ITIMER_REAL)`: el manejador ya corre en el hilo
   principal (que es lo que quiere `do_dump_from_ucontext`) y no hay hilo que
   crear. Es la más limpia.
2. Resolver la función real con `dlsym(RTLD_NEXT, "pthread_create")` (salta al
   ejecutable porque va antes que `libckpt.so`).
3. Crear el hilo con la llamada `clone` directa, sin pasar por libc.

En cualquier caso, comprobar el valor devuelto y avisar con un mensaje claro
si el temporizador no se pudo armar, para que el fallo no sea silencioso.

## Cómo reproducir

```sh
# SPEC CPU2026 instalado (azha: ~/proyectos_personales/rmc_spec2026/specs2026), cpython_r compilado
cd <rundir de 714.cpython_r>
LD_PRELOAD=<repo>/build/libckpt.so CKPT_OUTPUT=/tmp/x.ckpt CKPT_AFTER_NS=3000000000 \
    ./cpython_r_base.test_compilacion-m64 -I -B coreml_pb.py -i 10 -a -c -m PoseNetMobileNet100S16FP16.mlmodel
ls /tmp/x.ckpt    # no existe
```
