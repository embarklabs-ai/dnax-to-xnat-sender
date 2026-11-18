#!/usr/bin/env python3
import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import tarfile
import time
import urllib.parse
import zipfile
from collections import namedtuple
from datetime import datetime, timezone

import dxpy
import fsspec
import pydicom
import requests
import yaml

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# XNAT project IDs and subject/session labels: letters, digits, "_" and "-" only.
XNAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Characters that break or confuse XNAT file URLs, even when encoded.
BAD_FILENAME_CHARS = set('\\/?#%+;:*"<>|')

# Session resource folder per non-DICOM type (default: the type itself). Whole-slide images go to WSI.
RESOURCE_LABELS = {"SVS": "WSI"}

# Retries apply to session resource uploads only. A DICOM-zip import is not idempotent
# (a retry can create a duplicate prearchive entry), so it is sent exactly once.
UPLOAD_ATTEMPTS = 3        # 1 + 2 retries
RETRY_WAIT = 30            # seconds before the 1st retry, growing each time
SETTLE_MINUTES = 10        # after an error once the whole file was sent
SETTLE_POLL = 20           # seconds between checks while settling
CHUNK = 8 * 1024 * 1024

REPORT_COLUMNS = ["project", "subject", "session_label", "file_name", "file_id", "sent_to_xnat",
                  "xnat_experiment_id", "message", "xnat_resource", "xnat_file_name", "md5"]

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

class XnatError(Exception):
    pass

class FatalStop(Exception):
    """XNAT refused a write (HTTP 403): the account cannot write to the project, so stop sending."""

def new_md5():
    try:
        return hashlib.md5(usedforsecurity=False)
    except TypeError:
        return hashlib.md5()

def q(segment):
    """URL-encode one path segment (project, label, file name)."""
    return urllib.parse.quote(str(segment), safe="")

def xnat_message(text, limit=300):
    """Readable message out of an XNAT/Tomcat error page."""
    if not text:
        return ""
    m = re.search(r"<b>Message</b>\s*(.*?)</p>", text, re.S)
    if m:
        text = m.group(1)
    text = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(text)).split())
    return text if len(text) <= limit else text[:limit] + "..."

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

def dx_md5(url):
    """MD5 of a DNAnexus file, streamed (nothing is written to disk)."""
    h = new_md5()
    with fsspec.open(url, "rb") as fobj:
        for block in iter(lambda: fobj.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()

class StreamingBody:
    """Streams a DNAnexus file to XNAT, counting the bytes read and computing their MD5 on the way."""

    def __init__(self, fobj, length: int, chunk: int = 1024 * 1024):
        self.fobj = fobj
        self.length = int(length)
        self.chunk = int(chunk)
        self.sent = 0
        self._md5 = new_md5()

    def __len__(self):
        return self.length

    def read(self, amt=None):
        data = self.fobj.read(self.chunk if amt is None else amt)
        if data:
            self._md5.update(data)
            self.sent += len(data)
        return data

    def md5(self):
        return self._md5.hexdigest()

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

def xnat_check_project(sess, xnat_url, project):
    """Problem with the XNAT project ID, or None if it exists exactly as given (IDs are case-sensitive)."""
    if not XNAT_ID_RE.match(project):
        return f"'{project}' is not a valid XNAT project ID (letters, digits, _ and - only)"
    try:
        resp = sess.get(xnat_url + f"/data/projects/{q(project)}", params={"format": "json"}, timeout=60)
    except requests.RequestException as e:
        return f"could not reach XNAT to check project '{project}': {type(e).__name__}: {e}"
    if resp.status_code != 200:
        return (f"XNAT project '{project}' not found or not accessible (HTTP {resp.status_code}); "
                "project IDs are case-sensitive")
    try:
        found = resp.json()["items"][0]["data_fields"]["ID"]
    except (ValueError, KeyError, IndexError, TypeError):
        found = None
    if found is not None and found != project:
        return f"XNAT project ID is '{found}', not '{project}'; project IDs are case-sensitive"
    return None

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
    for attempt in range(retries):
        eid, subject = xnat_find_session(sess, xnat_url, project, session_label)
        if eid:
            return eid, subject
        print(f"  Not yet in archive (attempt {attempt + 1}/{retries}), waiting {delay}s...")
        time.sleep(delay)
    return None, None

# ---------------------------------------------------------------------------
# XNAT session resource helpers
# ---------------------------------------------------------------------------

def xnat_find_session(sess, xnat_url, project, session_label):
    """-> (experiment ID, subject label) of the session with this label, or (None, None).
    XNAT session labels are unique within a project and compared case-insensitively."""
    endpoint = xnat_url + f"/data/projects/{q(project)}/experiments"
    resp = sess.get(endpoint, params={"label": session_label, "columns": "ID,label,subject_label",
                                      "format": "json"}, timeout=60)
    if resp.status_code < 400:
        for row in resp.json().get("ResultSet", {}).get("Result", []):
            if (row.get("label") or "").lower() == str(session_label).lower():
                return row.get("ID"), row.get("subject_label") or ""
    return None, None

def xnat_create_session(sess, xnat_url, project, subject, session_label, xsi_type):
    create_url = xnat_url + f"/data/projects/{q(project)}/subjects/{q(subject)}/experiments/{q(session_label)}"
    resp = sess.put(create_url, params={"xsiType": xsi_type}, timeout=60)
    if resp.status_code == 403:
        raise FatalStop(f"XNAT refused to create session {session_label} in project '{project}' (HTTP 403)")
    if resp.status_code < 300:
        return xnat_find_session(sess, xnat_url, project, session_label)[0]
    return None

def xnat_upload_resource_file(sess, xnat_url, experiment_id, resource_label, filename, body_stream,
                              overwrite=False, content_type="application/octet-stream"):
    url = xnat_url + f"/data/experiments/{q(experiment_id)}/resources/{q(resource_label)}/files/{q(filename)}"
    params = {"inbody": "true"}
    if overwrite:
        params["overwrite"] = "true"
    resp = sess.put(url, params=params, data=body_stream,
                    headers={"Content-Type": content_type}, timeout=600)
    return resp

def xnat_list_resource_files(sess, xnat_url, experiment_id, resource_label):
    """{file name: {"size", "digest"}} in a session resource; {} if the resource does not exist yet."""
    url = xnat_url + f"/data/experiments/{q(experiment_id)}/resources/{q(resource_label)}/files"
    try:
        resp = sess.get(url, params={"format": "json"}, timeout=60)
    except requests.RequestException as e:
        raise XnatError(f"listing {resource_label} files: {type(e).__name__}: {e}")
    if resp.status_code == 404:
        return {}
    if resp.status_code >= 400:
        raise XnatError(f"listing {resource_label} files: HTTP {resp.status_code}")
    try:
        rows = resp.json()["ResultSet"]["Result"] or []
    except (ValueError, KeyError, TypeError):
        raise XnatError(f"listing {resource_label} files: unexpected reply from XNAT")
    files = {}
    for row in rows:
        uri = row.get("URI") or ""
        name = urllib.parse.unquote(uri.split("/files/", 1)[1]) if "/files/" in uri else row.get("Name", "")
        try:
            size = int(row.get("Size") or -1)
        except ValueError:
            size = -1
        files[name] = {"size": size, "digest": (row.get("digest") or "").strip().lower()}
    return files

# ---------------------------------------------------------------------------
# Session resource upload: verified by size + MD5, additive, retried safely
# ---------------------------------------------------------------------------

def versioned_name(name, stamp):
    """report.pdf -> report_20261118T153045Z.pdf"""
    stem, ext = os.path.splitext(name)
    return f"{stem}_{stamp}{ext}"

def versions_on_xnat(name, remote):
    """Files on XNAT that are `name` itself or an earlier timestamped version of it."""
    stem, ext = os.path.splitext(name)
    pattern = re.compile(re.escape(stem) + r"(_\d{8}T\d{6}Z(_\d+)?)?" + re.escape(ext))
    return {n: f for n, f in remote.items() if pattern.fullmatch(n)}

def check_on_xnat(sess, xnat_url, experiment_id, resource_label, info, name, md5):
    """Compare the copy of `name` on XNAT with the source. -> (ok, how verified or why not, present).
    Size + MD5 when XNAT stores a checksum, size only otherwise (XNAT's checksums setting is off)."""
    remote = xnat_list_resource_files(sess, xnat_url, experiment_id, resource_label).get(name)
    if remote is None:
        return False, "file is not on XNAT", False
    if remote["size"] != info.size:
        return False, f"size on XNAT is {remote['size']} bytes, source is {info.size} bytes", True
    if not remote["digest"]:
        return True, "size", True
    md5 = md5 or dx_md5(info.url)
    if remote["digest"] != md5:
        return False, f"checksum on XNAT ({remote['digest']}) differs from source ({md5})", True
    return True, "size+md5", True

def settle_and_verify(sess, xnat_url, experiment_id, resource_label, info, name, md5, full_body_sent):
    """After an error XNAT may still have saved the file (e.g. a gateway timeout while XNAT was
    finishing). If the whole file went out, keep checking for a while, about 1 minute per GB and
    at least SETTLE_MINUTES, so a retry never races a save still in progress."""
    settle = max(SETTLE_MINUTES * 60, info.size / 1e9 * 60) if full_body_sent else 0
    deadline = time.monotonic() + settle
    while True:
        try:
            ok, how, present = check_on_xnat(sess, xnat_url, experiment_id, resource_label, info, name, md5)
            if ok:
                return True, how
            if present:
                return False, how       # a wrong copy is there: the retry replaces it
        except XnatError as e:
            how = f"could not check XNAT: {e}"
        if time.monotonic() >= deadline:
            return False, how
        print(f"    {name}: checking whether XNAT saved the file anyway...")
        time.sleep(min(SETTLE_POLL, max(0.1, deadline - time.monotonic())))

def upload_verified(sess, xnat_url, experiment_id, resource_label, info, name):
    """Stream a DNAnexus file into a session resource as `name`, verify it, retry safely.
    `name` was not on XNAT before this upload, so overwriting it on a retry only ever
    replaces this upload's own incomplete copy. -> (ok, how verified, md5, message)"""
    last, overwrite, md5 = "", False, ""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(RETRY_WAIT * (attempt - 1))
        stream = None
        try:
            with fsspec.open(info.url, mode="rb") as fobj:
                stream = StreamingBody(fobj, info.size)
                resp = xnat_upload_resource_file(sess, xnat_url, experiment_id, resource_label, name,
                                                 stream, overwrite=overwrite)
        except requests.RequestException as e:
            last = f"network error: {type(e).__name__}: {e}"
            full = stream is not None and stream.sent == info.size
            if full:
                md5 = stream.md5()
            ok, how = settle_and_verify(sess, xnat_url, experiment_id, resource_label, info, name, md5, full)
            if ok:
                return True, how, md5, "verified on XNAT after a network error"
            overwrite = True
            continue
        except Exception as e:
            # e.g. reading from DNAnexus failed part-way
            last = f"{type(e).__name__}: {e}"
            overwrite = True
            continue
        if stream.sent == info.size:
            md5 = stream.md5()
        status = resp.status_code
        if status == 403:
            raise FatalStop(f"XNAT refused the upload (HTTP 403): the account cannot write to this project")
        if status in (200, 201):
            try:
                ok, how, _ = check_on_xnat(sess, xnat_url, experiment_id, resource_label, info, name, md5)
            except XnatError as e:
                ok, how = False, f"could not check XNAT: {e}"
            if ok:
                return True, how, md5, ""
            last, overwrite = f"XNAT answered HTTP {status} but {how}", True
            continue
        if status in (408, 429) or status >= 500:
            last = f"HTTP {status}: {xnat_message(resp.text) or 'server/gateway error'}"
            ok, how = settle_and_verify(sess, xnat_url, experiment_id, resource_label, info, name, md5,
                                        stream.sent == info.size)
            if ok:
                return True, how, md5, f"verified on XNAT after HTTP {status}"
            overwrite = True
            continue
        return False, "", md5, f"HTTP {status}: {xnat_message(resp.text)}"
    return False, "", md5, f"gave up after {UPLOAD_ATTEMPTS} attempts; last error: {last}"

def send_resource_file(sess, xnat_url, experiment_id, resource_label, info):
    """Add one file to a session resource without ever replacing what is already there.
      - not on XNAT yet: upload it under its own name
      - identical copy already there (same size + MD5): not re-sent
      - same name and size, but XNAT stores no checksum to compare: not re-sent, flagged
      - a different file with the same name is there: upload this one as a new,
        timestamped version next to it
    -> (sent_ok, stored name, md5, message)"""
    bad = sorted(set(info.name) & BAD_FILENAME_CHARS)
    if bad or any(ord(ch) < 32 or ord(ch) > 126 for ch in info.name):
        return False, "", "", (f"file name has characters XNAT file URLs cannot handle "
                               f"({' '.join(bad) or 'non-ASCII'}); rename it on DNAnexus")
    remote = xnat_list_resource_files(sess, xnat_url, experiment_id, resource_label)
    versions = versions_on_xnat(info.name, remote)
    md5 = ""
    same_size = {n: f for n, f in versions.items() if f["size"] == info.size}
    if any(f["digest"] for f in same_size.values()):
        md5 = dx_md5(info.url)
        match = next((n for n, f in sorted(same_size.items()) if f["digest"] == md5), None)
        if match:
            return True, match, md5, (f"Already on XNAT: identical file in {resource_label} resource "
                                      f"as {match}; not re-sent")
    unconfirmed = sorted(n for n, f in same_size.items() if not f["digest"])
    if unconfirmed:
        return False, "", md5, (f"Not re-sent: {unconfirmed[0]} with the same size is already in the "
                                f"{resource_label} resource and XNAT stores no checksum to confirm it is "
                                f"identical; if this is a corrected file, rename it on DNAnexus and send again")
    target, note = info.name, ""
    if info.name in remote:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target, n = versioned_name(info.name, stamp), 2
        while target in remote:
            target, n = versioned_name(info.name, f"{stamp}_{n}"), n + 1
        note = f"; a different {info.name} was already on XNAT"
        print(f"  {info.name}: a different file with this name is on XNAT; sending as new version {target}")
    ok, how, md5_sent, detail = upload_verified(sess, xnat_url, experiment_id, resource_label, info, target)
    md5 = md5_sent or md5
    if not ok:
        return False, "", md5, f"Upload to {resource_label} resource failed: {detail}"
    where = f"{resource_label} resource" + (f" as new version {target}" if target != info.name else "")
    extra = f"; {detail}" if detail else ""
    return True, target, md5, f"Uploaded to {where}, verified ({how}){extra}{note}"

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

def file_result(file_id, ok, message, resource="", stored="", md5=""):
    return {"file_id": file_id, "ok": ok, "message": message,
            "resource": resource, "stored": stored, "md5": md5}

def write_report_rows(writer, fcsv, xnat_proj, subject, session_label, entry_by_id, file_results,
                      xnat_eid, overrides):
    override_suffix = f"; {'; '.join(overrides)}" if overrides else ""
    for r in file_results:
        if r["ok"]:
            try:
                dxpy.api.file_set_properties(r["file_id"], {"properties": {"sent_to_xnat": "True"}})
            except Exception:
                pass

        writer.writerow({
            "project": xnat_proj,
            "subject": subject,
            "session_label": session_label,
            "file_name": entry_by_id.get(r["file_id"], ""),
            "file_id": r["file_id"],
            "sent_to_xnat": str(r["ok"]),
            "xnat_experiment_id": xnat_eid if r["ok"] else "",
            "message": r["message"] + override_suffix,
            "xnat_resource": r["resource"],
            "xnat_file_name": r["stored"],
            "md5": r["md5"],
        })
    fcsv.flush()

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
    with open(report_path, "w", newline="", encoding="utf-8") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        fcsv.flush()

        sess = xnat_session(username, password, verify_ssl)

        # Check every target XNAT project once, before anything is sent
        targets = {xnat_project_override} if xnat_project_override else {p for p, _, _, _ in manifest_tasks}
        project_problems = {}
        for p in sorted(targets):
            problem = xnat_check_project(sess, xnat_url, p)
            if problem:
                print(f"ERROR: {problem}")
                project_problems[p] = problem

        stop_reason = None   # set when XNAT refuses a write (HTTP 403); nothing more is sent

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

            skip_reason = project_problems.get(xnat_proj) or (f"run stopped: {stop_reason}" if stop_reason else None)
            if skip_reason:
                print(f"\nNot sending session {session_label}: {skip_reason}")
                file_results = [file_result(fid, False, f"Not sent: {skip_reason}") for _, fid in resolved_files]
                write_report_rows(writer, fcsv, xnat_proj, subject, session_label, entry_by_id,
                                  file_results, "", [])
                continue

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
            xnat_subject = None
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
                    if stop_reason:
                        file_results.append(file_result(file_id, False, f"Not sent: run stopped: {stop_reason}"))
                        continue
                    import_ok = False
                    msg = ""
                    md5 = ""
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

                        # Sent exactly once: the DICOM-zip import is not idempotent
                        with fsspec.open(info.url, mode="rb") as fobj:
                            stream = StreamingBody(fobj, info.size)
                            resp = xnat_import_to_prearchive(
                                sess, xnat_url, xnat_proj, session_label, stream, subject=subject
                            )
                        if stream.sent == info.size:
                            md5 = stream.md5()

                        if resp.status_code < 300:
                            import_ok = True
                            msg = "Imported to prearchive"
                            print(f"  [{file_id}] imported OK")
                        elif resp.status_code == 403:
                            stop_reason = f"XNAT refused the DICOM import into project '{xnat_proj}' (HTTP 403)"
                            msg = f"Import HTTP 403: {resp.text[:300]}"
                            print(f"  ERROR: {stop_reason}; stopping")
                        else:
                            msg = f"Import HTTP {resp.status_code}: {resp.text[:300]}"

                    except Exception as e:
                        msg = f"ERROR: {type(e).__name__}: {e}"

                    file_results.append(file_result(file_id, import_ok, msg, md5=md5))

                # Check if any DICOM files were imported successfully
                any_imported = any(r["ok"] for r in file_results)
                if not any_imported:
                    print(f"  No DICOM files imported for session {session_label}")
                else:
                    # ------ Phase 2: Wait for XNAT auto-archive ------
                    print(f"\n[Phase 2] Waiting for XNAT to auto-archive session {session_label}")
                    try:
                        xnat_eid, xnat_subject = xnat_wait_for_auto_archive(sess, xnat_url, xnat_proj, session_label)
                        xnat_eid = xnat_eid or ""
                        if xnat_eid:
                            print(f"  Auto-archived and verified: {xnat_eid}")
                            # Mark DICOM results as OK
                            file_results = [dict(r, message="OK") if r["ok"] else r for r in file_results]
                        else:
                            print(f"  WARNING: Session not found in archive after waiting")
                    except Exception as e:
                        print(f"  Archive ERROR: {type(e).__name__}: {e}")

            # ============================================================
            # Resource flow (Phase 3 → 4)
            # ============================================================
            if resource_files and stop_reason:
                for file_id, _ in resource_files:
                    file_results.append(file_result(file_id, False, f"Not sent: run stopped: {stop_reason}"))
                resource_files = []

            if resource_files:
                # ------ Phase 3: Find session (or create for SVS only) ------
                if not xnat_eid:
                    print(f"\n[Phase 3] Looking up session {session_label} on XNAT")
                    try:
                        xnat_eid, xnat_subject = xnat_find_session(sess, xnat_url, xnat_proj, session_label)
                        xnat_eid = xnat_eid or ""
                    except (requests.RequestException, ValueError) as e:
                        reason = f"could not look up session {session_label} on XNAT: {type(e).__name__}: {e}"
                        print(f"  ERROR: {reason}")
                        for file_id, _ in resource_files:
                            file_results.append(file_result(file_id, False, f"Not sent: {reason}"))
                        resource_files = []
                    if xnat_eid:
                        print(f"  Session found: {xnat_eid}")

                # Guard: never attach files to a session that XNAT holds under a different subject
                if resource_files and xnat_eid and (xnat_subject or "").lower() != str(subject).lower():
                    if xnat_subject:
                        reason = f"session {session_label} belongs to subject {xnat_subject} in XNAT; manifest says {subject}"
                    else:
                        reason = f"XNAT did not report which subject session {session_label} belongs to"
                    print(f"  NOT attaching files: {reason}")
                    for file_id, _ in resource_files:
                        file_results.append(file_result(file_id, False, f"Not sent: {reason}"))
                    resource_files = []

                # Separate SVS files from other resource types
                svs_files = [(fid, rt) for fid, rt in resource_files if rt == "SVS"]
                other_resource_files = [(fid, rt) for fid, rt in resource_files if rt != "SVS"]

                # If no session exists, only SVS files can proceed (create SM session)
                create_error = ""
                if not xnat_eid and svs_files:
                    bad_label = next((l for l in (subject, session_label) if not XNAT_ID_RE.match(str(l))), None)
                    if bad_label:
                        create_error = f"label '{bad_label}' has characters XNAT does not allow (letters, digits, _ and - only)"
                        print(f"  ERROR: cannot create SM session {session_label}: {create_error}")
                    else:
                        print(f"  No existing session — creating SM session for SVS upload")
                        try:
                            xnat_eid = xnat_create_session(
                                sess, xnat_url, xnat_proj, subject, session_label, "xnat:smSessionData"
                            ) or ""
                        except FatalStop as e:
                            stop_reason = create_error = str(e)
                        except (requests.RequestException, ValueError) as e:
                            create_error = f"{type(e).__name__}: {e}"
                        if xnat_eid:
                            xnat_subject = subject
                            print(f"  SM session created: {xnat_eid}")
                        else:
                            print(f"  ERROR: Could not create SM session {session_label}")

                # Fail non-SVS resources if no session exists
                if not xnat_eid and other_resource_files:
                    print(f"  No imaging session exists for {session_label}, unable to determine session modality")
                    for file_id, resource_type in other_resource_files:
                        file_results.append(file_result(file_id, False,
                            "No imaging session exists, unable to determine session modality"))

                # Upload files that have a valid session
                uploadable = []
                if xnat_eid:
                    uploadable = svs_files + other_resource_files
                elif svs_files:
                    # SM session creation failed — mark SVS files as failed too
                    msg = "Failed to create SM session" + (f": {create_error}" if create_error else "")
                    for file_id, resource_type in svs_files:
                        file_results.append(file_result(file_id, False, msg))

                if uploadable:
                    # ------ Phase 4: Upload resource files ------
                    print(f"\n[Phase 4] Uploading {len(uploadable)} resource files to session {session_label}")
                    for file_id, resource_type in uploadable:
                        resource_label = RESOURCE_LABELS.get(resource_type, resource_type)
                        if stop_reason:
                            file_results.append(file_result(file_id, False, f"Not sent: run stopped: {stop_reason}",
                                                            resource=resource_label))
                            continue
                        upload_ok, stored, md5, msg = False, "", "", ""
                        try:
                            info = dx_file_info(file_id, project_hint=root_project_id)

                            try:
                                dxpy.api.file_set_properties(file_id, {"properties": {"sent_to_xnat": "False"}})
                            except Exception:
                                pass

                            if info.size <= 0:
                                raise ValueError(f"{file_id} has unknown/zero size; cannot stream")

                            upload_ok, stored, md5, msg = send_resource_file(
                                sess, xnat_url, xnat_eid, resource_label, info
                            )
                            print(f"  [{file_id}] {msg}")

                        except FatalStop as e:
                            stop_reason = str(e)
                            msg = f"ERROR: {e}"
                            print(f"  ERROR: {stop_reason}; stopping")
                        except Exception as e:
                            msg = f"ERROR: {type(e).__name__}: {e}"

                        file_results.append(file_result(file_id, upload_ok, msg, resource=resource_label,
                                                        stored=stored, md5=md5))

            # ============================================================
            # Write report rows and update file properties
            # ============================================================
            write_report_rows(writer, fcsv, xnat_proj, subject, session_label, entry_by_id,
                              file_results, xnat_eid, overrides)

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
