# For administrators

A short map of the admin panel. The operator's manual ships with the product: docs/runbook.md in the repository, and the starter knowledge folder (getting started, troubleshooting, the security model, the FAQ), which the assistant answers from in normal mode for as long as you keep those files.

## The admin panel

Sign in as an owner or administrator and click Admin at the top of the page. The tabs:

- Trust: the live evaluation numbers behind the public Trust page.
- Knowledge Base: every ingested document with its department and passage count; upload new documents; remove old ones.
- Quarantine: uploads withheld by the injection scan. Administrators can review the list; releasing an item is the owner's call.
- System Prompt: the assistant's standing instructions.
- Ingestion Queue: background ingest jobs, when they are enabled.
- Settings: guest access, the retrieval default and its switch, whether people may pick a model, branding and the welcome suggestions.
- Models: which model answers chat and which ones run the evaluations.
- Monitoring and Backup: health, alerts and backups.
- Users: accounts, roles, departments, per-account permissions, unlocking and MFA reset.
- Audit Log: every question answered, who asked and which documents were used.

Settings, Models, Monitoring, Backup and the Audit Log belong to the owner. An administrator runs the content and people tabs.

## Roles

- Owner: everything, including Settings and Models. The first account created on the instance.
- Administrator: documents, the quarantine list and users, not the system itself. The audit log and the system tabs stay with the owner unless a permission is granted per account.
- Member: chat and their own history.
- Guest: chat only, within the guest limits, and only when guest access is on.

Per-account permissions can widen or narrow a role under Users.

## Adding people

Users, then create the account with a username, a password, a role and a department. There is no invitation email: give the person their username and password, and ask them to change the password under their account menu (their name at the top of the page). Creating an account asks for your own password first.

## Adding documents

- The server folder: files placed there are ingested automatically, subfolders included, and a file deleted while the assistant is running is removed. A restart picks up files that changed while the assistant was stopped.
- Knowledge Base, Upload: PDF, Word, text, Markdown, JSON, YAML and source files. The backend's limit is MAX_UPLOAD_MB; the shipped proxy allows uploads up to 64 MB.
- Uploads go to the general department or your own. A department not yet listed in the access map is owner-only until it is; the map is described in the security model.

## Guest access

Off by default: visitors see the sign-in screen. Opening it takes two switches: ALLOW_GUEST_MODE in the server environment and the guest toggle under Settings. Guests are bounded per conversation (GUEST_MAX_TURNS), per answer (GUEST_MAX_TOKENS), per request (GUEST_MAX_INPUT_CHARS) and per day across everyone (DEMO_DAILY_GUEST_LIMIT), and they always answer with the instance's default model, or the one GUEST_MODEL names, never one the request picks.

## Help pages

The answers the Help button gives come from these help pages, shipped inside the product image and refreshed on every update. They are separate from your organization's documents: they never appear in normal answers, never count in the Knowledge Base tab, and your documents never appear in help answers. HELP_DOCS=false in the server environment turns the help lane off and hides the button.
