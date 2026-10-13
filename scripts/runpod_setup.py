#!/usr/bin/env python3
"""Create the Runpod serverless endpoint for dnd-music-creator, or guide you through doing it by hand.

  scripts/runpod_setup.py --data-dir DATA [--image REF] [--pool AMPERE_16] [--volume-gb 15]
                          [--dry-run] [--manual] [--teardown]

Exit codes: 0 configured, 10 finished by hand or skipped keys (set up in Settings > Runpod), 20 aborted.

Keys: read with getpass only, never from argv, never printed or logged. The temporary All-access key is
used in memory and dropped; <DATA>/runpod.json (0600) holds the restricted key only.

Request shapes come from the Runpod REST OpenAPI spec (https://rest.runpod.io/v1/openapi.json, read
2026-10-13) but have not been exercised live yet. REST has no catalogue or pricing and no GPU "pools":
the pool name maps to exact GPU type ids below, the rate is a measured estimate, and a failed create
(no capacity, no permission) falls back to the manual steps. Cost figures are estimates (cold-start
billing unverified).
"""
import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dndmusic.runpod.config import FILE, RunpodConfig, save_config  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)   # httpx logs request URLs

MGMT_BASE = os.environ.get("RUNPOD_MGMT_BASE", "https://rest.runpod.io/v1")   # management API (test/demo override)
JOB_BASE = os.environ.get("RUNPOD_API_BASE", "https://api.runpod.ai/v2")      # job API, same override as the conductor
IMAGE_BASE = os.environ.get("RUNPOD_IMAGE", "ghcr.io/thethirdadmin/dnd-music-generator")
POOL = "AMPERE_16"
GPU_TYPES = {"AMPERE_16": ["NVIDIA RTX A4000", "NVIDIA RTX A4500"]}   # pool name -> REST gpuTypeIds
RATE_ESTIMATE = {"AMPERE_16": 0.58}   # $/hr measured 2026-10-13 (list-gpu-types); no pricing in REST
DATA_CENTER = "EU-RO-1"               # measured working data centre; --data-center overrides
VOLUME_GB = 15
VOLUME_USD_GB_MONTH = 0.07            # estimate
MAX_WORKERS, IDLE_S, EXEC_TIMEOUT_MS, MIN_CUDA = 1, 5, 600_000, "13.0"   # 12.8 hosts have a driver too old for the image torch
DISK_GB = 20
OK, ABORTED = 0, 20
MANUAL_DONE = 10
HINT = "Runpod is not set up: open Settings > Runpod or run scripts/runpod_setup.py"
AUTOPAY_Q = "Have you turned off auto-pay in Runpod (Settings > Billing)? [y/N] "


class SetupError(Exception):
    """Message is safe to print (never carries a key)."""


class ApiError(SetupError):
    def __init__(self, status: int | None, what: str):
        self.status = status
        super().__init__(f"{what}: " + (f"HTTP {status}" if status else "unreachable"))


@dataclass
class Plan:
    rate_per_hr: float
    worst_case_hr: float
    volume_gb: int
    volume_per_month: float
    pool: str = POOL
    data_center: str | None = DATA_CENTER
    max_workers: int = MAX_WORKERS
    gpu_types: tuple = ()


@dataclass
class Created:
    template_id: str
    endpoint_id: str
    volume_id: str | None = None


class Api:
    """Management API over httpx. Errors carry the status code only."""

    def __init__(self, key: str, base: str = MGMT_BASE, transport=None):
        self._c = httpx.Client(base_url=base, timeout=30.0, transport=transport,
                               headers={"Authorization": f"Bearer {key}"})

    def call(self, method: str, path: str, what: str, **kw):
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError:
            raise ApiError(None, what) from None
        if r.status_code >= 400:
            raise ApiError(r.status_code, what)
        try:
            return r.json() if r.content else {}
        except ValueError:
            raise SetupError(f"{what}: reply is not JSON") from None

    def close(self) -> None:
        self._c.close()


def plan(pool: str = POOL, volume_gb: int = VOLUME_GB, max_workers: int = MAX_WORKERS,
         data_center: str = DATA_CENTER) -> Plan:
    if pool not in GPU_TYPES:
        raise SetupError(f"unknown pool {pool}: known pools are {', '.join(GPU_TYPES)}")
    rate = RATE_ESTIMATE[pool]
    return Plan(rate, round(rate * max_workers, 4), volume_gb, round(volume_gb * VOLUME_USD_GB_MONTH, 2),
                pool, data_center, max_workers, tuple(GPU_TYPES[pool]))


def endpoint_body(p: Plan, template_id: str, volume_id: str | None) -> dict:
    body = {"name": "dnd-music", "templateId": template_id, "computeType": "GPU",
            "gpuTypeIds": list(p.gpu_types), "gpuCount": 1, "minCudaVersion": MIN_CUDA,
            "workersMin": 0, "workersMax": p.max_workers, "idleTimeout": IDLE_S,
            "executionTimeoutMs": EXEC_TIMEOUT_MS, "flashboot": True,
            "scalerType": "QUEUE_DELAY", "scalerValue": 4}
    if volume_id:
        body["networkVolumeId"] = volume_id
        body["dataCenterIds"] = [p.data_center]
    return body


def template_body(p: Plan, image: str) -> dict:
    model_dir = "/runpod-volume" if p.volume_gb else "/models"   # where the volume mounts on serverless
    return {"name": "dnd-music-generator", "imageName": image, "isServerless": True,
            "containerDiskInGb": DISK_GB, "env": {"MODEL_DIR": model_dir}, "ports": [], "category": "NVIDIA"}


def create_all(api: Api, p: Plan, image: str) -> Created:
    """Template, volume (if any), endpoint. On failure deletes what this call created, then re-raises."""
    done: list[tuple[str, str]] = []   # (path, id) in creation order
    volume_id = None
    try:
        t = api.call("POST", "/templates", "create template", json=template_body(p, image))
        template_id = str(t["id"])
        done.append(("/templates", template_id))
        if p.volume_gb:
            v = api.call("POST", "/networkvolumes", "create network volume",
                         json={"name": "dnd-music-models", "size": p.volume_gb, "dataCenterId": p.data_center})
            volume_id = str(v["id"])
            done.append(("/networkvolumes", volume_id))
        e = api.call("POST", "/endpoints", "create endpoint", json=endpoint_body(p, template_id, volume_id))
        endpoint_id = str(e["id"])
    except (ApiError, KeyError, TypeError) as err:
        for path, rid in reversed(done):   # best effort; a failed cleanup is reported by the caller's text
            try:
                api.call("DELETE", f"{path}/{rid}", "cleanup")
            except SetupError:
                print(f"  could not delete {path.strip('/')} {rid}: remove it in the Runpod console")
        if isinstance(err, ApiError):
            raise
        raise SetupError("create: unexpected reply") from None
    return Created(template_id, endpoint_id, volume_id)


def verify(api: Api, p: Plan, c: Created, image: str) -> list[str]:
    """Read back what was created and list fields that differ from what we set (a warning, not a failure)."""
    bad = []
    t = api.call("GET", f"/templates/{c.template_id}", "read template")
    if t.get("imageName", image) != image:
        bad.append("template imageName")
    e = api.call("GET", f"/endpoints/{c.endpoint_id}", "read endpoint")
    want = {"workersMin": 0, "workersMax": p.max_workers, "idleTimeout": IDLE_S,
            "executionTimeoutMs": EXEC_TIMEOUT_MS, "templateId": c.template_id, "minCudaVersion": MIN_CUDA}
    bad += [k for k, v in want.items() if k in e and e[k] != v]
    if c.volume_id:
        v = api.call("GET", f"/networkvolumes/{c.volume_id}", "read network volume")
        if v.get("size", p.volume_gb) != p.volume_gb:
            bad.append("volume size")
    return bad


def manual_steps(p: Plan, image: str) -> str:
    vol = (f"  2. Network volume (recommended): {p.volume_gb} GB"
           + f", data centre {p.data_center}"
           + f" (estimate ${p.volume_per_month:.2f}/month). The models download once and stay on it.\n"
           if p.volume_gb else "  2. No network volume: the models download on every cold start.\n")
    return (
        "Set up the Runpod endpoint by hand (Runpod console > Serverless):\n"
        "  Keep auto-pay switched off in Runpod (Settings > Billing): the balance you load is the most you can spend.\n"
        f"  1. Template: serverless, container image {image}, container disk {DISK_GB} GB,\n"
        f"     environment variable MODEL_DIR={'/runpod-volume' if p.volume_gb else '/models'}, no ports.\n"
        + vol +
        f"  3. Endpoint: type Queue, GPU types {' or '.join(p.gpu_types)} (pool {p.pool}), CUDA {MIN_CUDA} or newer, min 0 workers, max {p.max_workers} worker,\n"
        f"     idle timeout {IDLE_S} s, execution timeout {EXEC_TIMEOUT_MS // 1000} s, FlashBoot on, queue delay scaling 4 s,\n"
        "     attach the template (and the volume).\n"
        "  4. Settings > API Keys: create a Restricted key: read/write on the endpoint, and read-only on\n"
        "     everything else (the balance is read from your account).\n"
        f"  Estimate: ${p.rate_per_hr:.2f}/hr while a worker runs, at most ${p.worst_case_hr:.2f}/hr with max {p.max_workers}.\n"
    )


def intro(p: Plan, image: str) -> str:
    src = "measured rate, not read from Runpod"
    s = (f"Runpod serverless endpoint, pool {p.pool} ({' or '.join(p.gpu_types)}), image {image}\n"
         f"  Price (estimate, {src}): ${p.rate_per_hr:.2f}/hr per running worker\n"
         f"  Worst case (estimate): ${p.worst_case_hr:.2f}/hr with max {p.max_workers} worker: "
         f"only while a track renders, plus a {IDLE_S} s idle tail; nothing runs otherwise\n")
    if p.volume_gb:
        s += (f"  Network volume: {p.volume_gb} GB, standing cost about ${p.volume_per_month:.2f}/month (estimate), "
              "billed even when idle\n")
    s += "  First track: about 10 minutes of worker time (image pull and model download), later cold starts about 10-12 s\n"
    return s


# ---- config file -------------------------------------------------------------------------------------

def write_config(data_dir: Path, endpoint_id: str, key: str, rate: float) -> None:
    save_config(Path(data_dir), RunpodConfig(endpoint_id, key, rate, idle_timeout_s=float(IDLE_S)))


def record_created(data_dir: Path, c: Created) -> None:
    """Add the ids teardown needs under `created` (load_config ignores unknown keys). Mode stays 0600."""
    p = Path(data_dir) / FILE
    doc = json.loads(p.read_text())
    doc["created"] = {k: v for k, v in (("template_id", c.template_id), ("endpoint_id", c.endpoint_id),
                                        ("volume_id", c.volume_id)) if v}
    fd = os.open(p.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    os.replace(p.with_suffix(".tmp"), p)


# ---- prompts -----------------------------------------------------------------------------------------

def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise SetupError("no terminal to ask on") from None


def ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise SetupError("no terminal to ask on") from None


def manual_finish(data_dir: Path, p: Plan, image: str, why: str, rate: float) -> int:
    """Path B: print the console steps, then take the endpoint id and restricted key (both skippable)."""
    print(f"\n{why}\n")
    print(manual_steps(p, image))
    eid = ""
    for _ in range(3):
        eid = ask("Endpoint id (Enter to skip; enter it later in Settings > Runpod): ")
        if not eid or re.fullmatch(r"[A-Za-z0-9_-]{3,64}", eid):
            break
        print("  that does not look like an endpoint id")
        eid = ""
    key = ask_secret("Restricted API key (input hidden; Enter to skip): ") if eid else ""
    if not eid:
        print("Nothing saved. Enter the endpoint id and key later in Settings > Runpod.")
        return MANUAL_DONE
    write_config(data_dir, eid, key, rate)
    if key:
        print(f"Saved endpoint {eid} and key to {Path(data_dir) / FILE}.")
    else:
        print(f"Saved endpoint {eid}. Add the restricted key in Settings > Runpod.")
    return MANUAL_DONE


# ---- test job ----------------------------------------------------------------------------------------

def run_test_job(cfg: RunpodConfig, client=None, timeout_s: int = 1500, poll_s: float = 5.0) -> dict:
    """One 30 s quality-1 job through RunpodClient. Returns delay/exec seconds, or {"error": text}."""
    from dndmusic.models import Prompt
    from dndmusic.runpod.client import RunpodClient

    async def go() -> dict:
        c = RunpodClient(cfg, client=client)
        try:
            jid = await c.submit(Prompt("calm acoustic guitar, tavern"), 30, None, 1)
            t0, last = time.monotonic(), 0.0
            while time.monotonic() - t0 < timeout_s:
                st = await c.status(jid)
                if st.state == "done":
                    m = c.last_run.get(jid)
                    return {"delay_s": (m.delay_ms if m else 0) / 1000, "exec_s": (m.exec_ms if m else 0) / 1000}
                if st.state == "failed":
                    return {"error": st.error or "job failed"}
                if time.monotonic() - last >= 15:
                    print(f"  test job {st.state}, {time.monotonic() - t0:.0f} s")
                    last = time.monotonic()
                await asyncio.sleep(poll_s)
            return {"error": f"no result after {timeout_s} s"}
        finally:
            await c.aclose()

    try:
        return asyncio.run(go())
    except Exception as e:   # GeneratorError text never carries the key
        return {"error": str(e)[:200]}


# ---- teardown ----------------------------------------------------------------------------------------

def teardown(data_dir: Path, transport=None) -> int:
    p = Path(data_dir) / FILE
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        doc = {}
    created = doc.get("created") or {}
    if not created:
        print(f"No created ids in {p}: nothing to tear down (resources made by hand: delete them in the console).")
        return OK
    plan_ = [("/endpoints", created.get("endpoint_id")), ("/templates", created.get("template_id")),
             ("/networkvolumes", created.get("volume_id"))]
    plan_ = [(path, i) for path, i in plan_ if i]
    print("Will delete:")
    for path, i in plan_:
        print(f"  {path.strip('/')} {i}")
    if ask("Delete these from Runpod? [y/N] ").lower() != "y":
        print("Aborted: nothing deleted.")
        return ABORTED
    key = ask_secret("Temporary All-access Runpod key (input hidden): ")
    if not key:
        print("Aborted: no key, nothing deleted.")
        return ABORTED
    api = Api(key, transport=transport)
    del key
    left = []
    try:
        for path, i in plan_:
            try:
                api.call("DELETE", f"{path}/{i}", f"delete {path.strip('/')}")
            except ApiError as e:
                if e.status != 404:
                    print(f"  {e}")
                    left.append((path, i))
                    continue
            try:
                api.call("GET", f"{path}/{i}", "verify")
                left.append((path, i))
            except ApiError as e:
                if e.status != 404:
                    left.append((path, i))
    finally:
        api.close()
    if left:
        print("Still present (remove in the Runpod console): " + ", ".join(f"{pa.strip('/')} {i}" for pa, i in left))
        return ABORTED
    doc.pop("created", None)   # the endpoint is gone: clear the dead reference, keep the other settings
    doc["endpoint_id"], doc["api_key"] = "", ""
    fd = os.open(p.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    os.replace(p.with_suffix(".tmp"), p)
    print("Deleted and verified gone. Runpod is no longer configured here (Settings > Runpod).")
    return OK


# ---- main flow ---------------------------------------------------------------------------------------

# Only tag published to ghcr.io so far; bump when the release workflow publishes a v* tag.
DEFAULT_IMAGE_TAG = "m8-rc1"
MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json"])


def default_image() -> str:
    """RUNPOD_IMAGE is the image name (tag added) or a full reference that already has a tag."""
    if ":" in IMAGE_BASE.rsplit("/", 1)[-1]:
        return IMAGE_BASE
    return f"{IMAGE_BASE}:{DEFAULT_IMAGE_TAG}"


def check_image(image: str, transport=None) -> bool | None:
    """Anonymous manifest check for ghcr.io images: True found, False not found/not public,
    None not checked (other registry or network trouble). No Runpod key is involved."""
    m = re.fullmatch(r"ghcr\.io/([^:@]+):([^:/@]+)", image)
    if not m:
        return None
    name, tag = m.groups()
    try:
        with httpx.Client(timeout=15.0, transport=transport) as c:
            r = c.get("https://ghcr.io/token", params={"scope": f"repository:{name}:pull"})
            if r.status_code in (401, 403, 404):
                return False
            r.raise_for_status()
            token = r.json().get("token", "")
            r = c.head(f"https://ghcr.io/v2/{name}/manifests/{tag}",
                       headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT})
    except (httpx.HTTPError, ValueError):
        print("  warning: could not check the image on ghcr.io (network); continuing")
        return None
    if r.status_code in (401, 403, 404):
        return False
    return True if r.status_code < 400 else None


def run(args, transport=None) -> int:
    data_dir = Path(args.data_dir)
    if args.teardown:
        return teardown(data_dir, transport)
    image = args.image or default_image()
    dry = args.dry_run or os.environ.get("RUNPOD_SETUP_DRY_RUN") == "1"
    p = plan(args.pool, args.volume_gb, data_center=args.data_center)
    print(intro(p, image))
    if dry:
        print("Dry run: no keys asked, nothing created. Steps: confirm auto-pay is off; "
              "create template, volume, template, endpoint; make a restricted key; run a test job; save runpod.json.")
        return OK
    if check_image(image, transport) is False:
        print(f"image {image} was not found or is not public on ghcr.io (anonymous pull failed): "
              "pass --image REF or set RUNPOD_IMAGE")
        return ABORTED
    if ask(AUTOPAY_Q).lower() != "y":
        print("Aborted: turn auto-pay off first so the balance you load is the most Runpod can charge. "
              "Nothing was created.")
        return ABORTED
    if args.manual:
        return manual_finish(data_dir, p, image, "Manual setup chosen.", p.rate_per_hr)

    print("Creating the endpoint needs a temporary All-access key (Runpod console > Settings > API Keys). "
          "Delete it afterwards. It is used in memory only and never saved.")
    key = ask_secret("Temporary All-access key (input hidden; Enter to set up by hand): ")
    if not key:
        return manual_finish(data_dir, p, image, "No key entered: setting up by hand.", p.rate_per_hr)
    api = Api(key, transport=transport)
    del key
    created: Created | None = None
    try:
        try:
            api.call("GET", "/endpoints", "check key")
        except ApiError as e:
            why = "this key cannot create endpoints" if e.status in (401, 403) else str(e)
            return manual_finish(data_dir, p, image, why, p.rate_per_hr)
        print("[1/4] creating template, volume and endpoint")
        try:
            created = create_all(api, p, image)
        except SetupError as e:
            why = ("this key cannot create endpoints" if getattr(e, "status", None) in (401, 403)
                   else f"Cannot create automatically ({e}); nothing was left behind.")
            return manual_finish(data_dir, p, image, why, p.rate_per_hr)
        write_config(data_dir, created.endpoint_id, "", p.rate_per_hr)
        record_created(data_dir, created)
        print(f"  endpoint {created.endpoint_id}, template {created.template_id}"
              + (f", volume {created.volume_id}" if created.volume_id else ""))
        print("[2/4] reading it back")
        try:
            bad = verify(api, p, created, image)
            print("  settings match" if not bad else "  differs from what was set: " + ", ".join(bad)
                  + " (check the endpoint in the console)")
        except SetupError as e:
            print(f"  could not read back ({e})")
    finally:
        api.close()

    print("[3/4] restricted key: Runpod keys are created in the console, not through the API.\n"
          f"  Settings > API Keys > Create: type Restricted, read/write on endpoint {created.endpoint_id}, and read-only\n"
          "  on everything else (the balance is read from your account).")
    rkey = ask_secret("Restricted key (input hidden; Enter to skip and add it in Settings > Runpod): ")
    configured = False
    if rkey:
        write_config(data_dir, created.endpoint_id, rkey, p.rate_per_hr)
        cfg = RunpodConfig(created.endpoint_id, rkey, p.rate_per_hr, api_base=JOB_BASE)
        print("[4/4] test job (30 s track, quality 1; first run downloads the models, several minutes; "
              f"about ${(292 + 350) * p.rate_per_hr / 3600:.2f} estimate)")
        r = run_test_job(cfg)
        if "error" in r:
            print(f"  test job failed: {r['error']}. The endpoint exists; check it in Settings > Runpod.")
        else:
            cost = (r["delay_s"] + r["exec_s"]) * p.rate_per_hr / 3600
            print(f"  cold start {r['delay_s']:.0f} s, render {r['exec_s']:.0f} s, about ${cost:.2f} (estimate)")
        configured = True
    del rkey
    print(f"\nEndpoint {created.endpoint_id}: worst case ${p.worst_case_hr:.2f}/hr (estimate) while a track renders.")
    print(f"Tear down (deletes endpoint, template, volume): scripts/runpod_setup.py --data-dir {data_dir} --teardown")
    if not configured:
        print("Add the restricted key in Settings > Runpod to finish.")
    print("Delete the temporary All-access key in the Runpod console now.")
    return OK if configured else MANUAL_DONE


def main(argv=None, transport=None) -> int:
    ap = argparse.ArgumentParser(description="Set up the Runpod serverless endpoint (see header).")
    ap.add_argument("--data-dir", required=True, help="where runpod.json goes")
    ap.add_argument("--image", help="generator image reference (default: ghcr.io/thethirdadmin/dnd-music-generator:<version>)")
    ap.add_argument("--pool", default=POOL, help="GPU pool name, mapped to GPU types (AMPERE_16)")
    ap.add_argument("--data-center", default=DATA_CENTER, help="data centre for the network volume (default EU-RO-1)")
    ap.add_argument("--volume-gb", type=int, default=VOLUME_GB, help="network volume size, 0 for none (default 15)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, make no calls")
    ap.add_argument("--manual", action="store_true", help="skip creation, show the console steps")
    ap.add_argument("--teardown", action="store_true", help="delete what this script created")
    args = ap.parse_args(argv)
    try:
        return run(args, transport)
    except SetupError as e:
        print(f"Aborted: {e}")
        return ABORTED
    except KeyboardInterrupt:
        print("\nAborted.")
        return ABORTED


if __name__ == "__main__":
    sys.exit(main())
