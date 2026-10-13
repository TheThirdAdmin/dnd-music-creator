# Runpod backend

Render the music on rented cloud GPUs instead of a graphics card at home. [Runpod](https://www.runpod.io) serverless bills per second of rendering and nothing runs when the app is idle.

Contents: [Before you start](#before-you-start-avoid-a-surprise-bill) · [Cost model](#cost-model) · [Set up, automatic](#set-up-automatic-recommended) · [Set up, manual](#set-up-manual) · [Using it](#using-it) · [Updating and tearing down](#updating-the-image-and-tearing-down) · [Troubleshooting](#troubleshooting) · [Privacy and security](#privacy-and-security) · [Known limits](#known-limits)

## What this is and who it is for

You have no NVIDIA GPU (or it is busy with games), you still want generated music, and you accept a small pay-as-you-go bill.

- The app (conductor) stays on your computer. Only the music generator moves to Runpod.
- Your computer needs Docker and nothing else: no GPU, no NVIDIA driver, no 12 GB generator image.
- Runpod starts a worker when a track is needed and stops it 5 s after the last one. A track takes 22-45 s to render once a worker is up; a worker that has been idle takes 1-3 minutes to start.
- Scenes you have already rendered play from your library with no Runpod cost.

**Read this whole page before you set it up.** The limits and the ways it can cost you money are spread through it, and the app's safety brakes are a second line behind your own account settings.

If you have a suitable local GPU, use that instead: it is free per track. See the [README](../README.md#install).

**All cost figures in this page are estimates**: measured on one account on 2026-10-13 (one account, one afternoon) and priced at the Runpod list price that day. The app shows estimates too; Runpod's own billing page is the truth, and it lags by 10 minutes or more.

## Before you start: avoid a surprise bill

**1. Turn auto-pay off.** Runpod console, Settings, Billing. With auto-pay on, a bug, a runaway job or a leaked key can keep reloading your card. With it off, the account stops at $0 and the most you can ever lose is the balance you loaded. The setup script asks you to confirm this before it does anything, and Settings > Runpod in the app repeats it.

**2. Load a small fixed amount and set the low-balance email.** Start with $5. At roughly $0.03-0.05 per track (see [Cost model](#cost-model)):

| | |
|---|---|
| $5 / $0.05 per track | 100 tracks |
| $5 / $0.03 per track | about 167 tracks |
| The network volume, per month | about $1.05 (15 GB at $0.07 per GB, estimate) |
| Left for tracks in the first month | $3.95, about 79 to 132 tracks |

Most tracks cost less than $0.03 when the worker is already warm ($0.008-0.010 each, measured). Planning with $0.03-0.05 leaves a margin. Set Runpod's low-balance email (Settings, Billing) so you hear before it reaches zero. With only $5 loaded, also lower the app's own **Warn below** to about $2 (Settings > Runpod), or its default of $5 warns at once.

**3. Use a Restricted API key scoped to the one endpoint.** The app needs read/write on the endpoint and read-only on everything else (it reads your balance). It never needs the All-access key; that key is only for creating the endpoint and tearing it down, and you delete it afterwards.

**4. Know the app's own brakes.** They are a second line, not the first; the balance you loaded is the real limit.

| Brake | Default | Where |
|---|---|---|
| Stop below (balance floor): no new jobs when the balance is under it | $1.00 | Settings > Runpod > Stop below |
| Warn below: a warning banner and tracks-left count | $5.00 | Settings > Runpod > Warn below |
| Daily cap: no new jobs once today's estimated spend reaches it | none | Settings > Runpod > Daily cap |
| 3 failed jobs in a row: no new jobs until you press **Reset after errors** | on | fixed |

With a $5 balance, the default Warn below ($5.00) shows the low-balance banner straight away. Lower it to $2 or so.

**5. Know what bills when you are not using the app.**

| Item | Cost | Where to look |
|---|---|---|
| Network volume (holds the 12.7 GB of models) | about $1.05/month for 15 GB, hourly, even when idle | console, Storage |
| An endpoint with workers 0 | nothing, but delete it if you are done | console, Serverless |
| Any pod you start by hand | per hour until you stop it | console, Pods |

Check the lot in the Runpod console under Billing, and Serverless, Storage and Pods for anything left running.

## Cost model

Per track: `(render seconds + 5 s idle tail + start-up seconds when this job woke the worker) x pool rate per second`. The app applies the same formula to show "last track" and totals; it counts start-up time as billed, which is an upper bound (cold-start billing has not been checked against Runpod's invoice).

Pools the setup script uses (rate read with `list-gpu-types` on 2026-10-13, serverless "flex" price):

| Pool | GPUs | $/hour | $/minute |
|---|---|---|---|
| `AMPERE_16` (default) | RTX A4000, RTX A4500 | 0.58 | 0.0097 |
| `ADA_24` (optional, add in the console) | RTX 4090 | 1.10 | 0.018 |

Measured on an RTX A4500, 60 s track (one account, one afternoon):

| Quality | Render | Job time once warm | Warm track incl. 5 s tail | Extra when it woke the worker (158 s start) |
|---|---|---|---|---|
| 1 | 22 s | about 33 s | $0.006 | + $0.025 |
| 2 | 39 s | 50 s | $0.009 | + $0.025 |
| 3 | 45 s | 56 s | $0.010 | + $0.025 |

"Job time" is render plus about 11 s of model loading. A live run through the setup script showed one cold track: rendered in 22 s, waited 158 s for a worker, about $0.03 (estimated).

**Illustration, 3 hour session.** Assumptions (mine, not measured): 60 new 60 s tracks are rendered, and the worker wakes from idle 6 times. Everything else plays from the library.

| Quality | 60 warm tracks | 6 wake-ups | Total |
|---|---|---|---|
| 1 | $0.37 | $0.15 | about $0.52 |
| 2 | $0.53 | $0.15 | about $0.69 |
| 3 | $0.59 | $0.15 | about $0.74 |

Double it for the 4090 pool (it renders faster, so the real figure is a little less than double).

**One-off costs.** The first start on a new endpoint pulls the image (about 8 GB, onto the worker's container disk) and downloads the 12.7 GB of models onto the network volume: 6-10 minutes, about $0.06 (first job: 354 s of execution at $0.58/hour). The whole first test in the measurements cost $0.07. After that the models stay on the volume. Without a volume they would download on every cold start, which is why the setup creates one.

## Set up, automatic (recommended)

You need the app's install folder (the README's install steps; `./deploy.sh --runpod` replaces the normal `./deploy.sh`), a Runpod account (sign up at [runpod.io](https://www.runpod.io); the console is at console.runpod.io) with a small balance and auto-pay off, and a temporary All-access API key (console, Settings, API Keys, Create API key, type All). To see the plan and price first without creating anything, run `RUNPOD_SETUP_DRY_RUN=1 ./deploy.sh --runpod`.

```bash
./deploy.sh --runpod
```

This installs the app **without** a generator container, then runs `scripts/runpod_setup.py` inside the conductor image. `--runpod` cannot be combined with `--gpu`, `--cpu-generator`, `--generator` or `--generator-only`. With `-y` the setup is not run (it asks questions); do it later with the command under [Run it again](#run-it-again).

Add `--image REF` to the setup script, or set `RUNPOD_IMAGE`, to use another image (see [Updating](#updating-the-image-and-tearing-down)). With `RUNPOD_SETUP_DRY_RUN=1 ./deploy.sh --runpod` (or `--dry-run` on the script) it prints the plan and price and creates nothing.

What it asks, in order:

| Step | What happens | Skippable |
|---|---|---|
| Image check | Anonymous check that the image exists on ghcr.io. If not, it stops before creating anything. | no |
| "Have you turned off auto-pay?" | Answer `y` only if you have. Anything else aborts with nothing created. | no |
| Temporary All-access key (hidden input) | Used in memory to create the resources, never saved. | Enter: it prints the manual steps instead ([path B](#set-up-manual)) |
| Create, read back | Creates template, network volume, endpoint; reads them back and lists any setting that differs. If a create fails it deletes what it made and falls back to the manual steps. | |
| Restricted key (hidden input) | The script waits here while you create it in the console (Runpod cannot mint keys through the API; the endpoint now exists, so you can pick it); permissions below. Saved to `runpod.json`. | Enter: add it later in Settings > Runpod |
| Test job | One 30 s quality-1 track. First run is several minutes. Prints cold-start, render time and estimated cost. | runs only if you gave the Restricted key |

Keys are read with a hidden prompt only: never from the command line, never printed, never logged.

**What it creates** (all in your Runpod account, ids recorded in `runpod.json` for teardown):

| Resource | Setting |
|---|---|
| Template `dnd-music-generator` | image `ghcr.io/thethirdadmin/dnd-music-generator:m8-rc1`, 20 GB container disk, env `MODEL_DIR=/runpod-volume`, no ports |
| Network volume `dnd-music-models` | 15 GB, data centre EU-RO-1 |
| Endpoint `dnd-music` | queue type, GPUs RTX A4000 or A4500 (pool `AMPERE_16`), CUDA 13.0 or newer, workers min 0 / max 1, idle timeout 5 s, execution timeout 600 s, FlashBoot on, queue-delay scaling 4 s |

**Restricted key permissions:** toggle Restricted; `api.runpod.io/graphql` Read only (gives the balance); `api.runpod.ai` Restricted with your endpoint on Read / Write and all others None. Exact clicks are under [Set up, manual](#set-up-manual), step 4. Without the GraphQL read the app shows "Balance read failed" but still renders tracks.

**Exit codes** (the script, and what `deploy.sh` reports): `0` configured, `10` finished by hand or keys skipped (finish in Settings > Runpod), `20` aborted, nothing created or the state is described in the output.

The image default is the only published tag, `m8-rc1`; the package is public so Runpod can pull it with no registry credentials.

When it finishes it prints the endpoint id, the worst-case hourly cost (estimate), the teardown command, and a reminder to delete the temporary All-access key in the console. Do that now.

### Run it again

Re-running creates a second set of resources; tear down the first ([below](#updating-the-image-and-tearing-down)) before you do. From the install folder:

```bash
docker compose run --rm -v "$PWD/scripts:/scripts:ro" conductor \
  python /scripts/runpod_setup.py --data-dir /data
```

Options: `--image REF`, `--pool AMPERE_16`, `--data-center EU-RO-1`, `--volume-gb 15` (0 for no volume: models re-download on every cold start), `--manual`, `--dry-run`, `--teardown`.

## Set up, manual

Used when you skip the key, the key cannot create endpoints, or a create fails. `scripts/runpod_setup.py --manual` (or `./deploy.sh --runpod`, then skip the key) prints the same steps. Console labels may change; the settings are what matter.

1. **Template.** Console, Serverless, then Templates, New template. Type serverless. Container image `ghcr.io/thethirdadmin/dnd-music-generator:m8-rc1`, container disk 20 GB, environment variable `MODEL_DIR` = `/runpod-volume`, no exposed ports.
2. **Network volume.** Console, Storage, New network volume. 15 GB, data centre **EU-RO-1**. Pick the data centre first: the volume only mounts on workers in the same data centre, so the endpoint must be allowed to run there.
3. **Endpoint.** Console, Serverless, New endpoint, type **Queue**.
   - GPU types: RTX A4000 and RTX A4500 (the 16 GB "Ampere" group). Optionally also RTX 4090 (24 GB, $1.10/hour, 2x the cost; the app's cost rows keep using $0.58 and under-report when a 4090 runs).
   - **Minimum CUDA version 13.0.** Do not leave it on any/12.8: those hosts crash (see [Troubleshooting](#troubleshooting)).
   - Workers: min **0**, max **1** (the app runs one job at a time; a second worker would sit idle and bill).
   - Idle timeout **5 s**, execution timeout **600 s**, **FlashBoot** on, queue-delay scaling 4 s.
   - Attach the template and the network volume.
4. **Restricted key.** Create the endpoint first: the key's endpoint list only shows endpoints that exist. Console, Settings, API Keys, Create API key.
   - Top toggle (All / Restricted / Read only): choose **Restricted**.
   - Group `api.runpod.io/graphql` (None / Read / Write / Read only): choose **Read only**. This is what lets the app read your balance.
   - Group `api.runpod.ai` (None / Restricted / Read / Write / Read only): choose **Restricted**. An "Endpoint permissions" list appears (search box "Search by name or ID"; each endpoint shows its name, ID and "GET and POST requests to this endpoint"). Set your endpoint to **Read / Write** and leave every other endpoint on **None**.
   - Click the create button (it reads **Update** when you edit an existing key). Copy the key now; Runpod shows it once.
5. **App.** Settings > Runpod in the app: endpoint id (from the endpoint's page), API key, Save, then Test. Or give the id and key to the setup script prompts.

## Using it

**Status page, Runpod rows:**

| Row | Meaning |
|---|---|
| `<endpoint id> · idle / warming up / ready / blocked / error` | Endpoint state. Green dot = ready, red = error or blocked. |
| Activity line (only while a job is in flight) | `Waiting for a worker to start (N starting) · 2m10s`; `No GPU available right now (N throttled)`; `Worker ready, starting the job`; `Rendering · 0m25s`; `Can't read Runpod health right now`. Appended `+N more queued` when jobs wait. |
| `Last track (estimated): $0.03 · rendered in 22 s · waited 158 s for a worker to start` | Estimated cost of the last render, render time, and wait for a worker (only shown if it was over 5 s). |
| `This session (estimated): $0.18 · today: $0.52 · this month: $4.10` | Sums of the estimates. Session starts when the conductor starts. |
| `Balance: $14.75 (from Runpod) · about 490 tracks left (estimated)` | Balance read every minute from your account; tracks left uses your average track cost. |

**Warm-up banner.** At the top of the page: "Warming up the generator on Runpod." On a new endpoint it says the first start can take 6-10 minutes; once the app has seen a cold start it quotes the measured one ("First track in about 158 s"). The playing scene keeps playing from the library meanwhile. If the activity line shows Rendering for more than 5 minutes it adds "longer than usual" (and, on the very first run, that the models are being downloaded).

**Track progress bar.** A thin bar under the volume slider shows how far into the playing track you are; it is not render progress.

**Balance and "estimated".** Costs are computed from job timings and the rate recorded at setup (0.58 $/hour for `AMPERE_16`). Runpod's invoice can differ, and the billing API shows rows only after 10+ minutes. The balance is the real number, read from Runpod.

**Blocked messages** (the state turns to `blocked` and the same text appears as a banner; no jobs are submitted, tracks from the library still play):

| Text | Why | Fix |
|---|---|---|
| `Runpod balance is below your floor of $1.00` | balance under "Stop below" | load more, or lower the floor |
| `Daily spend cap of $X reached` | today's estimates reached the cap | wait for tomorrow, raise or clear the cap |
| `N jobs failed in a row; check the endpoint, then Reset in Settings` | three failed jobs | see [Troubleshooting](#troubleshooting), then press **Reset after errors** |

**Settings > Runpod.** Endpoint id; API key (write-only: the field is empty after saving and shows "Saved" as its placeholder; the app never sends the key back); Warn below; Stop below; Daily cap (empty for none); Budget (used only when Runpod cannot report a balance, see below); **Save**, **Test** (asks the endpoint's health and your balance), **Reset after errors**.

**What the app renders on Runpod.** It keeps more music ready than with a local GPU: 18 minutes for the playing scene and 10 minutes for the next one (local: 10 and 6; both never below 3 minutes), because a render can take 1-3 minutes to start. The DM's **Next** button (tooltip "Next track") also queues one render for the playing scene, so a library that looks full still wakes Runpod now and then. Press **Stop playing & rendering** when you are done: nothing is submitted until you press **Start rendering**.

## Updating the image and tearing down

**Updating the image.** The image tag is fixed in the template. To move to a new one:

1. Check the tag exists: `docker manifest inspect ghcr.io/thethirdadmin/dnd-music-generator:<tag>`.
2. Runpod console, Serverless, open the endpoint, Manage, edit the endpoint (or edit its template under Templates) and change the container image tag. Save. The next cold start pulls the new image; the models on the volume are kept.
3. Check CUDA is still 13.0 or newer.

For a fresh install of another tag, `RUNPOD_IMAGE=ghcr.io/…:<tag> ./deploy.sh --runpod` or `--image` on the script. Unpublished tag: the script's anonymous check says so before anything is created. The tag `m8-rc1` is the only one published today (see [Known limits](#known-limits)).

**Tearing down.** From the install folder, with a temporary All-access key:

```bash
docker compose run --rm -v "$PWD/scripts:/scripts:ro" conductor \
  python /scripts/runpod_setup.py --data-dir /data --teardown
```

It lists what it will delete (endpoint, template, volume ids from `runpod.json`), asks `y/N`, asks for the key, deletes, then checks each is gone. On success the app is left unconfigured.

`./uninstall.sh` offers the same teardown interactively (type `yes`). With `-y` it **never** deletes: it prints the leftover ids and where to delete them.

**What stays billable after an uninstall that skipped teardown:**

| Left over | Cost |
|---|---|
| Network volume | about $1.05/month, every month, until deleted |
| Endpoint with workers 0 | nothing; delete it anyway |
| Template | nothing |

Check the console after: Serverless (no endpoints), Storage (no volumes), Billing. Then delete the Restricted key and any temporary All-access key under Settings, API Keys.

## Troubleshooting

| What you see | Cause and fix |
|---|---|
| **Test** in Settings > Runpod, or the Generators list, shows `Runpod key rejected (check Settings)` (HTTP 401/403) | Wrong or deleted key, or a Restricted key without access to this endpoint. Create a new Restricted key (read/write on the endpoint) and paste it in Settings > Runpod. |
| Job hangs for 10 minutes, then fails. Endpoint shows a worker running; console worker log: `NVIDIA driver too old` and ComfyUI exits. `/health` still looks fine. | The endpoint landed on a host with CUDA 12.8. Console, Serverless, endpoint, Manage, edit: set the minimum CUDA version to **13.0** and save. Endpoints made by the setup script already have it. Then **Reset after errors**. |
| Activity says `No GPU available right now (N throttled)`; stays that way | No GPU stock in that data centre ("Low" stock is common for A4000/A4500). It usually clears in minutes: wait, or retry later. To widen, add the RTX 4090 pool in the endpoint's GPU list (costs about 2x; the cost rows will under-report). |
| Warm-up banner lasts more than 10 minutes | Console, Serverless, endpoint, **Workers**, open a worker, then Logs: container pull progress, model download and ComfyUI start are all logged there. A slow network can exceed the 600 s timeout during the first model download; it resumes on the next job. The app cannot show this log (the job API does not carry it). |
| Job fails with an error about output size | Runpod's `/run` result limit is 10 MB. The worker transcodes to Ogg when audio exceeds 7 MiB, but that path was not exercised live. Keep tracks at 60 s or less at quality 1 (8.6 MB as base64 at 60 s, quality 1: close to the limit). |
| Balance row reads `(from your budget)` | Runpod would not report a balance for this key, and you set a **Budget**. The app then shows budget minus what it estimates you have spent since you saved the budget. Fix the key (read-only on account) to get `(from Runpod)`. If no budget is set the row says `unknown`; a line "Balance read failed: key cannot read the balance" explains. |
| Blocked: `3 jobs failed in a row` | Something is wrong with the endpoint; look at the console (Serverless, endpoint, Workers, Logs), fix, then **Reset after errors**. Pressing it without a fix just burns three more jobs. |
| Worker never starts and the console says `IMAGE_NOT_FOUND` or a pull error | The image tag is not published or not public. `docker manifest inspect <image>` should work without logging in. Use `--image` or fix the tag in the template. |
| Three workers listed with max 1 | Seen once during a cold burst; only one runs the job. Harmless. |

## Privacy and security

- **The key** lives only in `runpod.json` in your library folder (`~/dnd-music-creator/data` by default), mode 0600, written by the app or the setup script. The setup script's temporary All-access key is used in memory and dropped. The key is never in the web page's state (`/api/state`), exports, logs or error text.
- **What is sent to Runpod:** for each track, the prompt tags (the generated scene description), lyrics text if any, duration, quality and seed, to your endpoint. Your balance is requested from Runpod's account API with the key. No account names, scene names, library contents, player data or IP addresses of your players are sent. Runpod sees the prompts like any host would.
- **Runpod's key in the GraphQL URL.** The balance query takes the key as a URL parameter (Runpod's design); the app silences HTTP request logging so it does not appear in logs.
- **The generator image** is public on ghcr.io and contains no secrets. Runpod needs no registry login.
- Never paste the All-access key into the app. Delete it from the console after setup.

## Known limits

Not done yet; none of these cost money unexpectedly, but they are rough edges:

- **A dead ComfyUI makes jobs hang** until the 600 s timeout instead of failing at once (`wait_ready()` in `generator/runpod_worker.py` needs a fail-fast; needs an image rebuild). The 3-failure brake and the cost per hung job (about $0.10 at 600 s) bound it.
- **One job at a time.** Concurrency 2 needs a conductor change.
- **`rate_per_hr` and the warm-up targets are not editable in the UI.** The rate is set at setup (0.58) and in `runpod.json`.
- **The release tag is still `m8-rc1`**; there is no `v*` release flow yet.
- **Worker log detail is not reachable through the job API**; use the console.
- **Up to 3 workers may appear with max 1** during a cold burst.
- **The first-start time on a slow network is unverified** (one live run, 6-10 minutes).
- **The transcode-to-Ogg path is not exercised live**, and **cold-start billing is unverified** against Runpod's invoice (the app's figure is an upper bound).
