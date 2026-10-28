# Proposal: Cambios en el lenguaje V para mejorar vlambda y vcraft

## Motivación

vlambda es un servidor ASGI-like para lambdas Python escrito en V, usando vcraft para el
bridge V↔Python. Tras optimizar vcraft y vlambda (caching, pool, GIL scope, arena allocator,
batch marshalling, orjson, free-threaded Python), los cuellos de botella restantes están
en el propio lenguaje V y en CPython. Este documento propone cambios en V que desbloquearían
mejoras de 10-1000x en vlambda.

---

## 1. Async/await nativo (coroutines)

### Motivación
V no tiene `async`/`await`. vlambda usa un thread por worker (modelo thread-per-request).
Esto limita la concurrencia al número de cores (típicamente 4-16 threads). Con async,
vlambda podría manejar 100K+ conexiones con 4 threads (modelo nginx/io_uring).

### Propuesta
- Añadir keywords `async` y `await` a V.
- Una función `async fn` retorna un `Future[T]` (o `Promise[T]`).
- `await` suspende la coroutine sin bloquear el thread.
- Runtime: scheduler cooperativo basado en epoll/io_uring (Linux) o kqueue (macOS).
- `spawn async fn()` lanza una coroutine en el scheduler.

### Impacto en vlambda
- Concurrencia: de ~3.000 req/s (threads) a ~100.000+ req/s (async).
- Elimina el modelo thread-per-worker y el GIL contention.
- Permite I/O no bloqueante para SQLite, HTTP, etc.

### Esfuerzo estimado
6+ meses (cambio en el lenguaje). Requiere rediseño del runtime de V.

---

## 2. Connection pool en `db.sqlite`

### Motivación
`db.sqlite` no tiene pool de conexiones. Cada request abre y cierra conexión (`init_db` +
`db.close()`). Esto cuesta ~0.2ms/req y es un cuello de botella para high-throughput.

### Propuesta
- Añadir `sqlite.Pool` con `pool.get()` / `pool.put()`.
- Conexiones reutilizadas entre requests, con `busy_timeout` y WAL mode por defecto.
- API: `mut pool := sqlite.new_pool(path, max_connections: 8)`.

### Impacto en vlambda
- +10-20% throughput (elimina open/close por request).
- Ya implementado en vlambda con un pool Python, pero nativo sería más rápido.

### Esfuerzo estimado
1-2 semanas (módulo stdlib).

---

## 3. SIMD en el runtime de strings

### Motivación
V's string operations (copy, compare, search) son byte-by-byte. Para strings largos
(respuestas JSON, bodies), esto es un cuello de botella. CPython 3.11+ usa SIMD para
algunas operaciones de strings.

### Propuesta
- Usar intrínsecos SIMD (AVX2/NEON) en `strings` y `bytes` modules.
- `strings.contains`, `strings.index`, `bytes.copy` con SIMD.
- Detección automática de capacidades CPU (runtime dispatch).

### Impacto en vlambda
- +5-10% para lambdas que procesan strings grandes.
- Mejora general para todo código V.

### Esfuerzo estimado
2-3 semanas (módulo stdlib).

---

## 4. io_uring para I/O de red

### Motivación
V's `net.http` usa epoll/kqueue. io_uring (Linux 5.1+) es 2-3x más rápido para
I/O-bound workloads porque elimina syscalls por operación.

### Propuesta
- Añadir backend io_uring a `net` module (Linux only).
- `net.listen_tcp_uring()` como alternativa a `net.listen_tcp()`.
- Integrar con el scheduler async (propuesta #1).

### Impacto en vlambda
- +20-40% en throughput HTTP para requests pequeños.
- Reduce latencia de red.

### Esfuerzo estimado
4-6 semanas (módulo net).

---

## 5. Arena allocator en el runtime

### Motivación
V's GC aloca y libera memoria individualmente. Para requests cortos (<1ms), el GC
no tiene tiempo de acumular basura significativa, pero el overhead de alloc/free
por objeto es significativo.

### Propuesta
- Añadir `mem.Arena` al runtime de V.
- `arena := mem.new_arena(65536)` crea un arena de 64KB.
- `arena.alloc(size)` aloca en el arena (bump allocator, O(1)).
- `arena.free()` libera todo de golpe.
- Integrar con el GC: objetos en arena no se rastrean individualmente.

### Impacto en vlambda
- +5-10% (reduce overhead de alloc/free).
- Ya implementado en vcraft como `vcraft.Arena`, pero nativo sería más rápido.

### Esfuerzo estimado
2-3 semanas (runtime).

---

## 6. Huge pages support

### Motivación
V's allocator usa páginas de 4KB. Para workloads memory-heavy, esto causa TLB misses.
Páginas de 2MB (huge pages) reducen TLB misses en ~95%.

### Propuesta
- `mem.set_huge_pages(true)` para habilitar huge pages.
- `mem.alloc_huge(size)` para alocar con huge pages.
- Requiere configuración del kernel (`vm.nr_hugepages`).

### Impacto en vlambda
- +3-5% para workloads memory-heavy.
- Mejora general para todo código V.

### Esfuerzo estimado
1-2 semanas (runtime).

---

## 7. Profile-Guided Optimization (PGO) en el compilador

### Motivación
V compila con optimizaciones genéricas (-O2). PGO usa datos de profiling para
optimizar ramas calientes, inlining, y layout de código.

### Propuesta
- `v -pgo profile.txt` para compilar con PGO.
- `v -pgo-generate` para generar datos de profiling.
- Workflow: compilar con -pgo-generate, ejecutar carga, recompilar con -pgo.

### Impacto en vlambda
- +10-20% throughput (mejor branch prediction, inlining).
- Mejora general para todo código V.

### Esfuerzo estimado
2-3 semanas (compilador).

---

## 8. Link-Time Optimization (LTO)

### Motivación
V compila módulo por módulo. LTO permite inlining cross-module y eliminación de
código muerto a nivel de todo el programa.

### Propuesta
- `v -lto` para habilitar LTO.
- Integrar con clang/gcc LTO flags.

### Impacto en vlambda
- +5-15% throughput (mejor inlining, menos overhead de llamadas).
- Mejora general para todo código V.

### Esfuerzo estimado
1-2 semanas (compilador).

---

## 9. String interning en el runtime

### Motivación
V strings son inmutables pero no interned. Strings idénticos (ej: "application/json",
"GET", "POST") se alocan múltiples veces.

### Propuesta
- `strings.intern(s)` retorna un string canónico (interned).
- El runtime interna automáticamente strings literales y strings cortos.
- Similar a Java's String.intern() o Python's sys.intern().

### Impacto en vlambda
- +5-10% (reduce allocations, mejora cache locality).
- Mejora general para todo código V.

### Esfuerzo estimado
1-2 semanas (runtime).

---

## 10. Object pooling en el runtime

### Motivación
V no tiene object pooling built-in. Cada `map[string]X` o `[]X` aloca memoria nueva.
Para requests que crean muchos objetos (dicts, listas), esto es costoso.

### Propuesta
- `mem.Pool[T]` genérico para reutilizar objetos.
- `pool := mem.new_pool[MyStruct](100)` crea un pool de 100 objetos.
- `obj := pool.get()` obtiene un objeto (reutilizado o nuevo).
- `pool.put(obj)` devuelve el objeto al pool.

### Impacto en vlambda
- +5-10% (reduce GC pressure).
- Mejora general para todo código V.

### Esfuerzo estimado
1-2 semanas (runtime, genéricos).

---

## 11. SIMD JSON en el runtime

### Motivación
V's `json` module parsea byte-by-byte. Para JSON grande, esto es un cuello de botella.
simdjson es 2-4x más rápido.

### Propuesta
- Integrar simdjson como backend opcional para `json` module.
- `json.parse_simd(data)` para parsing acelerado.
- Detección automática de capacidades CPU.

### Impacto en vlambda
- +20-30% para lambdas que parsean JSON grande.
- Mejora general para todo código V.

### Esfuerzo estimado
2-3 semanas (módulo json).

---

## 12. Memory-mapped file I/O

### Motivación
V's `os.read_file` lee el archivo completo en memoria. Para archivos grandes, esto
es costoso y fragmenta la memoria.

### Propuesta
- `os.mmap_file(path)` retorna un `[]u8` backed por mmap.
- Zero-copy read: los datos no se cargan en RAM hasta que se acceden (page cache).
- `os.munmap(data)` libera el mapping.

### Impacto en vlambda
- +50% para lambdas que leen archivos grandes.
- Mejora general para todo código V.

### Esfuerzo estimado
1 semana (módulo os).

---

## Resumen de priorización

| # | Cambio | Esfuerzo | Impacto vlambda | Impacto general |
|---|--------|----------|-----------------|-----------------|
| 1 | Async/await nativo | 6+ meses | +100-1000x concurrencia | Transformador |
| 2 | Connection pool SQLite | 1-2 semanas | +10-20% | Alto |
| 3 | String interning | 1-2 semanas | +5-10% | Alto |
| 4 | PGO en compilador | 2-3 semanas | +10-20% | Alto |
| 5 | LTO en compilador | 1-2 semanas | +5-15% | Alto |
| 6 | SIMD strings | 2-3 semanas | +5-10% | Medio |
| 7 | Arena allocator | 2-3 semanas | +5-10% | Medio |
| 8 | Object pooling genérico | 1-2 semanas | +5-10% | Medio |
| 9 | SIMD JSON | 2-3 semanas | +20-30% | Medio |
| 10 | io_uring para net | 4-6 semanas | +20-40% | Medio |
| 11 | Huge pages | 1-2 semanas | +3-5% | Bajo |
| 12 | Memory-mapped file I/O | 1 semana | +50% (files) | Medio |

## El cambio más transformador

**Async/await nativo** es el cambio que más impacto tendría. Con async, vlambda pasaría
de ~3.000 req/s (threads) a ~100.000+ req/s (async), porque:
- Un thread puede manejar miles de conexiones concurrentes (modelo nginx).
- No hay GIL contention (cada coroutine cede el control cooperativamente).
- I/O no bloqueante (DB, HTTP, filesystem) sin threads adicionales.
- Escalado lineal con cores sin overhead de context switching.

Esto es un cambio en el lenguaje V, no en vcraft. Pero es el que desbloquea el
siguiente orden de magnitud de performance.

## El cambio más práctico

**String interning + Connection pool + PGO/LTO** son los cambios de mayor ROI:
- Esfuerzo total: ~4-6 semanas.
- Impacto combinado: +25-45% throughput.
- Mejoran todo el ecosistema V, no solo vlambda.
- No requieren cambios arquitectónicos en V.
