# Documents and who can see them

## Where the documents come from

Your administrator adds documents in two ways:

- A folder on the assistant's server. Files placed there are picked up automatically, subfolders included, and a file deleted while the assistant is running is removed.
- Uploads in the admin panel, under Knowledge Base.

Upload accepts PDF, Word (.docx), plain text, Markdown, JSON, YAML and source code files (.py, .js, .ts). The server folder takes the text formats: Markdown, plain text, JSON, YAML and source files. PDF and Word documents go in through Upload, which extracts their text.

Some deployments add connected sources such as shared drives. Those are not part of every instance; ask your administrator what this one uses.

## Access tiers and departments

Every account has a tier: owner, administrator, member or guest. Every document belongs to a department, and every department has a minimum tier:

- The general department is open to everyone the instance admits, guests included.
- Other departments require a tier. On a fresh instance the restricted department (the operator's internal material) and the history department (the operator's work log) are owner-only, and any department not yet listed in the access map is owner-only until an administrator opens it.

You see the general documents plus the documents of the departments your tier is cleared for. The assistant checks this for every question. Nothing you type can widen it.

## I know the document exists, but the assistant cannot find it

The usual reasons, in order of likelihood:

1. It is not in the assistant yet. A file in the server folder is picked up within moments; an uploaded file is in as soon as the upload finishes. Your administrator can see the full list under Knowledge Base.
2. It is in a department above your tier.
3. It was withheld. Documents from outside sources are checked for hidden instructions when they are added; a suspicious one is held instead of being indexed. Administrators see it under Quarantine for the departments their clearance covers; the owner sees every department and decides whether to release it.
4. Its file type is not one the assistant can read.

## Checking what the assistant knows

Ask it. A question like "What do you have about the vendor onboarding policy?" gets you the documents it can see on that topic. For the full list of everything ingested, ask your administrator: the admin panel shows every document, its department and how many passages it holds.
