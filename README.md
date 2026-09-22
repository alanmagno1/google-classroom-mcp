# google-classroom-mcp

[MCP](https://modelcontextprotocol.io) server for Google Classroom, built for the
student and with support for several Google accounts at once. It lets Claude Code,
Claude Desktop, Cursor or any MCP client look up your courses, pending assignments,
due dates, grades and announcements and, if you ask it to, attach files or turn in
an assignment.

It uses the official Google Classroom API with OAuth on your own account. It asks for
Classroom and Drive permissions: read-only to download to your disk the attachments of
assignments, materials and announcements (Google Docs, Sheets and Slides are exported
to PDF, xlsx, etc.), and `drive.file` to upload your submissions to a "Classroom
Submissions" folder in your Drive (with that permission the server only sees the files
it uploads itself).

<a id="sobre-las-entregas"></a>

> **About submissions.** The Google API only allows attaching files and turning in
> from the same application that created the assignment. If your teacher created it
> from the Classroom web app (the usual case), attaching returns
> `403 @ProjectPermissionDenied`. That is a Google restriction, not the server's. For
> those assignments there is `submit_in_browser`: it drives Google Chrome with your
> session and makes the same clicks you would (see
> [Submitting through the browser](#submitting-through-the-browser)).
> `submit_assignment(files=[...])` tries the API route and, if it fails, at least
> leaves your files in Drive.

## Requirements

- [uv](https://docs.astral.sh/uv/) installed. On macOS: `brew install uv`.
  On any system: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- A Google Cloud OAuth client secret (free, see below).
- Google Chrome, only if you want to submit assignments through the browser.
- Your Classroom account must allow third-party apps. If it is an institutional
  account, the administrator may have that blocked.

## Installation

**1. Create the client secret in Google Cloud** (once, about 5 minutes):

1. Go to <https://console.cloud.google.com> and create a project, for example `classroom-mcp`.
2. APIs & Services > Library: enable **Google Classroom API** and **Google Drive API**
   (Drive is needed to download attachments).
3. APIs & Services > OAuth consent screen (or "Google Auth Platform"): user type
   **External**, fill in name and email, and under **Test users** add **all** the
   Google accounts you use to sign in to Classroom.
4. APIs & Services > Credentials > Create credentials > **OAuth client ID**,
   application type **Desktop app**. Download the JSON.
5. Recommended: in Google Auth Platform > **Audience**, click **Publish app**. While the
   app is in "Testing" status, Google expires every authorization after 7 days and you
   have to repeat `setup`. In "Production" the authorization lasts indefinitely; the app
   is still yours and unverified, you will just see the "unverified app" notice once per account.

**2. Authorize your account** (opens the browser; the token is stored in
`~/.config/google-classroom-mcp/accounts/<alias>.json` with permissions only for your user):

```bash
uvx --from git+https://github.com/AlanMagno1/google-classroom-mcp google-classroom-mcp setup ~/Downloads/client_secret_XXXX.json
```

If Google warns that the app is not verified, choose "Continue": the app is yours.

**Another account?** Run `setup` again (without the JSON this time) and pick the other
Google account in the browser. By default each account is saved with its email as the
alias; if you prefer a short name use `setup --as unam`. With `--hint you@university.edu`
Google goes straight to that account and setup refuses to save if you authorize with a
different one (useful when the browser only has the wrong account open).

**3. Register the server in Claude Code:**

```bash
claude mcp add google-classroom -s user -- uvx --from git+https://github.com/AlanMagno1/google-classroom-mcp google-classroom-mcp
```

Done. Open Claude Code and ask it, for example:

> What assignments do I have pending in Classroom?

> Check https://classroom.google.com/c/NzE2NDU5MjM0/a/NjA1MzIx/details and tell me what it asks for.

> Download the files for Data Mining practice 3 and summarize what has to be done.

> Submit ~/Documents/practica3.ipynb to Data Mining practice 3.

### Other clients (Claude Desktop, Cursor, etc.)

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

### Environment variables

| Variable | Description |
|---|---|
| `GOOGLE_CLASSROOM_MCP_CONFIG_DIR` | Configuration folder. Default: `~/.config/google-classroom-mcp` |
| `GOOGLE_CLASSROOM_CLIENT_SECRET` | Path to the client secret. Default: `<config>/client_secret.json` |
| `GOOGLE_CLASSROOM_DOWNLOAD_DIR` | Downloads folder. Default: `~/Downloads/google-classroom-mcp` |
| `GOOGLE_CLASSROOM_BROWSER_PROFILES` | Folder with one Chrome profile per account. Default: `<config>/browser-profiles` |
| `GOOGLE_CLASSROOM_BROWSER_HEADLESS` | `0` to show the Chrome window when submitting. Default: hidden |

### Several Google accounts

A single server handles all your accounts. Every tool accepts an optional `account`
parameter (alias, email, or a piece of either):

- If there is only one account, you never need to pass it.
- `list_courses` and `list_pending_assignments` without `account` go through every
  account and mark which one each course belongs to.
- Tools that take a course or an assignment work out on their own which account it is in.

If for some reason you want two separate servers, the `GOOGLE_CLASSROOM_MCP_CONFIG_DIR`
variable still works with a different server name for each one.

## Tools

All of them accept `account?` as the last parameter. `course_id` also accepts the course
URL; the ids in Classroom URLs are base64 and the server decodes them on its own.

| Tool | What it does |
|---|---|
| `list_accounts()` | Configured accounts (alias, email, name). |
| `get_profile()` | Checks the connection and returns the authenticated user of each account. |
| `list_courses(include_archived?)` | Courses you are enrolled in as a student, with the account of each one. |
| `list_pending_assignments(course_id?)` | Assignments you have not turned in yet, by account and course, sorted by due date. Flags the overdue ones. |
| `get_course_contents(course_id)` | Assignments, questions and materials of the course grouped by topic, with the state of your submission and grade. |
| `get_assignment(coursework_id_or_url, course_id?)` | Details of an assignment: instructions, due date, points, attachments and your submission. Accepts the full Classroom URL. |
| `list_announcements(course_id, limit?)` | Stream announcements, newest first. |
| `download_assignment_files(coursework_id_or_url, course_id?, dest_dir?, include_submission?, export_format?)` | Downloads every Drive attachment of an assignment or material to `~/Downloads/google-classroom-mcp/<course>/<assignment>/`. With `include_submission=True` it also downloads the files of your submission. Links, videos and forms are returned with their URL. |
| `download_file(file_id_or_url, filename?, dest_dir?, export_format?)` | Downloads a single Drive file (the `drive_id` or `url` returned by the other tools) and returns the local path. |
| `submit_in_browser(coursework_id_or_url, files?, course_id?, turn_in?)` | The real submission, through the browser: drives a hidden Chrome with your session, attaches the local files in `files`, clicks "Turn in" and verifies through the API that it ended up as `TURNED_IN`. With `turn_in=False` it only attaches. Requires `browser-login <alias>` once per account. |
| `reclaim_in_browser(coursework_id_or_url, course_id?)` | Unsubmits an already turned-in assignment through the browser ("Unsubmit") and verifies through the API that it is no longer `TURNED_IN`. Attachments stay. This is the way to unsubmit the assignments the API rejects. |
| `upload_file(path, folder?, name?)` | Uploads a local file to `Classroom Submissions/` in your Drive (or to the `folder` subfolder, or to a folder id/URL) and returns its `drive_id` and `url`. |
| `submit_assignment(coursework_id_or_url, course_id?, files?, drive_ids?, links?, turn_in?)` | Uploads the local files in `files` to `Classroom Submissions/<course>/`, attaches those plus the ones in `drive_ids` and/or `links` to your submission and, if `turn_in=True`, turns it in. If Google rejects the attachment, it returns in `next_step` how to finish from the web app. See the notice above. |
| `reclaim_submission(coursework_id_or_url, course_id?)` | Unsubmits an already turned-in assignment so you can modify it. Same restriction. |

Google-native files have no binary to download, so they are exported: Docs and Slides
to `pdf`, Sheets to `xlsx`, drawings to `png`. With `export_format` you can ask for
another one (`docx`, `txt`, `md`, `html`, `csv`, `pptx`...). `dest_dir` accepts an
absolute path or a folder relative to the downloads folder.

`upload_file` and `submit_assignment(files=...)` need the account to have been authorized
with the `drive.file` permission. If you authorized it with an earlier version, those two
tools will tell you which command to run; everything else keeps working without it.

## Submitting through the browser

Since the API refuses to turn in assignments created by the teacher, `submit_in_browser`
submits the same way you would: with [Playwright](https://playwright.dev/python/) it drives
your installed Google Chrome on a separate profile, opens the assignment with the right
account, "Add or create" > "File", uploads the file, "Turn in", and then confirms through
the API that the state changed to `TURNED_IN`. `reclaim_in_browser` does the opposite: it
clicks "Unsubmit", confirms and checks that the submission is no longer `TURNED_IN`. If
something fails, either one leaves a screenshot in `~/Downloads/google-classroom-mcp/_browser/`.

It requires Google Chrome installed and, for each account, a session signed in once in
its own Chrome profile (one account per profile; Google's multi-account sign-in is not
reliable for this):

```bash
google-classroom-mcp browser-login unam --email you@university.edu   # opens Chrome: sign in and close the window
google-classroom-mcp browser-login personal --email you@gmail.com
google-classroom-mcp browser-status                                  # session of each profile
```

Profiles live in `~/.config/google-classroom-mcp/browser-profiles/<alias>` (variable
`GOOGLE_CLASSROOM_BROWSER_PROFILES`). Always sign in from `browser-login`: on macOS the
Chrome that Playwright opens encrypts cookies with a different key than your regular
Chrome, so a session started elsewhere is no use to it. If Google blocks signing in from
the controlled browser, `browser-login ALIAS --plain` opens a Chrome without automation
but compatible with it. When submitting, Chrome runs hidden in the background; with
`GOOGLE_CLASSROOM_BROWSER_HEADLESS=0` the window is shown, useful to watch what happens if
something fails. Automating the Classroom web app is not a use Google offers officially;
it is your account and your assignments, but it is worth knowing.

## Commands

```bash
google-classroom-mcp setup [client_secret.json] [--as ALIAS] [--hint EMAIL]   # saves the client secret and authorizes an account
google-classroom-mcp accounts                                  # lists the configured accounts
google-classroom-mcp remove ALIAS                              # removes an account
google-classroom-mcp check                                     # checks the connection of every account
google-classroom-mcp browser-login ALIAS [--email EMAIL] [--plain]   # signs in to that account's Chrome profile
google-classroom-mcp browser-status                            # session of each browser profile
google-classroom-mcp                                           # starts the MCP server over stdio (used by the client)
```

<a id="permisos-que-pide"></a>

## Permissions requested

Classroom: read access to courses, materials, announcements, topics, the course roster
(to read your profile) and the profile email, and read and write access to your own
coursework and submissions (`classroom.coursework.me`). Drive: read-only
(`drive.readonly`) to download attachments, and `drive.file` to upload your submissions;
with the latter the server can only see and touch the files it created itself, never the
rest of your Drive, and it deletes nothing. Nothing is sent to any server other than
Google's. `upload_file`, `submit_assignment`, `submit_in_browser`, `reclaim_submission`
and `reclaim_in_browser` create files or modify your submission: Claude should only use
them when you explicitly ask.

If you already had accounts authorized with an earlier version, they keep working for
everything except uploading; for that, run `google-classroom-mcp setup --as <alias>
--hint <email>` again once per account.

## Development

```bash
git clone https://github.com/AlanMagno1/google-classroom-mcp
cd google-classroom-mcp
uv sync
uv run google-classroom-mcp check
```

To try local changes in Claude Code without publishing:

```bash
claude mcp add google-classroom -s user -- uv --directory /path/to/google-classroom-mcp run google-classroom-mcp
```

## License

MIT
