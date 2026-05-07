# UTN FRC Monitor

Bot que monitorea automáticamente tu cuenta de **UTN FRC Autogestión** y te
notifica por **Telegram** cada vez que hay novedades. Pensado para correr
desatendido (cron / task scheduler / GitHub Actions / Raspberry Pi) cada 15
minutos.

## ¿Qué notifica?

1. **Mensajes de docentes** publicados en NOTAS (avisos de cátedra,
   suspensiones, cambios de aula, etc.) leídos desde el iframe `tipo=NOTAS` de
   Autogestión 3.
2. **Avisos del timeline de Autogestión 4** (encuestas, invitaciones,
   adjuntos globales) — incluyendo los archivos adjuntos, que se reenvían
   directamente al chat de Telegram.
3. **Calificaciones nuevas o modificadas** en cada materia que tengas
   inscripta del año vigente. Detecta tanto cargas nuevas como cambios sobre
   notas ya cargadas.

## Stack

- Python 3.10+
- [Playwright](https://playwright.dev/python/) (Chromium headless) — para el
  login con JavaScript de UTN.
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — parsers
  del HTML del iframe a3 y del timeline a4.
- [requests](https://requests.readthedocs.io/) — Telegram Bot API y descarga
  autenticada de adjuntos.
- API HTTP de [Telegram Bot](https://core.telegram.org/bots/api).

## Setup paso a paso

### 1. Cloná el repo

```bash
git clone https://github.com/<tu-usuario>/utn-frc-monitor.git
cd utn-frc-monitor
```

### 2. Instalá las dependencias

Recomendado en un virtualenv:

```bash
python -m venv venv
# Linux/Mac:
source venv/bin/activate
# Windows:
venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

### 3. Creá el bot de Telegram

1. Abrí Telegram y hablale a [@BotFather](https://t.me/BotFather).
2. Mandale `/newbot` y seguí las instrucciones (nombre + username terminado en
   `bot`).
3. BotFather te va a devolver un **token** del estilo
   `123456789:ABCdefGhI...`. Guardalo, ese es tu `TELEGRAM_TOKEN`.
4. **Importante:** mandale al menos un mensaje al bot recién creado (un `/start`
   alcanza). El script descubre tu `chat_id` la primera vez via `getUpdates`,
   y para que aparezca tiene que haber al menos un mensaje tuyo en la cola.

### 4. Configurá tus credenciales

```bash
cp .env.example .env
```

Editá `.env` y completá:

- `UTN_USER` — tu legajo.
- `UTN_PASS` — tu contraseña de Autogestión.
- `TELEGRAM_TOKEN` — el token que te dio BotFather.
- `UTN_DOMAIN` — opcional, default `sistemas` (es lo correcto para FRC).

> El script carga las variables desde el entorno. Si alguna falta, falla con
> un mensaje claro indicando cuál — no hay valores hardcodeados de fallback.

Para que Python las lea desde el `.env`, podés:

- exportarlas en tu shell antes de correr (`source .env` con un `.env` que
  use `KEY=VAL` simples y prefijes con `export`),
- o instalar [`python-dotenv`](https://pypi.org/project/python-dotenv/) y
  agregar `from dotenv import load_dotenv; load_dotenv()` arriba del script,
- o pasarlas inline (`UTN_USER=... UTN_PASS=... python utn_monitor_v3.py`),
- o setearlas en el cron / scheduler / workflow.

### 5. Primera corrida

```bash
python utn_monitor_v3.py
```

La primera ejecución:

- Detecta tu `chat_id` automáticamente y lo guarda en
  `utn_telegram_chat_id.txt` (queda local, ignorado por git).
- Crea `utn_seen_messages.json` con los IDs de los mensajes actuales (no te
  reenvía mensajes históricos al pasado).
- Crea `utn_grades_state.json` con el snapshot inicial de tus notas. Esta
  primera corrida **no notifica notas** — solo guarda el estado base. A partir
  de la segunda corrida, te avisa por cada nota nueva o modificada.

## Opciones de deploy

El script está pensado para correr en una sola corrida y salir, así que
funciona con cualquier scheduler:

### Cron en Linux / VPS / Raspberry Pi

```cron
*/15 * * * * cd /ruta/al/repo && /ruta/al/venv/bin/python utn_monitor_v3.py >> utn_monitor_v3.log 2>&1
```

Si usás `.env`, asegurate de cargarlo en el cron (cron no hereda tu shell):

```cron
*/15 * * * * cd /ruta/al/repo && set -a && . ./.env && set +a && /ruta/al/venv/bin/python utn_monitor_v3.py >> utn_monitor_v3.log 2>&1
```

### Windows Task Scheduler

Creá una tarea que corra cada 15 min con:

- Programa: `C:\ruta\al\venv\Scripts\python.exe`
- Argumentos: `utn_monitor_v3.py`
- Iniciar en: `C:\ruta\al\repo`
- En la pestaña **Configuración** → marcar “Ejecutar tanto si el usuario
  inició sesión como si no”.
- Setear las variables de entorno (`UTN_USER`, `UTN_PASS`, `TELEGRAM_TOKEN`)
  como variables de usuario o de sistema en Windows, o cargarlas con un `.bat`
  wrapper.

### GitHub Actions

Podés correr el bot gratis en GitHub Actions con un cron schedule. Workflow
mínimo (`.github/workflows/monitor.yml`):

```yaml
name: utn-monitor
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt && playwright install --with-deps chromium
      - run: python utn_monitor_v3.py
        env:
          UTN_USER:       ${{ secrets.UTN_USER }}
          UTN_PASS:       ${{ secrets.UTN_PASS }}
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
```

Cargá las credenciales como **GitHub Secrets** en la pestaña Settings →
Secrets and variables → Actions del repo. Tener en cuenta: si usás Actions,
los archivos de estado (`utn_seen_messages.json`, `utn_grades_state.json`,
`utn_telegram_chat_id.txt`) **no persisten entre corridas** — vas a recibir
mensajes duplicados. Solucionable con un job que commitee el state a una rama
separada o usando `actions/cache`, pero queda como ejercicio.

## Cómo funciona internamente

- **Login:** Playwright carga `https://www.frc.utn.edu.ar/logon.frc`,
  completa los tres campos (`txtUsuario`, `pwdClave`, `txtDominios=sistemas`)
  y verifica que el redirect post-submit no vuelva al form (señal de que
  rechazó las credenciales).
- **Mensajes a3:** los avisos de docentes viven en un `<iframe>` con
  `tipo=NOTAS` embebido en el dashboard `academico3/defaultreduced.frc`. El
  script itera `page.frames` para encontrarlo y parsea el HTML del frame
  (`div.txtCmn.fCmn` con `<strong>` para metadata + `<blockquote>` para el
  cuerpo).
- **Timeline a4:** el dashboard `https://a4.frc.utn.edu.ar/4/` tiene un
  `<ul id="listaMensajes">` con `<li id="idMensaje{N}">` para cada item. El
  parser extrae autor, materia, fecha, cuerpo y adjuntos.
- **Dedup cruzado a3↔a4:** algunos mensajes de docente aparecen en ambas
  fuentes. Se calcula un set de IDs equivalentes para cada mensaje (hash MD5
  del header `materia+fecha+autor`, hash del cuerpo normalizado, e ID nativo
  `a4:<idMensaje>`); si cualquiera ya está en `utn_seen_messages.json`, no se
  reenvía.
- **Adjuntos:** los archivos del timeline a4 usan POSTs firmados con headers
  `A4-Token` / `A4-TimeStamp` / `A4-Data` que JS arma en el cliente, así que
  no se pueden reproducir desde `requests`. El script hace click en el `<a>`
  desde Playwright y captura la descarga (`page.expect_download`), después la
  reenvía a Telegram con `sendDocument`.
- **Notas (a4):** la lista de cursos sale del DOM (`<li id="idCurso{N}">` del
  panel “Materias (YYYY)”). Por cada curso se consultan dos endpoints:
  - `GET /4/academico/notas/titulos/{cursoId}` → texto pipe-separated con los
    nombres de las 12 columnas (ej: `1º Parc.|2º Parc.|...|.|.|.|.|.|.|`).
  - `GET /4/cursado/materias/notas/{cursoId}` → JSON con `nota1`..`notaN` +
    `notafinal`. Devuelve **HTTP 204** si no hay notas cargadas.

  El snapshot se compara contra `utn_grades_state.json`. Considera “sin nota
  cargada” a `0`, `0.00`, `.`, `-` y string vacío. Notifica `nueva` cuando
  pasa de vacío a un valor real, y `modificada` cuando cambia un valor ya
  cargado (corrección post-publicación).

## Limitaciones

- Solo monitorea las materias del **año vigente** que aparecen en el panel
  “Materias (YYYY)” del dashboard a4. Materias de años anteriores no se ven.
- No detecta notas antes de que UTN las publique en Autogestión — depende del
  delay con el que cada cátedra carga las calificaciones.
- El script asume el dominio `sistemas` por default (FRC). Otras facultades
  regionales tendrán URLs / dominios distintos y probablemente parsers
  distintos.
- Es **scraping**: si UTN rediseña Autogestión, los selectores se rompen y
  hay que actualizarlos. Si el sitio está caído (suele estar lento de
  madrugada), las corridas pueden fallar — el script reintenta hasta 3 veces
  por corrida y sale en silencio si todas fallan, así que no te spamea.
- El estado vive en archivos JSON en disco. Si los borrás o se corrompen,
  vas a recibir notificaciones duplicadas o, en el caso de las notas, una
  primera corrida “muda” mientras se rearma el snapshot.

## Disclaimer de seguridad

- **Nunca commitees tu `.env`.** Ya está en `.gitignore`. Si lo subiste por
  accidente, rotá la contraseña UTN y revocá el token Telegram con
  `/revoke` en BotFather inmediatamente.
- Las credenciales son **personales e intransferibles**. No compartas tu
  `.env`, ni copies el de un compa, ni hosteés este bot para terceros.
- El bot solo lee tu propia cuenta — no toca los datos de nadie más. Pero
  pensá dos veces dónde lo deployás: cualquier persona con acceso al server
  donde corre puede leer tu `.env` y, por lo tanto, tu cuenta.
- El uso del scraping puede chocar con los términos de uso de UTN FRC. Lo
  publicamos con fines educativos. Usalo bajo tu responsabilidad.

## Licencia

MIT — ver [LICENSE](LICENSE).

## Aporte

Si encontrás un bug, un selector que rompió, o se te ocurre una feature, abrí
un issue o un PR. Bienvenidos los aportes de otros estudiantes de UTN.
