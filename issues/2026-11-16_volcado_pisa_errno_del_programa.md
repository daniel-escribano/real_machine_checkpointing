# El volcado por señal cambia el `errno` del programa, en la ejecución nativa y dentro del `.ckpt`

Fecha: 2026-11-16 · Estado: **cerrado (2026-11-19)** · Afecta a: `src/libckpt.c` (manejadores de SIGUSR1/SIGTRAP), `src/dumper.c` (`ckpt_dump_impl`)

Severidad: **alta** · Confianza: **confirmado** (reproducido) · Dónde: `src/libckpt.c:144-148`, `src/libckpt.c:214-234`, `src/dumper.c:218` (`fcntl` → EBADF), `src/dumper.c:293-303` (`write` → EFAULT), `src/dumper.c:326`

## Síntoma

Un programa de prueba fija `errno = 77` y comprueba en bucle que no cambia. Con
`gen_ckpt.sh -t 200000000`:

```
ejecución nativa tras el volcado:  errno pisado: 77 -> 14   (EFAULT)
restaurado con loader y loader_pie: errno pisado: 77 -> 9    (EBADF)
```

El valor guardado en el `.ckpt` en la dirección de `errno` es 9 en 3 de 3
checkpoints. Es decir: **todo checkpoint generado con `-t`, `-s` o `SIGUSR1`
arranca con un `errno` que el programa no tenía**. (En una primera prueba, sin la
línea que imprime `&errno`, un `.ckpt` restauró con `errno` intacto; no lo pude
explicar ni repetir.)

## Causa

- `errno` es una variable por hilo (TLS). El manejador interrumpe al programa en
  un punto arbitrario, por ejemplo entre una llamada que ha fallado y la lectura
  de `errno`.
- El volcado hace llamadas que fallan a propósito y escriben `errno`:
  `parse_fds` prueba `fcntl(fd, F_GETFL)` sobre los descriptores 0-255
  (EBADF en los cerrados), y `dump_region_data` recibe EFAULT al escribir
  páginas ilegibles ([vvar], páginas de guarda).
- Ningún manejador guarda y restaura `errno` (`signal-safety(7)`, apartado
  "errno": un manejador solo es seguro si lo guarda al entrar y lo restaura al
  salir).
- Además, la página TLS que contiene `errno` se escribe en el fichero **después**
  de esas llamadas, así que la imagen lleva el `errno` del dumper. Restaurar al
  salir del manejador arregla la ejecución nativa, pero no la imagen.

## Resolución (2026-11-19)

Aplicada la opción recomendada (1 + 2):

- **Manejadores (`src/libckpt.c`):** `sigusr1_handler` y `sigtrap_rearm_or_hook` copian
  `errno` a una local lo primero y lo restauran antes de cada salida (en el segundo, las
  dos: la del re-armado por TF y la del punto de entrada normal; `sigtrap_handler`, que solo
  se llama desde ahí, queda cubierto).
- **Volcado (`src/dumper.c`):** `ckpt_dump_impl` copia `errno` en `local_errno` antes del
  primer `ckpt_log` y lo restaura en la salida de éxito. La función nueva
  `patch_errno_in_image` localiza la región que contiene `&errno` (el del hilo en curso, que
  en el camino por señal es el interrumpido), y sobrescribe con `pwrite` los 4 bytes de la
  imagen con el `errno` original, tras el bucle de payloads y antes del `pwrite` de los
  descriptores. Si no encuentra la región o no tiene payload, solo avisa con `WARNING`.
- **Decisión de diseño:** en los caminos de fallo (`return -1` tempranos y `goto fail`) no
  se restaura `errno`: quien llama a `ckpt_dump()` y recibe `-1` ve el `errno` de la causa
  (contrato clásico de libc). Hoy nadie lo lee tras el `-1`.
- **Verificado** con una sonda con `errno = 77` vigilado por puntero volatile:
  - estático por `ckpt_dump()`: `errno tras=77` (antes 14) y 77 dentro del `.ckpt`;
  - por señal con `-t`: nativo sin `77 -> 14`, `.ckpt` con 77 en `&errno`, y restaurado con
    `loader` y con `loader_pie` sin cambios (antes `77 -> 9`);
  - los controles con el código original sí reprodujeron el fallo;
  - fallo forzado (directorio inexistente): `ckpt_dump=-1`, `errno=ENOENT`, sin `.tmp`;
  - `make` sin avisos (`-Wall -Wextra`, también el camino estático) y
    `test/run_native_tests.sh 3` sin fallos (13 pasados).
- **Sin verificar:** fallos tardíos (`fsync`, `rename`) y una región SKIP que contenga
  `&errno`.

## Qué se hizo

- Programa de prueba (`errno_probe.c` en el scratchpad de la auditoría) y
  lectura directa del `.ckpt` en la dirección de `errno` con un script Python.
- Corrección de referencia probada: tras aplicarla, nativo y restaurado
  (`loader` y `loader_pie`) dan `errno intacto`; `test/run_native_tests.sh 3`
  sin fallos nuevos.

## Opciones

1. Guardar `errno` al entrar en cada manejador y restaurarlo antes de cada
   `return` (arregla la ejecución nativa).
2. Además, en `ckpt_dump_impl`, copiar `errno` como primera instrucción y, una
   vez escritas las regiones, sobrescribir con `pwrite` los 4 bytes de `errno`
   dentro de la región que lo contiene (arregla la imagen). Es la opción
   recomendada (1 + 2).
3. Alternativa a 2: que el camino de volcado no use envoltorios de libc que
   escriben `errno` (syscalls en ensamblador). Mucho más código y frágil.

Nota de concepto: [`dudas/senales/errno-en-manejadores-de-senal.md`](../dudas/senales/errno-en-manejadores-de-senal.md)
· Corrección de referencia: [`dudas/correcciones/volcado_pisa_errno_del_programa/`](../dudas/correcciones/volcado_pisa_errno_del_programa/)
