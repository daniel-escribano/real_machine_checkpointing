# Guía: arreglar `CKPT_AFTER_NS` sustituyendo el hilo por un temporizador POSIX

**Estado: cerrada el 2026-11-18** (etapas 0-3 hechas; las 4 y 5 quedan fuera de esta guía).

Issue: [`issues/2026-11-12_cpython_ckpt_after_ns_pthread_create_stub.md`](../issues/2026-11-12_cpython_ckpt_after_ns_pthread_create_stub.md)

El código lo escribes tú. Esta guía da los ficheros, el pseudocódigo, las
pistas y un hito comprobable por etapa. Después de cada etapa ejecuta `make`
y `test/run_native_tests.sh 3`.

## Contexto: qué falla

`libckpt_init` (`src/libckpt.c`, bloque `if (g_after_ns > 0)`) crea el hilo
`timer_thread` con `pthread_create` sin comprobar lo que devuelve. En
`cpython_r` esa llamada no llega a la de libc: el ejecutable define y exporta
su propio `pthread_create`, que siempre falla, y el enlazador lo resuelve
antes que el de `libckpt.so`. No se crea ningún hilo y aun así el log dice
"Timer thread started".

Arreglo elegido: un temporizador POSIX (`timer_create` + `timer_settime`)
que entrega SIGUSR1 al hilo principal con `SIGEV_THREAD_ID`. No hace falta
ningún hilo y esas funciones no las puede tapar un stub de pthread.

Descartado:
- `SIGEV_SIGNAL` a secas: la señal va dirigida al proceso y, con varios
  hilos, podría capturar los registros de un hilo que no es el principal.
- `setitimer`: usa SIGALRM, que va dirigida al proceso y choca con `alarm()`.
- `SIGEV_THREAD`: glibc crea un hilo auxiliar.
- `dlsym(RTLD_NEXT)`: no funciona en `STATIC_BUILD`.
- `clone` a mano: el hilo no tiene TLS y no puede usar libc con seguridad.

## ¿Rompe algo de lo que ya existe?

Para el disparo con `-t`, el disparo manual con SIGUSR1 y el disparo por
símbolo, no cambia nada. Efectos secundarios:

1. **`test_signal_malloc` pierde cobertura sin avisar.** Sin el hilo del
   temporizador, glibc se salta el lock de malloc y el test deja de comprobar el
   deadlock dentro de malloc. Es un problema aparte, con su propio issue
   ([`test_signal_malloc_monohilo_sin_lock`](../issues/2026-11-16_test_signal_malloc_monohilo_sin_lock.md)):
   no forma parte de esta guía. La sección "signal inside malloc" del runner
   también falla a veces por otra causa (ventana aleatoria,
   [`runner_malloc_ventana_aleatoria_intermitente`](../issues/2026-11-16_runner_malloc_ventana_aleatoria_intermitente.md)).
2. **Los checkpoints nuevos hechos con `-t` no son iguales que los antiguos.**
   - Antes, el `pthread_create` sacaba a glibc del modo de un solo hilo
     (`__libc_single_threaded` pasa a falso y no vuelve atrás): malloc toma
     locks y libstdc++ usa atómicas en `shared_ptr`. Los nuevos se quedan en
     modo de un solo hilo, como los manuales con SIGUSR1.
   - El volcado ya no incluye la pila del hilo del temporizador (~8 MiB y una
     página de guarda): cambian el mapa de memoria y el tamaño.

   No mezcles un checkpoint viejo y uno nuevo del mismo benchmark en una misma
   comparación del TFM.
3. **Documentación que cita líneas concretas:** `docs/GUIA_ESTUDIO_RISCV.md:136`
   y la cabecera de `src/libckpt.c` (líneas 10-12 y 25).
4. **El mensaje "Timer thread started" se puede cambiar:** ningún script lo busca.

Sin cambios: el manejador, `SA_RESTART`, el comportamiento con `fork`/`exec`
(los temporizadores POSIX no se heredan y desaparecen con `exec`, igual que el
hilo), el hook por símbolo y `target_app` (usa `ckpt_dump()`, no el
temporizador).

---

## Etapa 0: reproducir el fallo sin SPEC

- [x] Hecho

**Fichero nuevo:** `test/test_pthread_stub.c`. **Fichero que tocas:**
`test/run_native_tests.sh`.

```
definir pthread_create con la firma exacta de <pthread.h>
    → no crea nada, devuelve EAGAIN
main:
    t0 ← reloj monotónico
    mientras (ahora - t0) < 3 s:
        trabajo que el compilador no pueda eliminar
    imprimir "STUB DONE"
```

- **La trampa:** que el ejecutable defina `pthread_create` no basta. Tiene que
  estar en la tabla de símbolos **dinámicos**; si no, el enlazador no se lo da
  a libckpt y el test pasaría sin probar nada. Busca la opción de gcc/ld que
  exporta los símbolos del ejecutable.
- **Prueba:** `nm -D ./test_pthread_stub | grep pthread_create` tiene que
  mostrar `T`.
- **En el runner:** añade una sección que genere el checkpoint con
  `gen -t 1000000000`, lo restaure con `loader_pie` y espere `STUB DONE`.

**Hito:** el test nuevo da **FAIL** con "terminó sin generar el checkpoint"
(el síntoma del issue) y `strace -f -e trace=clone3` no muestra ningún
`clone3`.

## Etapa 1: que el fallo se vea

- [x] Hecho

**Fichero:** `src/libckpt.c`, bloque `if (g_after_ns > 0)` del constructor.

```
rc ← pthread_create(...)
si rc ≠ 0: log "[libckpt] ERROR: ..." con la causa
si no:     log "started"
```

- **La pregunta a resolver:** ¿por qué aquí `perror`/`errno` dan una causa
  equivocada? Lee la sección RETURN VALUE de `man pthread_create`.

**Hito:** el test de la etapa 0 sigue en FAIL, pero el log dice ERROR y la
causa (`Resource temporarily unavailable`).

## Etapa 2: temporizador POSIX en lugar del hilo

- [x] Hecho

**Fichero:** `src/libckpt.c`, con una función nueva.

```
static int arm_dump_timer(ns):            // 0 = ok, -1 = error (errno válido)
    tid ← TID del KERNEL del hilo actual (no pthread_self)
    sev ← { notificación: SIGEV_THREAD_ID, señal: SIGUSR1, hilo: tid }
    si timer_create(CLOCK_MONOTONIC, &sev, &id) falla → return -1
    its ← { valor: ns partido en (s, ns), intervalo: 0 }    // un solo disparo
    si timer_settime(id, relativo, &its) falla:
        timer_delete(id); return -1
    return 0

constructor:
    ... sigaction(SIGUSR1) ...          // ¡ANTES que el temporizador!
    si g_after_ns > 0:
        si arm_dump_timer falla → log ERROR + causa, terminar con código ≠ 0
        si no → log "[libckpt] Timer armed (N ns, tid T)."
```

Pistas:
- Lee `man 2 timer_create` y `man 7 sigevent`, en la parte de
  `SIGEV_THREAD_ID`.
- Si el compilador no reconoce `sigev_notify_thread_id`, busca el campo real
  en `/usr/include/x86_64-linux-gnu/bits/types/struct_sigevent.h`.
- El orden importa: si armas el temporizador antes del `sigaction` y `ns` es
  muy pequeño, la acción por defecto de SIGUSR1 mata el proceso.
- Un `it_value` a cero **desarma** el temporizador en vez de dispararlo ya. El
  `if (g_after_ns > 0)` te protege; no lo quites.
- Terminar en el constructor es decisión tuya. Recomendado: `gen_ckpt.sh` ya
  convierte un código de salida distinto de cero en `FALLO` al momento, en vez
  de esperar a que acabe el programa.
- Con glibc < 2.34 (comprueba altek con `ldd --version`) `timer_create` está en
  librt y hay que añadir `-lrt` al enlazado de `libckpt.so` (línea 80 del
  `Makefile`) y al estático. Con glibc 2.39 no hace falta.
- Comprueba antes que cpython no tapa también estas funciones:
  `nm -D cpython_r_base.test_compilacion-m64 | grep -E 'timer_(create|settime)|gettid'`
  debería salir vacío.

**Hito:**
- El test de la etapa 0 pasa a **PASS**.
- Con `strace -f -e trace=clone3,timer_create,timer_settime` ves
  `timer_create` y `timer_settime`, ningún `clone3`, y aparece
  `--- SIGUSR1 {si_code=SI_TIMER...}`.
- `redzone`, `malloc` y `vdso` siguen pasando (el fallo intermitente de `malloc` es el issue
  `runner_malloc_ventana_aleatoria_intermitente`, no de esta guía).

## Etapa 3: limpieza

- [x] Hecho

**Ficheros:** `src/libckpt.c` y, si quieres, el `Makefile`.

- Borra `timer_thread`, `g_main_tid` y la llamada `pthread_self()`.
- Quita `<pthread.h>` si ya nada lo usa.
- Actualiza la cabecera (líneas 10-12 y 25).

**Hito:** `nm -D --undefined-only build/libckpt.so | grep -i pthread` sale
**vacío**. Eso prueba que un ejecutable con stubs de pthread ya no puede
afectar a libckpt. Si sigue saliendo algo, averigua qué fichero lo pide.

## Etapa 4: SPEC

- [x] Hecho (fuera de esta guía: se comprueba por otro lado)

1. Repite el comando de "Cómo reproducir" del issue con `cpython_r`: ahora
   tiene que aparecer `/tmp/x.ckpt`.
2. Genera de nuevo `omnetpp_r` con `-t` como control de regresión. Debería
   salir unos ~8 MiB más pequeño que antes (la pila del hilo que ya no está).
3. Genera de nuevo cpython con `-t 8400000000` y compara su ROI RIP con el del
   `.ckpt` manual. Prueba ese checkpoint solo antes de meterlo en una tanda.

## Etapa 5: decidir y documentar

- [x] Hecho (descartada: la decisión y la documentación se rehicieron aparte)

- **Decide qué haces con los 21 checkpoints `-t` antiguos** (punto 2 de
  "¿Rompe algo?"):
  - regenerarlos todos para que sean coherentes entre sí, o
  - dejarlos y anotar qué versión de libckpt generó cada uno.
- **Documenta:**
  - cierra el issue (`Estado`);
  - añade una entrada en `docs/BUG_FIXES.md`;
  - corrige `docs/GUIA_ESTUDIO_RISCV.md:136`.

Al acabar cada etapa, pasa el diff para revisarlo antes de seguir.
