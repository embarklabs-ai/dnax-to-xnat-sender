#!/usr/bin/env python3
"""
Launcher for DNAnexus XNAT Sender applet that runs the Docker-asset exporter container.
- Loads Docker image tar from the asset path
- Runs container with job security context (no user PAT)
- Auto-suffixes the report filename with a UTC timestamp so each run is unique
"""
import os
import subprocess
from datetime import datetime, timezone
import dxpy

DOCKER_TAR_PATH = "/opt/dnax2xnat/dnax2xnat-1.0.tar"
IMAGE_TAG = "dnax2xnat:1.0"
DEFAULT_REPORT = "export_report.csv"

def utc_stamp() -> str:
    """Return a compact UTC timestamp: YYYYMMDD_HHMMSS."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

@dxpy.entry_point("main")
def main(
    manifest,
    secrets,
    src_root_path,
    xnat_project=None,
    report_filename=DEFAULT_REPORT,
):
    workdir = os.getcwd()
    man_local = os.path.join(workdir, "manifest.yaml")
    dxpy.download_dxfile(manifest, man_local)

    sec_local = os.path.join(workdir, "secrets.yaml") # this is set up as a protected file directly on the DNAnexus platform
    dxpy.download_dxfile(secrets, sec_local)

    # Always append a UTC timestamp to the report filename
    report_filename = report_filename or DEFAULT_REPORT
    base, ext = os.path.splitext(report_filename)
    report_filename = f"{base}_{utc_stamp()}{ext}"

    if not os.path.exists(DOCKER_TAR_PATH):
        raise RuntimeError(f"Docker image tar not found at {DOCKER_TAR_PATH}")

    # Load image from asset tar
    subprocess.run(["docker", "load", "-i", DOCKER_TAR_PATH], check=True)
    # Log image + arch for sanity
    subprocess.run([
        "docker", "inspect", "--format",
        "Loaded image: {{.RepoTags}} (arch={{.Architecture}}/os={{.Os}})", IMAGE_TAG
    ], check=True)

    # Pass DNAnexus job env needed by dxpy/fsspec-dnanexus
    env_keys = [
        "DX_SECURITY_CONTEXT", "DX_APISERVER_HOST", "DX_APISERVER_PORT",
        "DX_APISERVER_PROTOCOL", "DX_PROJECT_CONTEXT_ID"
    ]
    docker_env = []
    for k in env_keys:
        v = os.environ.get(k)
        if v:
            docker_env += ["-e", f"{k}={v}"]

    report_local = os.path.join(workdir, report_filename)

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{workdir}:/work",
        *docker_env,
        IMAGE_TAG,
        "--manifest", "/work/manifest.yaml",
        "--secrets", "/work/secrets.yaml",
        "--src-root-path", src_root_path,
        "--report", f"/work/{report_filename}",
    ]
    if xnat_project:
        cmd += ["--xnat-project", xnat_project]

    subprocess.run(cmd, check=True)

    out = dxpy.upload_local_file(report_local, wait_on_close=True)
    return {"export_report": dxpy.dxlink(out)}

if __name__ == "__main__":
    dxpy.run()
