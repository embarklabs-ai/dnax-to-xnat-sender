#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import sys
import tarfile
import time
import zipfile
from collections import namedtuple

import dxpy
import fsspec
import pydicom
import requests
import yaml

# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def _init_dx_security():
    sc = os.environ.get("DX_SECURITY_CONTEXT")
    if not sc:
        print("DX_SECURITY_CONTEXT not found in environment", file=sys.stderr)
        return
    try:
        dxpy.set_security_context(json.loads(sc))
    except Exception as e:
        print(f"Failed to set job security context: {e}", file=sys.stderr)

def read_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# ---------------------------------------------------------------------------
# DNAnexus helpers
# ---------------------------------------------------------------------------

FileInfo = namedtuple("FileInfo", ["project_id", "folder", "name", "url", "size", "properties"])

def resolve_file_entry(entry, project_id, folder):
    """Resolve a manifest entry to a DNAnexus file ID.
    If entry is already a file ID (file-XXXX), return as-is.
    Otherwise treat as a filename and look it up in the given folder."""
    if entry.startswith("file-"):
        return entry
    result = dxpy.find_one_data_object(
        classname="file", name=entry, name_mode="exact",
        project=project_id, folder=folder, zero_ok=False, more_ok=False,
    )
    return result["id"]

def get_resource_type(filename):
    name_upper = filename.upper()
    for ext in ('.SVS', '.CSV', '.JSON', '.PDF'):
        if name_upper.endswith(ext):
            return ext[1:]
    if name_upper.endswith('.ZIP'):
        for suffix in ('-SVS.ZIP', '-CSV.ZIP', '-JSON.ZIP', '-PDF.ZIP'):
            if name_upper.endswith(suffix):
                return suffix[1:].split('.')[0]
    return None

def normalize_root_path(path_str: str):
    if ":" in path_str:
        proj, folder = path_str.split(":", 1)
        folder = "/" + folder.strip("/")
    else:
        proj = path_str
        folder = "/"

    if re.match(r"^project-[A-Za-z0-9]{24}$", proj):
        return proj, folder

    results = dxpy.find_projects(name=proj, name_mode="exact", level="VIEW")
    rec = next(results, None)
    if not rec:
        raise ValueError(f"DNAnexus project not found: {proj}")
    return rec["id"], folder

def dx_file_info(file_id, project_hint=None):
    fields = {"name": True, "folder": True, "project": True, "size": True, "properties": True}
    if project_hint:
        desc = dxpy.api.file_describe(file_id, {"project": project_hint, "fields": fields})
    else:
        desc = dxpy.describe(file_id)

    project_id = desc.get("project") or project_hint or os.environ.get("DX_PROJECT_CONTEXT_ID")
    if not project_id:
        raise RuntimeError(f"Could not resolve project for {file_id}")

    folder = desc.get("folder", "/")
    name = desc["name"]
    size = int(desc.get("size") or 0)
    props = desc.get("properties") or {}
    path = ("/" + name) if folder == "/" else (folder.rstrip("/") + "/" + name)
    url = f"dnanexus://{project_id}:{path}"
    return FileInfo(project_id, folder, name, url, size, props)

class StreamingBody:
    def __init__(self, fobj, length: int, chunk: int = 1024 * 1024):
        self.fobj = fobj
        self.length = int(length)
        self.chunk = int(chunk)

    def __len__(self):
        return self.length

    def read(self, amt=None):
        return self.fobj.read(self.chunk if amt is None else amt)

    def close(self):
        try:
            self.fobj.close()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def parse_manifest(manifest_path):
    """Parse hierarchical manifest: project -> subject -> session_label -> [file_ids]"""
    raw = read_yaml(manifest_path)
    images = raw.get("images", {})
    tasks = []
    for project, subjects_dict in images.items():
        for subject, sessions_dict in subjects_dict.items():
            for session_label, file_ids in sessions_dict.items():
                tasks.append((str(project), str(subject), str(session_label), file_ids))
    return tasks

# ---------------------------------------------------------------------------
# XNAT helpers
# ---------------------------------------------------------------------------

def xnat_session(username, password, verify_ssl: bool):
    session = requests.Session()
    session.auth = (username, password)
    session.verify = verify_ssl
    return session

def xnat_import_to_prearchive(sess, xnat_url, project, session_label, body_stream, subject=None):
    params = {
        "inbody": "true",
        "import-handler": "DICOM-zip",
        "Ignore-Unparsable": "true",
        "project": project,
        "session": session_label,
    }
    if subject:
        params["subject"] = subject
    headers = {"Content-Type": "application/zip"}
    return sess.post(xnat_url + "/data/services/import",
                     params=params, data=body_stream, headers=headers, timeout=600)

def xnat_wait_for_auto_archive(sess, xnat_url, project, session_label,
                               initial_wait=360, retries=20, delay=60):
    print(f"  Confirming session has been archived...")
    time.sleep(initial_wait)
    endpoint = xnat_url + f"/data/projects/{project}/experiments"
    for attempt in range(retries):
        resp = sess.get(endpoint, params={"label": session_label, "format": "json", "limit": 1}, timeout=60)
        if resp.status_code < 400:
            arr = resp.json().get("ResultSet", {}).get("Result", [])
            if arr:
                return arr[0].get("ID")
        print(f"  Not yet in archive (attempt {attempt + 1}/{retries}), waiting {delay}s...")
        time.sleep(delay)
    return None

# ---------------------------------------------------------------------------
# XNAT session resource helpers
# ---------------------------------------------------------------------------

def xnat_find_session(sess, xnat_url, project, session_label):
    endpoint = xnat_url + f"/data/projects/{project}/experiments"
    resp = sess.get(endpoint, params={"label": session_label, "format": "json", "limit": 1}, timeout=60)
    if resp.status_code < 400:
        arr = resp.json().get("ResultSet", {}).get("Result", [])
        if arr:
            return arr[0].get("ID")
    return None

def xnat_create_session(sess, xnat_url, project, subject, session_label, xsi_type):
    create_url = xnat_url + f"/data/projects/{project}/subjects/{subject}/experiments/{session_label}"
    resp = sess.put(create_url, params={"xsiType": xsi_type}, timeout=60)
    if resp.status_code < 300:
        return xnat_find_session(sess, xnat_url, project, session_label)
    return None

def xnat_upload_resource_file(sess, xnat_url, experiment_id, resource_label, filename, body_stream, content_type="application/octet-stream"):
    url = xnat_url + f"/data/experiments/{experiment_id}/resources/{resource_label}/files/{filename}"
    resp = sess.put(url, params={"inbody": "true"}, data=body_stream,
                    headers={"Content-Type": content_type}, timeout=600)
    return resp

def xnat_verify_resource_file(sess, xnat_url, experiment_id, resource_label, filename):
    url = xnat_url + f"/data/experiments/{experiment_id}/resources/{resource_label}/files"
    resp = sess.get(url, params={"format": "json"}, timeout=60)
    if resp.status_code >= 400:
        return False
    files = resp.json().get("ResultSet", {}).get("Result", [])
    return any(f.get("Name") == filename for f in files)

# ---------------------------------------------------------------------------
# DICOM metadata extraction (for validation)
# ---------------------------------------------------------------------------

def _has_dicm_preamble(data):
    return len(data) >= 132 and data[128:132] == b"DICM"

def _first_dicom_in_zip(zf: zipfile.ZipFile):
    candidates = [n for n in zf.namelist() if not n.endswith("/")]
    # quick check for .dcm extension
    for n in candidates:
        if n.lower().endswith(".dcm"):
            return n
    # fallback check for DICM preamble in first 132 bytes
    for n in candidates:
        try:
            with zf.open(n) as f:
                if _has_dicm_preamble(f.read(132)):
                    return n
        except Exception:
            continue
    return candidates[0] if candidates else None

def _first_dicom_in_tar(tf: tarfile.TarFile): #fallback just in case .tar is present instead of .zip
    members = [m for m in tf if m.isreg()]
    # quick check for .dcm extension
    for m in members:
        if m.name.lower().endswith(".dcm"):
            return m
    # fallback check for DICM preamble in first 132 bytes
    for m in members:
        try:
            f = tf.extractfile(m)
            if f and _has_dicm_preamble(f.read(132)):
                return m
        except Exception:
            continue
    return members[0] if members else None

def extract_dicom_metadata(url: str):
    """
    Extract DICOM metadata from the first file in an archive.
    Returns dict with PatientName, PatientID, Modality, StudyDate, or empty dict.
    """
    try:
        with fsspec.open(url, "rb") as fobj:
            head = fobj.read(4)
            fobj.seek(0)
            if head == b"PK\x03\x04":
                with zipfile.ZipFile(fobj) as zf:
                    name = _first_dicom_in_zip(zf)
                    if not name:
                        return {}
                    with zf.open(name) as dcm_fp:
                        ds = pydicom.dcmread(dcm_fp, stop_before_pixels=True, force=True)
            else:
                try:
                    with tarfile.open(fileobj=fobj, mode="r:*") as tf:
                        member = _first_dicom_in_tar(tf)
                        if not member:
                            return {}
                        dcm_fp = tf.extractfile(member)
                        if not dcm_fp:
                            return {}
                        ds = pydicom.dcmread(dcm_fp, stop_before_pixels=True, force=True)
                except tarfile.ReadError:
                    return {}
        return {
            "PatientName": str(getattr(ds, "PatientName", "") or ""),
            "PatientID": str(getattr(ds, "PatientID", "") or ""),
            "Modality": str(getattr(ds, "Modality", "") or ""),
            "StudyDate": str(getattr(ds, "StudyDate", "") or ""),
        }
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# DICOM validation (Phase 0)
# ---------------------------------------------------------------------------

def _validate_dicom_metadata(file_ids, root_project_id, subject, session_label):
    """Validate manifest subject/session against DICOM metadata from first file.
    Returns (possibly corrected subject, session_label, list of override messages)."""
    overrides = []
    if not file_ids:
        return subject, session_label, overrides

    try:
        info = dx_file_info(file_ids[0], project_hint=root_project_id)
        if info.size <= 0:
            return subject, session_label, overrides

        dcm_meta = extract_dicom_metadata(info.url)
        if not dcm_meta:
            print(f"  WARNING: Could not extract DICOM metadata from {file_ids[0]}")
            return subject, session_label, overrides

        # Validate subject vs DICOM PatientName
        dicom_patient_name = dcm_meta.get("PatientName", "")
        if dicom_patient_name and dicom_patient_name != subject:
            overrides.append(f"Subject overridden: manifest='{subject}' -> DICOM PatientName='{dicom_patient_name}'")
            print(f"  Subject override: '{subject}' -> '{dicom_patient_name}'")
            subject = dicom_patient_name

        # Validate session label vs DICOM PatientID-Modality-StudyDate
        dicom_pid = dcm_meta.get("PatientID", "")
        dicom_mod = dcm_meta.get("Modality", "")
        dicom_date = dcm_meta.get("StudyDate", "")
        if dicom_pid and dicom_mod and dicom_date:
            dicom_session_label = f"{dicom_pid}-{dicom_mod}-{dicom_date}"
            if dicom_session_label != str(session_label):
                overrides.append(f"Session label overridden: manifest='{session_label}' -> DICOM='{dicom_session_label}'")
                print(f"  Session label override: '{session_label}' -> '{dicom_session_label}'")
                session_label = dicom_session_label

    except Exception as e:
        print(f"  WARNING: DICOM validation failed: {e}")

    return subject, session_label, overrides

# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(manifest_path, secrets_path, src_root_path, report_path, xnat_project_override=None):
    manifest_tasks = parse_manifest(manifest_path)

    if not manifest_tasks:
        print("manifest.yaml must contain 'images' with at least one project/subject/session", file=sys.stderr)
        sys.exit(1)

    secrets = read_yaml(secrets_path) or {}
    xnat_url   = secrets.get("xnat_url", "").rstrip("/")
    username   = secrets.get("username")
    password   = secrets.get("password")
    verify_ssl = bool(secrets.get("verify_ssl", True))
    if not (xnat_url and username and password):
        print("secrets.yaml must include xnat_url, username, password", file=sys.stderr)
        sys.exit(1)

    root_project_id, root_prefix = normalize_root_path(src_root_path)
    _ = fsspec.filesystem("dnanexus")

    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    cols = ["project", "subject", "session_label", "file_name", "file_id", "sent_to_xnat", "xnat_experiment_id", "message"]
    with open(report_path, "w", newline="", encoding="utf-8") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=cols)
        writer.writeheader()
        fcsv.flush()

        sess = xnat_session(username, password, verify_ssl)

        for manifest_project, subject, session_label, file_entries in manifest_tasks:
            xnat_proj = xnat_project_override or manifest_project
            file_results = []

            # Resolve filenames to file IDs (file IDs pass through unchanged)
            session_folder = root_prefix.rstrip("/") + f"/{manifest_project}/{subject}/{session_label}"
            resolved_files = []  # list of (original_entry, file_id)
            for entry in file_entries:
                try:
                    fid = resolve_file_entry(str(entry), root_project_id, session_folder)
                    resolved_files.append((str(entry), fid))
                except Exception as e:
                    print(f"  WARNING: Could not resolve '{entry}' in {session_folder}: {e}")

            if not resolved_files:
                print(f"  No files resolved for session {session_label}, skipping")
                continue

            entry_by_id = {fid: entry for entry, fid in resolved_files}

            # Split into DICOM files and resource files
            dicom_files = []
            resource_files = []  # list of (file_id, resource_type)
            for entry, fid in resolved_files:
                rtype = get_resource_type(entry)
                if rtype:
                    resource_files.append((fid, rtype))
                else:
                    dicom_files.append(fid)

            xnat_eid = ""
            overrides = []

            # ============================================================
            # DICOM flow (Phase 0 → 1 → 2)
            # ============================================================
            if dicom_files:
                # ------ Phase 0: Validate manifest against DICOM metadata ------
                print(f"\n[Phase 0] Validating DICOM metadata for session {session_label}")
                subject, session_label, overrides = _validate_dicom_metadata(
                    dicom_files, root_project_id, subject, session_label
                )

                # ------ Phase 1: Import DICOM zips to prearchive ------
                print(f"\n[Phase 1] Importing session {session_label} ({len(dicom_files)} DICOM files) -> prearchive")
                for file_id in dicom_files:
                    import_ok = False
                    msg = ""
                    try:
                        info = dx_file_info(file_id, project_hint=root_project_id)

                        if info.project_id != root_project_id or not info.folder.startswith(root_prefix):
                            raise ValueError(f"{file_id} at {info.project_id}:{info.folder}/ is outside src_root_path {root_project_id}:{root_prefix}")

                        try:
                            dxpy.api.file_set_properties(file_id, {"properties": {"sent_to_xnat": "False"}})
                        except Exception:
                            pass

                        if info.size <= 0:
                            raise ValueError(f"{file_id} has unknown/zero size; cannot stream")

                        with fsspec.open(info.url, mode="rb") as fobj:
                            stream = StreamingBody(fobj, info.size)
                            resp = xnat_import_to_prearchive(
                                sess, xnat_url, xnat_proj, session_label, stream, subject=subject
                            )

                        if resp.status_code < 300:
                            import_ok = True
                            msg = "Imported to prearchive"
                            print(f"  [{file_id}] imported OK")
                        else:
                            msg = f"Import HTTP {resp.status_code}: {resp.text[:300]}"

                    except Exception as e:
                        msg = f"ERROR: {type(e).__name__}: {e}"

                    file_results.append((file_id, import_ok, msg))

                # Check if any DICOM files were imported successfully
                any_imported = any(ok for _, ok, _ in file_results)
                if not any_imported:
                    print(f"  No DICOM files imported for session {session_label}")
                else:
                    # ------ Phase 2: Wait for XNAT auto-archive ------
                    print(f"\n[Phase 2] Waiting for XNAT to auto-archive session {session_label}")
                    try:
                        xnat_eid = xnat_wait_for_auto_archive(sess, xnat_url, xnat_proj, session_label)
                        if xnat_eid:
                            print(f"  Auto-archived and verified: {xnat_eid}")
                            # Mark DICOM results as OK
                            file_results = [
                                (fid, ok, "OK" if ok else m) for fid, ok, m in file_results
                            ]
                        else:
                            print(f"  WARNING: Session not found in archive after waiting")
                    except Exception as e:
                        print(f"  Archive ERROR: {type(e).__name__}: {e}")

            # ============================================================
            # Resource flow (Phase 3 → 4)
            # ============================================================
            if resource_files:
                # ------ Phase 3: Find session (or create for SVS only) ------
                if not xnat_eid:
                    print(f"\n[Phase 3] Looking up session {session_label} on XNAT")
                    xnat_eid = xnat_find_session(sess, xnat_url, xnat_proj, session_label)
                    if xnat_eid:
                        print(f"  Session found: {xnat_eid}")

                # Separate SVS files from other resource types
                svs_files = [(fid, rt) for fid, rt in resource_files if rt == "SVS"]
                other_resource_files = [(fid, rt) for fid, rt in resource_files if rt != "SVS"]

                # If no session exists, only SVS files can proceed (create SM session)
                if not xnat_eid and svs_files:
                    print(f"  No existing session — creating SM session for SVS upload")
                    xnat_eid = xnat_create_session(
                        sess, xnat_url, xnat_proj, subject, session_label, "xnat:smSessionData"
                    )
                    if xnat_eid:
                        print(f"  SM session created: {xnat_eid}")
                    else:
                        print(f"  ERROR: Could not create SM session {session_label}")

                # Fail non-SVS resources if no session exists
                if not xnat_eid and other_resource_files:
                    print(f"  No imaging session exists for {session_label}, unable to determine session modality")
                    for file_id, resource_type in other_resource_files:
                        file_results.append((file_id, False,
                            "No imaging session exists, unable to determine session modality"))

                # Upload files that have a valid session
                uploadable = []
                if xnat_eid:
                    uploadable = svs_files + other_resource_files
                elif svs_files:
                    # SM session creation failed — mark SVS files as failed too
                    for file_id, resource_type in svs_files:
                        file_results.append((file_id, False, "Failed to create SM session"))

                if uploadable:
                    # ------ Phase 4: Upload resource files ------
                    print(f"\n[Phase 4] Uploading {len(uploadable)} resource files to session {session_label}")
                    for file_id, resource_type in uploadable:
                        upload_ok = False
                        msg = ""
                        try:
                            info = dx_file_info(file_id, project_hint=root_project_id)

                            try:
                                dxpy.api.file_set_properties(file_id, {"properties": {"sent_to_xnat": "False"}})
                            except Exception:
                                pass

                            if info.size <= 0:
                                raise ValueError(f"{file_id} has unknown/zero size; cannot stream")

                            with fsspec.open(info.url, mode="rb") as fobj:
                                stream = StreamingBody(fobj, info.size)
                                resp = xnat_upload_resource_file(
                                    sess, xnat_url, xnat_eid, resource_type, info.name, stream
                                )

                            if resp.status_code < 300:
                                # Verify the file is in the resource folder
                                if xnat_verify_resource_file(sess, xnat_url, xnat_eid, resource_type, info.name):
                                    upload_ok = True
                                    msg = f"Uploaded to {resource_type} resource, verified"
                                    print(f"  [{file_id}] uploaded to {resource_type}, verified")
                                else:
                                    msg = f"Uploaded to {resource_type} but verification failed"
                                    print(f"  [{file_id}] uploaded to {resource_type}, NOT verified")
                            else:
                                msg = f"Upload HTTP {resp.status_code}: {resp.text[:300]}"

                        except Exception as e:
                            msg = f"ERROR: {type(e).__name__}: {e}"

                        file_results.append((file_id, upload_ok, msg))

            # ============================================================
            # Write report rows and update file properties
            # ============================================================
            override_suffix = f"; {'; '.join(overrides)}" if overrides else ""
            for file_id, sent_ok, msg in file_results:
                if sent_ok:
                    try:
                        dxpy.api.file_set_properties(file_id, {"properties": {"sent_to_xnat": "True"}})
                    except Exception:
                        pass

                writer.writerow({
                    "project": xnat_proj,
                    "subject": subject,
                    "session_label": session_label,
                    "file_name": entry_by_id.get(file_id, ""),
                    "file_id": file_id,
                    "sent_to_xnat": str(sent_ok),
                    "xnat_experiment_id": xnat_eid if sent_ok else "",
                    "message": msg + override_suffix,
                })
            fcsv.flush()

def main():
    _init_dx_security()
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--src-root-path", required=True)
    ap.add_argument("--xnat-project", default=None, help="Override XNAT project from manifest")
    ap.add_argument("--report", required=True)
    args = ap.parse_args()
    run(args.manifest, args.secrets, args.src_root_path, args.report, args.xnat_project)

if __name__ == "__main__":
    main()
