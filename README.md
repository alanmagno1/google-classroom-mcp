# google-classroom-mcp

Servidor [MCP](https://modelcontextprotocol.io) para Google Classroom, pensado para el
alumno y con soporte para varias cuentas de Google a la vez. Permite que Claude Code,
Claude Desktop, Cursor o cualquier cliente MCP consulte tus cursos, tareas pendientes,
fechas de entrega, calificaciones y anuncios y, si se lo pides, adjunte archivos o
entregue una tarea.

Usa la API oficial de Google Classroom con OAuth de tu propia cuenta. Pide permisos de
Classroom y de solo lectura de Drive: con eso baja a tu disco los archivos adjuntos a
tareas, materiales y anuncios (los Docs, Sheets y Slides de Google se exportan a PDF,
xlsx, etc.). Para subir un archivo local a Drive y adjuntarlo a una entrega se usa el
[servidor MCP oficial de Google Drive](https://developers.google.com/workspace/guides/configure-mcp-servers),
que en Claude se conecta con un clic.

> **Sobre las entregas.** La API de Google solo permite adjuntar archivos y entregar
> desde la misma aplicación que creó la tarea. Si tu profesor la creó desde la web de
> Classroom (lo normal), `submit_assignment` devuelve `403 @ProjectPermissionDenied` y
> hay que adjuntar desde classroom.google.com. Es una restricción de Google, no del
> servidor.

## Requisitos

- [uv](https://docs.astral.sh/uv/) instalado. En macOS: `brew install uv`.
  En cualquier sistema: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- Un client secret de OAuth de Google Cloud (gratis, ver abajo).
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
| `submit_assignment(coursework_id_or_url, course_id?, drive_ids?, links?, turn_in?)` | Adjunta archivos de Drive (ids o URLs) y/o enlaces a tu entrega y si `turn_in=True` la entrega. Ver el aviso de arriba. |
| `reclaim_submission(coursework_id_or_url, course_id?)` | Anula una entrega ya enviada para poder modificarla. Misma restricción. |

Los archivos nativos de Google no tienen un binario que bajar, así que se exportan:
Docs y Slides a `pdf`, Sheets a `xlsx`, dibujos a `png`. Con `export_format` puedes pedir
otro (`docx`, `txt`, `md`, `html`, `csv`, `pptx`...). `dest_dir` acepta una ruta absoluta o
una carpeta relativa a la de descargas.

Para subir un archivo local a Drive usa el servidor MCP de Google Drive y pasa el id
resultante a `submit_assignment(drive_ids=[...])`.

## Comandos

```bash
google-classroom-mcp setup [client_secret.json] [--as ALIAS] [--hint CORREO]   # guarda el client secret y autoriza una cuenta
google-classroom-mcp accounts                                  # lista las cuentas configuradas
google-classroom-mcp remove ALIAS                              # quita una cuenta
google-classroom-mcp check                                     # verifica la conexión de todas las cuentas
google-classroom-mcp                                           # arranca el servidor MCP por stdio (lo usa el cliente)
```

## Permisos que pide

Classroom: lectura de cursos, materiales, anuncios, temas, lista del curso (para leer tu
perfil) y correo del perfil, y lectura y escritura de tu propio trabajo de clase y tus
entregas (`classroom.coursework.me`). Drive: solo lectura (`drive.readonly`), para bajar
los adjuntos; el servidor nunca crea, modifica ni borra nada en Drive. Nada se envía a
ningún servidor que no sea Google. `submit_assignment` y `reclaim_submission` modifican
tu entrega: Claude solo debe usarlas cuando se lo pidas explícitamente.

Si ya tenías cuentas autorizadas con una versión anterior (sin Drive), el servidor te
pedirá volver a correr `google-classroom-mcp setup --as <alias>` para renovar el token
con el permiso nuevo.

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
