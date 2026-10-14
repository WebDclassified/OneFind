# OneFind User Guide

> For people who want to *use* OneFind to find things in their notes. You do not need to know
> anything about search engines. Developers should also read [`README.md`](../README.md) and
> [`HOW_TO_RUN.md`](../HOW_TO_RUN.md).
>
> Applies to **OneFind 1.2**. Quoted text such as **Index a folder** or "No index ready" is exactly
> what you will see on screen.

## Contents

1. [What OneFind is](#1-what-onefind-is)
2. [First run](#2-first-run)
3. [Indexing a folder](#3-indexing-a-folder)
4. [Searching](#4-searching)
5. [Reading the results](#5-reading-the-results)
6. [Filters](#6-filters)
7. [Why this result?](#7-why-this-result)
8. [Opening, previewing and copying](#8-opening-previewing-and-copying)
9. [Saved searches, bookmarks and recent items](#9-saved-searches-bookmarks-and-recent-items)
10. [Collections](#10-collections)
11. [Keeping the index up to date](#11-keeping-the-index-up-to-date)
12. [Backup, restore, and moving to another computer](#12-backup-restore-and-moving-to-another-computer)
13. [Health and maintenance](#13-health-and-maintenance)
14. [Preferences, themes and keyboard](#14-preferences-themes-and-keyboard)
15. [Troubleshooting](#15-troubleshooting)
16. [Privacy and limits](#16-privacy-and-limits)

---

## 1. What OneFind is

OneFind searches **your own notes and documents on your own computer**. You point it at a folder
and it reads the files and builds an **index**: one file that lets it find things quickly. You can
then search in three ways:

| Mode | Good for | Example |
|---|---|---|
| **Keyword** | Exact words, names, codes, error messages | `INV-2041`, `Alice budget` |
| **Meaning** | Ideas you can describe but whose exact words you don't remember | "how to undo a bad release" finds a note titled *Rollback checklist* |
| **Hybrid** (default) | Almost everything. It combines both and puts notes that match both ways first | |

OneFind reads Markdown and plain-text files: `.md`, `.markdown`, `.txt`, `.text`, `.rst`, `.org`.
It never changes, moves or deletes your files.

---

## 2. First run

1. Start OneFind. In a terminal, run:

   ```bash
   onefind serve
   ```

   It prints `onefind serving on http://127.0.0.1:8080 …`. Leave that window open, because
   closing it stops OneFind.
2. Open **<http://127.0.0.1:8080/>** in your browser. Bookmarking it is a good idea.
3. The first time, you'll see a welcome card headed "No index ready" and **Search your notes
   privately, on this computer**, with two buttons:
   - **Index a folder**: start here if this is your first time.
   - **Open an existing collection**: use this if you already have a OneFind `.db` file, for
     example one copied from another computer.

The line at the bottom of every screen, "Everything stays on this computer. No network calls.",
is a reminder that your notes are never uploaded anywhere.

The **status pill** at the top right always tells you where things stand:

| Pill | Meaning |
|---|---|
| **Ready** | You can search. |
| **Vectors missing** | Keyword search works. Meaning search needs vectors (see [section 11](#11-keeping-the-index-up-to-date)). |
| **Indexing NN%** | A folder is being indexed. Search keeps working with what is already indexed. |
| **No index** | Nothing has been indexed yet, or the collection is empty. |
| **Needs attention** | Something is out of step. Click the pill to open Manage → Health. |
| **Offline** | The browser cannot reach OneFind. See [Troubleshooting](#15-troubleshooting). |

Clicking the pill opens the most useful place for that state: the indexing progress, Manage →
Health, or a reconnect attempt.

---

## 3. Indexing a folder

Click **Index a folder** on the welcome card, or **Manage → Maintenance → Add a folder…** later.
The window is titled **Choose a folder to index**.

1. **Pick the folder.** Use **Places** on the left (Home, Documents, Desktop and your drives), click folders to go
   inside, and use the up arrow to go back. You can also type or paste a path under **Or type a
   folder path** and press **Go**.
   - Supported files show up in the list, so you can tell you're in the right place.
   - Clicking a single file indexes just that file. Clicking it again selects the whole folder.
   - Subfolders are always included.
2. **(Optional) Skip things.** Open **Options** → **Skip these files or folders** and type
   patterns separated by commas or new lines, for example `drafts, archive/*, *.log.md`. Hidden
   folders and `node_modules` are always skipped. OneFind remembers these patterns for the folder.
3. **Meaning search.** Keep **Build vectors for meaning search** ticked. The first time, OneFind
   loads a small language model that runs on your computer. Without vectors you still get Keyword
   search.
4. If this creates a new collection, you can give it a **Collection name** (for example *Notes*).
5. Click **Index "folder name"**. If you picked a very large folder such as your whole home folder,
   OneFind asks **Index this whole folder?** first. Usually it's better to choose the folder that
   actually holds your notes.

**Progress.** You'll see four steps: **Scan files → Index text → Build vectors → Ready**, with
counts such as "Found 240 files", then "Saving and building vectors…". You can **Close** the window
and indexing carries on. The status pill shows the percentage, and clicking it brings the progress
back. **Cancel indexing** stops between files. "Notes indexed before you cancelled stay searchable.
Run it again any time to pick up where it left off."

**Done.** "Your collection is ready" shows how many notes were added, updated or skipped. If some
files were skipped, open **Skipped files** to see why: *too large* (over 5 MB), *binary*, *empty*
or *unreadable*. Click **Start searching**.

> **Tip: titles and tags.** A note's title is its first `# Heading`, or its file name. You can also
> start a note with a front-matter block:
>
> ```markdown
> ---
> title: Release rollback checklist
> tags: [ops, release]
> ---
> ```
>
> Tags then become a filter (see [section 6](#6-filters)).

---

## 4. Searching

Click the search box, or press `/` or `Ctrl`+`K` (`Cmd`+`K` on a Mac), and start typing.

- Results appear **as you type** (after two characters). Press **Enter** to search immediately.
- Choose the mode under the box: **Keyword**, **Meaning** or **Hybrid**. The line below explains
  each one:
  - Keyword: "Keyword finds the exact words, names and codes you type."
  - Meaning: "Meaning finds notes about the same idea, even when the words differ."
  - Hybrid: "Hybrid combines exact keywords with related meaning. Best for most searches."
- While searching, a short progress line shows the steps: *Understanding query → Keyword match →
  Meaning match → Combining → Preparing results*. The first meaning search after starting OneFind
  also shows *Loading the local model*, which takes a few seconds once.
- If a search takes a while, "Still working…" appears with a **Cancel search** button.
- The **×** in the box (or `Esc`) clears the search and takes you back to the home screen.

**Options** (next to Filters) holds two advanced settings you can usually ignore:

- **Results**: how many to show (5, 10, 20 or 30).
- **Vector precision**: **Full** (most accurate, the default), **Int8** ("a little faster, nearly
  as accurate") or **Binary** ("the fastest and least precise option"). This applies only to
  Meaning and Hybrid.

The page address includes your search, so you can bookmark it in your browser or reload it and get
the same search back.

---

## 5. Reading the results

The summary line reads like "1 result · 9 weaker · Hybrid · 34 ms". Click the time to see how long
each step took.

Each result card shows:

- the **title**, then the **folder › file name**;
- a **snippet**, the passage that best matches your words, with them highlighted;
- the date it was **Modified** and the file type;
- a match label: **Strong match**, **Good match** or **Weak match**;
- the buttons **Open**, **Preview**, **Explain** and **More**.

**Weaker matches.** Results that only share a few words or ideas with your search are collected
under **Weaker matches (N)**: "These share only some words or ideas with your search. They may not
be what you are looking for." Click it to show or hide them.

**No strong matches.** If nothing fits well, OneFind says so honestly: "Nothing in this collection
closely matches "…". The closest documents are below, but they may not be what you are looking
for."

**Partial keyword matches.** If no note contains *all* of your keywords (small words such as
"the" or "what" don't count), you'll see "No note contains all of your keywords, so notes with
some of them are included."

**No results at all.** The message depends on the mode. For example, in Keyword mode: "No document
contains any of the words in "…". Check the spelling, try other words, or search by meaning."
Buttons offer the next step: **Search by meaning**, **Try Hybrid**, **Clear filters** (if filters
are on) or **Edit search**.

**Saving and exporting.** Above the results:

- **Save search** keeps the query, mode and filters. It then appears on the home screen.
- **Export** → **Markdown (.md)** or **JSON (.json)** downloads the current results.

---

## 6. Filters

Click **Filters** under the search box to narrow results:

| Filter | Choices |
|---|---|
| **Folder** | Any folder in the collection (includes its subfolders) |
| **File type** | e.g. MD, TXT (the chip reads "Type MD") |
| **Modified** | Modified: any time, Past 7 days, Past 30 days, Past year |
| **Tag** | Tags from your notes' front matter (shown when your notes have tags) |

Active filters appear as **chips**. Click a chip's × to remove it, or **Clear filters** to remove
them all. The Filters button shows how many are active, and the results summary includes them.

Quick way to filter: on a result, **More → Filter to this folder** shows only results from that
note's folder.

If a filtered search finds nothing, the message adds "Your filters may also be hiding results."

---

## 7. Why this result?

Click **Explain** on a result (or press `e`). A **Why this result** box opens with:

- plain-language reasons, such as "Matched all 4 keywords", "Very close in meaning (0.57)" and
  "Found by both keyword and meaning search";
- **Matched words**: which of your words the note contains;
- how each search ranked it, such as "Keyword search ranked it #1." and "Meaning search ranked it
  #1 (similarity 0.57).", or "… did not find it.";
- the mode and vector precision used.

Use it to judge whether a result is really what you're after.

---

## 8. Opening, previewing and copying

| Action | How | What happens |
|---|---|---|
| **Preview** | **Preview** button, or `Enter` on a focused result | Shows the note beside the results (full screen on a phone). Use **Find in document** to jump between matches. |
| **Open** | **Open**, or `o` | Opens the file in your computer's default app for that file type. |
| **Show in folder** | **More → Show in folder** | Opens your file manager with the file selected. |
| **Copy snippet** | **More → Copy snippet**, or `c` | Copies the snippet text. "Snippet copied" confirms it. |
| **Bookmark** | **More → Bookmark**, or `b` | Keeps the note on the home screen under *Bookmarks*. |

Very long notes show only their beginning in Preview: "This is a long document, so only the
beginning is shown. Open the file to read all of it."

**Open** and **Show in folder** only open files inside folders you indexed, and only from the
computer OneFind runs on.

---

## 9. Saved searches, bookmarks and recent items

When the search box is empty, the home screen shows:

- **Recent searches**: click one to run it again. Use **Clear history** to remove them all
  (saved searches and bookmarks stay).
- **Saved searches**: one click reruns the search with its mode and filters. Use the × to
  remove one.
- **Recent documents**: notes you opened recently, plus recently changed ones.
- **Bookmarks**: notes you bookmarked.

Each **collection** keeps its own history, saved searches and bookmarks, and they are stored inside
the collection file, so they travel with it.

---

## 10. Collections

A **collection** is one index, stored as one file. Use separate collections for separate worlds,
such as *Notes*, *Work* and *Project X*. Searching only ever looks inside the current collection.

- **Switch:** click the collection name at the top left. The menu lists your collections with
  their document counts.
- **Create:** collection menu → **New collection…** → type a name (up to 64 characters, with at
  least one letter or number). Then index a folder into it.
- **One collection, several folders:** **Manage → Maintenance → Add a folder…**.
- **Rename / remove / delete:** **Manage → Collections → All collections**:
  - **Switch** makes it the current collection.
  - **Rename** changes the name only; the file stays where it is.
  - **Remove** takes it off OneFind's list but keeps the file on disk, so you can open it again
    later.
  - **Delete file** permanently deletes the collection file. You must type its name to confirm.
    Your notes folder is not touched, and backups are kept.
- **Where is the file?** **Manage → Collections → This collection** shows its path, with a **Copy
  path** button.

---

## 11. Keeping the index up to date

OneFind does not watch your folders constantly. When you add, edit or delete notes, update the
index:

- The home screen and **Manage → Health** check your folders and tell you what changed, for
  example "3 new, 1 changed files since the last index." Click **Update index**.
- Or use **Manage → Maintenance**:
  - **Update index** reads only new and changed files and removes notes whose files are gone. It is
    fast.
  - **Full rebuild** re-reads everything. It is slower, and search keeps working until it finishes.
  - **Reindex with vectors** builds vectors for notes that don't have them, so you can search by
    meaning. It appears only when vectors are missing. When everything is covered you'll see
    "Vectors complete: every document can be found by meaning."

To **leave a note out** of search for good, add an exclude pattern for it (section 3, step 2) and
update the index.

---

## 12. Backup, restore, and moving to another computer

### What a backup is

"A backup is a complete copy of this collection file: the index, your history, saved searches and
bookmarks." It does **not** include your original notes. Back those up separately, as you
normally would.

### Make a backup

**Manage → Backups → Back up now.** "Backup created (1.6 MB)" confirms it. Backups are listed
underneath, newest first, and are stored in OneFind's `backups` folder.

### Restore a backup

Click **Restore…** next to a backup, then choose:

- **Restore as a new collection**: you give it a name and it opens beside your current one. "Your
  current collection stays exactly as it is." This is the safe choice.
- **Replace current…**: replaces the current collection with the backup. You must type the
  collection name to confirm. OneFind makes a backup of the current state first, so you can undo
  it.

### Move to another computer

1. On the old computer, choose **Manage → Backups → Back up now** and copy that backup file to a
   USB stick or shared drive. (You can also copy the collection's `.db` file directly while OneFind
   is closed.)
2. On the new computer, install and start OneFind.
3. Collection menu → **Open existing collection file…** → paste the full path to the file (it ends
   in `.db`) → **Open collection**.

Search and Preview work straight away, because the text of your notes is inside the file.
**Open** and **Show in folder** need your notes folder at the **same location** as on the old
computer. If it lives somewhere else now, use **Manage → Maintenance → Add a folder…** and choose
the new location.

---

## 13. Health and maintenance

Open **Manage** (top right). Its tabs are **Health**, **Maintenance**, **Backups**,
**Collections** and **Preferences**, and there is a **Privacy** note.

**Health** shows, in plain words:

- a status sentence, for example "240 notes ready. Keyword and meaning search available.";
- **Documents**, **Vectors**, **Consistency** ("Index and documents agree"), **Last indexed** and
  **Size**;
- whether your folders have changed since the last update.

For large collections the Size row adds a note. Over 5,000 notes: "Search stays quick; full
rebuilds and building vectors take a few minutes." Over about 50,000: OneFind suggests splitting
into smaller collections.

**Maintenance:**

| Button | Use it when |
|---|---|
| **Check integrity** | Search seems wrong, or the pill says *Needs attention*. It reports "No problems found. The index is consistent." or lists problems with a **Repair** button. |
| **Repair** | After a check finds problems. It rebuilds the keyword index, removes leftovers, and (when the model is available) rebuilds missing vectors, then reports "Repair finished." or "Repair finished, but some problems remain. Restore a backup if search still misbehaves." |
| **Compact** | After deleting lots of notes or a full rebuild. It reclaims disk space, e.g. "Compacted: 12 MB → 8 MB (4 MB freed)." |
| **Clear collection…** (in Collections) | You want to start this collection over. It removes every document, vector, history entry, saved search and bookmark, and asks you to type the name. Your files are not touched, but consider a backup first. |

---

## 14. Preferences, themes and keyboard

**Theme.** The header button switches between **System**, **Light** and **Dark**. You can also
choose in **Manage → Preferences**.

**Reduce motion.** In Manage → Preferences, this "Turns off movement and fades." OneFind already
follows your system's reduced-motion setting.

**Keyboard shortcuts.** Press `?` or click **Shortcuts**.

| Key | Action |
|---|---|
| `/` or `Ctrl`+`K` | Focus the search box |
| `Enter` in search | Search now |
| `↓` `↑` or `j` `k` | Move between results |
| `Enter` on a result | Preview the document |
| `o` / `c` / `b` / `e` | Open / Copy snippet / Bookmark / Explain |
| `Esc` | Close the preview, then clear the search |
| `?` | Show the list |

If single-letter keys get in the way (for example with speech input), turn off **Single-key
shortcuts** in Manage → Preferences. `Ctrl`+`K`, the arrow keys and `Esc` keep working.

---

## 15. Troubleshooting

### "OneFind is not responding" / pill shows **Offline**

> OneFind's local service stopped responding. Start it again with "onefind serve", then try again.
> Your query and your collection are safe.

The terminal window running `onefind serve` was closed, or the computer slept or restarted. Run
`onefind serve` again, then click **Try again**.

### "No index ready"

> No index is ready yet. Index a folder to start searching.

The current collection is empty or brand new. Click **Index a folder**.

### "Meaning search is not set up yet"

> This collection has no meaning-search vectors yet. Build vectors from Manage, or search by
> keyword.

The folder was indexed without vectors. Use **Use Keyword search** for now, or **Manage →
Maintenance → Reindex with vectors**.

### "The local model could not load"

> The meaning-search model could not be loaded. Search by keyword for now, or install the model
> extra (pip install -e ".[model]").

The meaning-search add-on isn't installed, or the first-time model download failed (for example
because you were offline). Keyword search still works. Install the add-on with
`pip install -e ".[model]"` and check it with `onefind check --full`.

### "This collection was built with a different meaning model"

> This collection was built with a different meaning model. Reindex it to search by meaning.

Models can't be mixed in one collection. Either reindex with the original model, or create a new
collection and index the folder into it.

### "Vector storage (sqlite-vec) is not available"

> Vector storage (sqlite-vec) is not available, so meaning search cannot run. Search by keyword
> instead.

Your Python's SQLite can't load extensions. Run `onefind check` to see the fix. Keyword search
works in the meantime.

### "That file is no longer where it was indexed."

You clicked **Open** for a note whose file has been moved, renamed or deleted. Click **Update
index** in the message to tidy up. If the whole folder moved, add the new location with **Add a
folder…**.

### "Opening files is turned off for this server. Use Preview instead."

OneFind was started with `--no-open`, or you're using it from another device. Preview still works.

### "This note's file is outside its indexed folder, so OneFind will not open it."

This is a safety rule: OneFind only opens files inside folders you indexed. Re-index the folder
the note now lives in.

### Indexing problems

| Message | What to do |
|---|---|
| "The folder … does not exist (it may have been moved, renamed, or unplugged). Choose the folder again." | Plug the drive back in, or pick the folder's new location. |
| "No notes OneFind can read were found in … Supported files are .txt, .md, .markdown, .rst, .org and .text …" | Check you chose the right folder, and that your exclude patterns aren't skipping everything. |
| "OneFind is not allowed to read … Check the folder's permissions, or pick a folder you own." | Choose a folder in your own user area, or fix its permissions. |
| "The collection file is busy (another program is writing to it). Wait a moment and try again." | Another OneFind task, a backup program or a sync tool has the file open. Wait, then **Try again**. |
| "An indexing job is running on this collection. Wait for it to finish or cancel it first." | Click the status pill to see the job. Wait or **Cancel indexing**. |
| "Indexing was cancelled" / "Indexing could not finish" | "Notes that were already indexed stay searchable." Fix the cause and choose **Try again**. |
| "Lost contact with OneFind while indexing." | OneFind stopped during indexing. Restart it and run **Update index**, which continues where it stopped. |

### Collection and backup problems

| Message | What to do |
|---|---|
| "The collection file could not be read. Run a check from Manage, or restore a backup." | **Manage → Maintenance → Check integrity**, then **Repair**. If that doesn't help, restore a backup. |
| "The file for "Name" is missing. Open it from its new location, or index a folder to rebuild it." | The `.db` file was moved or deleted. Use **Open existing collection file…** with its new path. In the collection menu it shows as "File missing". |
| "That file is not a OneFind collection, or it was made by a newer version." | Pick a OneFind `.db` file, or update OneFind. |
| "A collection with that name already exists." | Choose another name. Names are not case-sensitive. |
| "To delete the collection file, type its name "…" to confirm." | Type the name exactly as shown. |

### "This request was blocked because it did not come from the OneFind app. Reload the page and try again."

This is a safety check against other web pages controlling OneFind. Reload the OneFind tab. If you
use a browser extension that rewrites requests, turn it off for `127.0.0.1`.

### Something else

Run `onefind status` and `onefind doctor` in a terminal (see [`HOW_TO_RUN.md`](../HOW_TO_RUN.md)).
They print the collection's health, which folders changed, and the result of the last indexing
run.

---

## 16. Privacy and limits

**Privacy.** OneFind reads your files, keeps its index in one local file, and runs meaning search
with a model stored on your computer. There are no accounts, no tracking and no uploads. The only
network use is the **one-time model download** the first time you build vectors. After that the
model loads from your computer without contacting the model host. If you want an extra guarantee,
set `HF_HUB_OFFLINE=1` before starting OneFind. Out of the box, OneFind can only be reached from your own computer.

**Limits worth knowing:**

- It works best with up to a few thousand notes and is comfortable up to about 50,000.
- Only text files: no PDFs, Word documents or scanned images. Files over 5 MB are skipped.
- Meaning search looks mainly at the **beginning** of each note (roughly the first 200 words or
  so). Keyword search covers the whole note.
- The meaning model is trained mostly on English.
- It doesn't watch folders in real time, so use **Update index** after changes.
- It is for one person on one computer. There is no sharing or sync.
