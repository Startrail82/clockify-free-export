# clockify-free-export

Export your own Clockify time entries, projects and clients to CSV files when the free plan gives you no export, ready to import into [Solidtime](https://github.com/solidtime-io/solidtime).

> This is an unofficial community script. It is not affiliated with, endorsed by or supported by Clockify or Solidtime.

## Why this exists

I moved from Clockify to a self-hosted Solidtime. Getting my data out turned out to be harder than expected:

- The Clockify **free plan has no CSV export** for detailed reports.
- The Clockify API on the free plan is limited to **30 requests per hour**. Existing migration tools that fetch everything in one go (for example a plain `clockify-to-solidtime migrate`) run into HTTP 429 errors long before they finish.
- With a few thousand time entries you need many paged requests, so a one-shot approach does not work.

This script solves that by fetching everything slowly and safely:

- It keeps itself under the rate limit (default 28 requests per hour) and waits automatically when the limit is reached.
- It saves every page to a local cache, so you can stop it (Ctrl+C) and start it again later. It continues where it left off.
- It handles HTTP 429 (honouring `Retry-After`), server errors and network errors with retries.
- It writes CSV files in the format of Solidtime's **Generic** importers, so no other conversion tool is needed.

In my case it exported 7 clients, 40 projects and 5393 time entries in a single unattended run.

## What it exports

| Data | Included |
|---|---|
| Clients | Yes, including archived ones |
| Projects | Yes, including archived ones (name, colour, hourly rate, billable, public, client, time estimate) |
| Time entries | Only **your own** entries, with description, billable flag, client, project, tags, task, start and end |
| Running timers | No, they have no end time yet |
| Breaks and other non-regular entries | No |
| Other users' entries | No, only the user that owns the API key |

All raw API answers are also saved as JSON in `export/raw/` as a backup.

## Requirements

- Python 3.9 or newer
- No extra packages (standard library only)
- A Clockify API key: Clockify > Profile settings > Preferences > **API** > Generate

## Usage

Set your API key in an environment variable, then run the script.

Windows (cmd):

```
set CLOCKIFY_API_KEY=your_key
python clockify_export.py
```

Linux / macOS:

```
export CLOCKIFY_API_KEY=your_key
python3 clockify_export.py
```

The script prints each step and its progress while it runs. Each request returns up to 200 entries, but the hourly limit means a large history can take more than an hour. You can leave it running and just let it wait.

If it stops for any reason, run the same command again. Pages that were already fetched are read from the `cache` folder and do not count against the limit.

### Options

| Option | Default | Description |
|---|---|---|
| `--out` | `export` | Output folder |
| `--cache` | `cache` | Cache folder used for resuming |
| `--workspace` | your default workspace | Workspace ID, or set `CLOCKIFY_WORKSPACE_ID` |
| `--from` | everything | Only time entries from this date, for example `2023-01-01` |
| `--limit` | `28` | Maximum requests per hour (the Clockify limit is 30) |
| `--refresh` | off | Ignore the cache and fetch everything again |

## Importing into Solidtime

The script creates these files in the output folder:

- `projects.csv`
- `time_entries.csv`
- `raw/*.json` (raw backup)

In Solidtime go to **Import** and import in this order, because time entries refer to projects and clients:

1. **Generic Projects**: upload `projects.csv` (this also creates the clients)
2. **Generic Time Entries**: upload `time_entries.csv`

Notes:

- Solidtime merges clients and projects that have the same name, so the numbers it reports can be slightly lower than the numbers in the script summary.
- Time entries are assigned to the user whose e-mail address is in the file. Use the same e-mail address in Solidtime as in Clockify, or the entries may not match your account.
- Spot-check a few totals per project after the import before you cancel Clockify.

## Privacy and safety

- Your API key is only read from the environment variable and sent to `api.clockify.me`. It is never written to disk.
- The `export` and `cache` folders contain your personal time data. They are listed in `.gitignore` so they do not end up in a repository by accident.
- Revoke the Clockify API key when you are done.

## Troubleshooting

| Problem | Solution |
|---|---|
| `The environment variable CLOCKIFY_API_KEY is not set` | Set the variable in the same terminal window you run the script from |
| `Clockify rejected the key (HTTP 401/403)` | Generate a new API key and check that you copied it completely |
| It keeps waiting | That is the rate limit at work. Leave it running, or stop it and start it again later |
| You want fresh data | Run with `--refresh`, or delete the `cache` folder |

## License

[MIT](LICENSE)
