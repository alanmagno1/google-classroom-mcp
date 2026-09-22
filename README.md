# google-classroom-mcp

Servidor [MCP](https://modelcontextprotocol.io) para Google Classroom, pensado para el
alumno y con soporte para varias cuentas de Google a la vez. Permite que Claude Code,
Claude Desktop, Cursor o cualquier cliente MCP consulte tus cursos, tareas pendientes,
fechas de entrega, calificaciones y anuncios y, si se lo pides, adjunte archivos o
entregue una tarea.

Usa la API oficial de Google Classroom con OAuth de tu propia cuenta. Pide permisos de
Classroom y de Drive: solo lectura para bajar a tu disco los adjuntos de tareas,
materiales y anuncios (los Docs, Sheets y Slides de Google se exportan a PDF, xlsx,
etc.), y `drive.file` para subir tus entregas a una carpeta "Entregas Classroom" de tu
Drive (con ese permiso el servidor solo ve los archivos que él mismo sube).

> **Sobre las entregas.** La API de Google solo permite adjuntar archivos y entregar
> desde la misma aplicación que creó la tarea. Si tu profesor la creó desde la web de
> Classroom (lo normal), adjuntar devuelve `403 @ProjectPermissionDenied`. Es una
> restricción de Google, no del servidor. Para esas tareas está `submit_in_browser`:
> maneja Google Chrome con tu sesión y hace los mismos clics que tú (ver
> [Entregar por navegador](#entregar-por-navegador)). `submit_assignment(files=[...])`
> intenta la vía API y, si falla, al menos deja tus archivos en Drive.

## Requisitos

- [uv](https://docs.astral.sh/uv/) instalado. En macOS: `brew install uv`.
  En cualquier sistema: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- Un client secret de OAuth de Google Cloud (gratis, ver abajo).
- Google Chrome, solo si quieres entregar tareas por navegador.
- Que tu cuenta de Classroom permita apps de terceros. Si es una cuenta
  institucional, el administrador puede tenerlo bloqueado.

## Instalación

**1. Crea el client secret en Google Cloud** (una sola vez, unos 5 minutos):

1. Entra a <https://console.cloud.google.com> y crea un proyecto, por ejemplo `classroom-mcp`.
2. APIs y servicios > Biblioteca: habilita **Google Classroom API** y **Google Drive API**
   (Drive es para poder bajar los adjuntos).
3. APIs y servicios > Pantalla de consentimiento de OAuth (o "Google Auth Platform"):
   tipo de usuario **Externo**, llena nombre y correo, y en **Usuarios de prueba**
   agrega **todas** las cuentas de Google con las que entras a Classroom.
4. APIs y servicios > Credenciales > Crear credenciales > **ID de cliente de OAuth**,
   tipo de aplicación **Aplicación de escritorio**. Descarga el JSON.
5. Recomendado: en Google Auth Platform > **Audiencia**, dale **Publicar app**. Mientras la
   app esté en estado "Prueba", Google caduca cada autorización a los 7 días y hay que
   repetir `setup`. En "Producción" la autorización dura indefinidamente; la app sigue
   siendo tuya y sin verificar, solo verás el aviso de "app no verificada" una vez por cuenta.

**2. Autoriza tu cuenta** (abre el navegador; el token queda en
`~/.config/google-classroom-mcp/accounts/<alias>.json` con permisos solo para tu usuario):

```bash
uvx --from git+https://github.com/AlanMagno1/google-classroom-mcp google-classroom-mcp setup ~/Downloads/client_secret_XXXX.json
```

Si Google avisa que la app no está verificada, elige "Continuar": la app es tuya.

**¿Otra cuenta?** Vuelve a correr `setup` (ya sin el JSON) y en el navegador elige la
otra cuenta de Google. Por default cada cuenta se guarda con su correo como alias; si
prefieres un nombre corto usa `setup --as unam`. Con `--hint tu@correo.unam.mx` Google va
directo a esa cuenta y el setup se niega a guardar si autorizas con otra (útil cuando el
navegador tiene abierta solo la cuenta equivocada).

**3. Registra el servidor en Claude Code:**

```bash
claude mcp add google-classroom -s user -- uvx --from git+https://github.com/AlanMagno1/google-classroom-mcp google-classroom-mcp
```

Listo. Abre Claude Code y pídele, por ejemplo:

> ¿Qué tareas tengo pendientes en Classroom?

> Revisa https://classroom.google.com/c/NzE2NDU5MjM0/a/NjA1MzIx/details y dime qué piden.

> Bájame los archivos de la práctica 3 de Minería y resume qué hay que hacer.

> Entrega ~/Documents/practica3.ipynb en la práctica 3 de Minería.

### Otros clientes (Claude Desktop, Cursor, etc.)

```json
{
  "mcpServers": {
    "google-classroom": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/AlanMagno1/google-classroom-mcp", "google-classroom-mcp"]
    }
  }
}
```

### Variables de entorno

| Variable | Descripción |
|---|---|
| `GOOGLE_CLASSROOM_MCP_CONFIG_DIR` | Carpeta de configuración. Default: `~/.config/google-classroom-mcp` |
| `GOOGLE_CLASSROOM_CLIENT_SECRET` | Ruta al client secret. Default: `<config>/client_secret.json` |
| `GOOGLE_CLASSROOM_DOWNLOAD_DIR` | Carpeta de descargas. Default: `~/Downloads/google-classroom-mcp` |
| `GOOGLE_CLASSROOM_BROWSER_PROFILES` | Carpeta con un perfil de Chrome por cuenta. Default: `<config>/browser-profiles` |
| `GOOGLE_CLASSROOM_BROWSER_HEADLESS` | `1` para entregar con Chrome oculto. Default: ventana visible |

### Varias cuentas de Google

Un solo servidor maneja todas tus cuentas. Cada herramienta acepta un parámetro
`account` opcional (alias, correo, o un pedazo de cualquiera de los dos):

- Si solo hay una cuenta, nunca hace falta indicarlo.
- `list_courses` y `list_pending_assignments` sin `account` recorren todas las cuentas
  y marcan a cuál pertenece cada curso.
- Las herramientas que reciben un curso o una tarea averiguan solas en qué cuenta está.

Si por alguna razón quieres dos servidores separados, sigue funcionando la variable
`GOOGLE_CLASSROOM_MCP_CONFIG_DIR` con un nombre de servidor distinto para cada uno.

## Herramientas

Todas aceptan `account?` al final. Los `course_id` aceptan también la URL del curso; los
ids en las URLs de Classroom van en base64 y el servidor los decodifica solo.

| Herramienta | Qué hace |
|---|---|
| `list_accounts()` | Cuentas configuradas (alias, correo, nombre). |
| `get_profile()` | Verifica la conexión y devuelve el usuario autenticado de cada cuenta. |
| `list_courses(include_archived?)` | Cursos en los que estás inscrito como alumno, con la cuenta de cada uno. |
| `list_pending_assignments(course_id?)` | Tareas que aún no has entregado, por cuenta y curso, ordenadas por fecha límite. Marca las vencidas. |
| `get_course_contents(course_id)` | Tareas, preguntas y materiales del curso agrupados por tema, con el estado de tu entrega y calificación. |
| `get_assignment(coursework_id_or_url, course_id?)` | Detalle de una tarea: instrucciones, fecha, puntos, adjuntos y tu entrega. Acepta la URL completa de Classroom. |
| `list_announcements(course_id, limit?)` | Anuncios del tablón, del más reciente al más antiguo. |
| `download_assignment_files(coursework_id_or_url, course_id?, dest_dir?, include_submission?, export_format?)` | Baja todos los adjuntos de Drive de una tarea o material a `~/Downloads/google-classroom-mcp/<curso>/<tarea>/`. Con `include_submission=True` baja también los archivos de tu entrega. Enlaces, videos y formularios se devuelven con su URL. |
| `download_file(file_id_or_url, filename?, dest_dir?, export_format?)` | Baja un solo archivo de Drive (el `drive_id` o `url` que devuelven las demás herramientas) y devuelve la ruta local. |
| `submit_in_browser(coursework_id_or_url, files?, course_id?, turn_in?)` | Entrega de verdad, por navegador: abre Chrome con tu sesión, adjunta los archivos locales de `files`, da "Entregar" y verifica por la API que quedó en `TURNED_IN`. Con `turn_in=False` solo adjunta. Requiere `browser-login <alias>` una vez por cuenta. |
| `upload_file(path, folder?, name?)` | Sube un archivo local a `Entregas Classroom/` en tu Drive (o a la subcarpeta `folder`, o a un id/URL de carpeta) y devuelve su `drive_id` y `url`. |
| `submit_assignment(coursework_id_or_url, course_id?, files?, drive_ids?, links?, turn_in?)` | Sube los archivos locales de `files` a `Entregas Classroom/<curso>/`, adjunta esos, los de `drive_ids` y/o `links` a tu entrega y si `turn_in=True` la entrega. Si Google rechaza el adjunto, devuelve en `next_step` cómo terminar desde la web. Ver el aviso de arriba. |
| `reclaim_submission(coursework_id_or_url, course_id?)` | Anula una entrega ya enviada para poder modificarla. Misma restricción. |

Los archivos nativos de Google no tienen un binario que bajar, así que se exportan:
Docs y Slides a `pdf`, Sheets a `xlsx`, dibujos a `png`. Con `export_format` puedes pedir
otro (`docx`, `txt`, `md`, `html`, `csv`, `pptx`...). `dest_dir` acepta una ruta absoluta o
una carpeta relativa a la de descargas.

`upload_file` y `submit_assignment(files=...)` necesitan que la cuenta se haya autorizado
con el permiso `drive.file`. Si la autorizaste con una versión anterior, esas dos
herramientas te dirán qué comando correr; todo lo demás sigue funcionando sin él.

## Entregar por navegador

Como la API rechaza entregar tareas creadas por el profesor, `submit_in_browser` hace la
entrega igual que tú: con [Playwright](https://playwright.dev/python/) maneja tu Google
Chrome instalado sobre un perfil aparte, abre la tarea con la cuenta correcta, "Agregar o
crear" > "Archivo", sube el archivo, "Entregar", y luego confirma por la API que el estado
cambió a `TURNED_IN`. Si algo falla, deja una captura de pantalla en
`~/Downloads/google-classroom-mcp/_navegador/`.

Requiere Google Chrome instalado y, por cada cuenta, una sesión iniciada una sola vez en
un perfil de Chrome propio (una cuenta por perfil; la multisesión de Google no es
confiable para esto):

```bash
google-classroom-mcp browser-login unam --email tu@correo.unam.mx   # abre Chrome: inicia sesión y cierra la ventana
google-classroom-mcp browser-login personal --email tu@gmail.com
google-classroom-mcp browser-status                                 # sesión de cada perfil
```

Los perfiles viven en `~/.config/google-classroom-mcp/browser-profiles/<alias>` (variable
`GOOGLE_CLASSROOM_BROWSER_PROFILES`). Inicia sesión siempre desde `browser-login`: en macOS
el Chrome que abre Playwright cifra las cookies con una llave distinta a la de tu Chrome
normal, así que una sesión iniciada fuera no le sirve. Si Google bloquea el inicio de
sesión en el navegador controlado, `browser-login ALIAS --plain` abre un Chrome sin
automatizar pero compatible. La ventana de Chrome se ve mientras entrega; con
`GOOGLE_CLASSROOM_BROWSER_HEADLESS=1` corre oculta. Automatizar la web de Classroom no es
un uso que Google ofrezca oficialmente; es tu cuenta y tus tareas, pero conviene saberlo.

## Comandos

```bash
google-classroom-mcp setup [client_secret.json] [--as ALIAS] [--hint CORREO]   # guarda el client secret y autoriza una cuenta
google-classroom-mcp accounts                                  # lista las cuentas configuradas
google-classroom-mcp remove ALIAS                              # quita una cuenta
google-classroom-mcp check                                     # verifica la conexión de todas las cuentas
google-classroom-mcp browser-login ALIAS [--email CORREO] [--plain]   # inicia sesión en el perfil de Chrome de esa cuenta
google-classroom-mcp browser-status                            # sesión de cada perfil de navegador
google-classroom-mcp                                           # arranca el servidor MCP por stdio (lo usa el cliente)
```

## Permisos que pide

Classroom: lectura de cursos, materiales, anuncios, temas, lista del curso (para leer tu
perfil) y correo del perfil, y lectura y escritura de tu propio trabajo de clase y tus
entregas (`classroom.coursework.me`). Drive: solo lectura (`drive.readonly`) para bajar
los adjuntos, y `drive.file` para subir tus entregas; con este último el servidor solo
puede ver y tocar los archivos que él mismo creó, nunca el resto de tu Drive, y no borra
nada. Nada se envía a ningún servidor que no sea Google. `upload_file`,
`submit_assignment` y `reclaim_submission` crean archivos o modifican tu entrega: Claude
solo debe usarlas cuando se lo pidas explícitamente.

Si ya tenías cuentas autorizadas con una versión anterior, siguen funcionando para todo
menos para subir; para eso vuelve a correr `google-classroom-mcp setup --as <alias>
--hint <correo>` una vez por cuenta.

## Desarrollo

```bash
git clone https://github.com/AlanMagno1/google-classroom-mcp
cd google-classroom-mcp
uv sync
uv run google-classroom-mcp check
```

Para probar cambios locales en Claude Code sin publicar:

```bash
claude mcp add google-classroom -s user -- uv --directory /ruta/a/google-classroom-mcp run google-classroom-mcp
```

## Licencia

MIT
