# google-classroom-mcp

Servidor [MCP](https://modelcontextprotocol.io) para Google Classroom, pensado para el
alumno y con soporte para varias cuentas de Google a la vez. Permite que Claude Code,
Claude Desktop, Cursor o cualquier cliente MCP consulte tus cursos, tareas pendientes,
fechas de entrega, calificaciones y anuncios, descargue los materiales adjuntos de Drive
y, si se lo pides, suba tus archivos a Drive y los adjunte o entregue en una tarea.

Usa la API oficial de Google Classroom con OAuth de tu propia cuenta.

> **Sobre las entregas.** La API de Google solo permite adjuntar archivos y entregar
> desde la misma aplicación que creó la tarea. Si tu profesor la creó desde la web de
> Classroom (lo normal), `submit_assignment` sube tus archivos a Drive pero el paso de
> adjuntar o entregar devuelve `403 @ProjectPermissionDenied`. En ese caso solo queda
> adjuntar el archivo desde classroom.google.com. Es una restricción de Google, no del
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
2. APIs y servicios > Biblioteca: habilita **Google Classroom API** y **Google Drive API**.
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
prefieres un nombre corto usa `setup --as unam`.

**3. Registra el servidor en Claude Code:**

```bash
claude mcp add google-classroom -s user -- uvx --from git+https://github.com/AlanMagno1/google-classroom-mcp google-classroom-mcp
```

Listo. Abre Claude Code y pídele, por ejemplo:

> ¿Qué tareas tengo pendientes en Classroom?

> Revisa https://classroom.google.com/c/NzE2NDU5MjM0/a/NjA1MzIx/details y dime qué piden.

> Sube ~/tarea3.pdf a la tarea 3 de Cálculo de la cuenta unam y entrégala.

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
- `download_file` prueba con cada cuenta hasta que una pueda leer el archivo.

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
| `download_file(drive_id_or_url, filename?, export_mime_type?)` | Descarga un archivo de Drive a `~/Downloads/google-classroom-mcp/`. Docs y Slides se exportan a PDF, Sheets a CSV. |
| `upload_to_drive(path, folder?)` | Sube un archivo local a tu Drive (carpeta "Classroom MCP") y devuelve `drive_id` y enlace. |
| `submit_assignment(coursework_id_or_url, course_id?, file_paths?, drive_ids?, links?, turn_in?)` | Sube archivos locales a Drive, los adjunta a tu entrega (junto con `drive_ids` y `links`) y si `turn_in=True` la entrega. Ver el aviso de arriba. |
| `reclaim_submission(coursework_id_or_url, course_id?)` | Anula una entrega ya enviada para poder modificarla. Misma restricción. |

## Comandos

```bash
google-classroom-mcp setup [client_secret.json] [--as ALIAS]   # guarda el client secret y autoriza una cuenta
google-classroom-mcp accounts                                  # lista las cuentas configuradas
google-classroom-mcp remove ALIAS                              # quita una cuenta
google-classroom-mcp check                                     # verifica la conexión de todas las cuentas
google-classroom-mcp                                           # arranca el servidor MCP por stdio (lo usa el cliente)
```

## Permisos que pide

Lectura de cursos, materiales, anuncios, temas, lista del curso (para leer tu perfil) y
correo del perfil. Lectura y escritura de tu propio trabajo de clase y tus entregas
(`classroom.coursework.me`). Drive de solo lectura para descargar adjuntos, y
`drive.file` para subir tus archivos (solo ve los archivos que él mismo creó). Nada se
envía a ningún servidor que no sea Google. `submit_assignment` y `reclaim_submission`
modifican tu entrega: Claude solo debe usarlas cuando se lo pidas explícitamente.

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
