# ADAPT XNAT Sender v1.1

DNAnexus applet that exports DICOM zip archives and non-DICOM, clinical files (SVS, CSV, PDF, JSON) from DNAnexus to XNAT.

## Repo Structure

```
dnax-to-xnat-sender/
├── dxapp.json                          # DNAnexus applet definition (inputs, outputs, runtime)
├── src/
│   └── launch.py                       # DNAnexus entry point (loads Docker, runs container)
├── docker/
│   ├── Dockerfile                      # Python 3.11 image with dependencies
│   └── app/
│       └── main.py                     # Core logic (DICOM import, resource upload, reporting)
└── docker-image-asset/
    ├── dxasset.json                    # DNAnexus asset metadata
    └── resources/opt/dnax2xnat/
        └── dnax2xnat-1.1.tar           # Pre-built Docker image (loaded at runtime)
```

## How It Works

1. Resolves manifest file entries (filenames or file IDs) on DNAnexus
2. Splits files into DICOM zips and non-DICOM resources by extension/suffix
3. **DICOM**: Validates metadata, streams to XNAT, confirms files are archived.
4. **Resources**: Finds existing XNAT session and uploads to type-specific resource folders (WSI/, CSV/, JSON/, PDF/). SVS files can create a new Slide Microscopy session if matching session does not exist.
5. Outputs a timestamped CSV report with detailed logging.

### Resource upload rules

- **Project check**: every target XNAT project is checked before anything is sent (IDs are case-sensitive). Files for a project that does not exist are reported as not sent.
- **Verified**: every upload is checked against XNAT by size, and by MD5 when XNAT stores checksums (XNAT site setting).
- **Additive, never overwritten**: if a file with the same name is already in the session resource:
  - identical (same size and MD5) -> not re-sent, reported as already on XNAT
  - same size, but XNAT stores no checksum to compare -> not re-sent, reported as not sent (rename a corrected file to send it)
  - different -> stored next to it as a new version with a UTC timestamp, e.g. `report_20261118T153045Z.pdf`
- **Subject check**: files are only attached to an existing session if XNAT holds that session under the same subject as the manifest. Otherwise they are not sent and the report gives both subjects.
- **Safe retries**: after a timeout or 5xx error the applet first checks whether XNAT saved the file anyway, then retries (up to 3 attempts). DICOM-zip imports are sent exactly once, since repeating an import is not safe.
- **Names**: labels and file names are URL-encoded; file names containing `\ / ? # % + ; : * " < > |` are refused with a message.
- **Permission errors**: if XNAT refuses a write (HTTP 403), the run stops sending and the remaining files are reported as not sent.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `manifest` | file (.yaml) | Yes | Hierarchical YAML defining project/subject/session/files |
| `secrets` | protected file (.yaml) | Yes | XNAT authentication |
| `src_root_path` | string | Yes | DNAnexus data base path (e.g. `project-XXXX:/test_data/folder`) |
| `xnat_project` | string | No | Override XNAT project from manifest |
| `report_filename` | string | No | Output CSV name (default: `export_report.csv`, always UTC-timestamped) |

## Example of Manifest shown using a sample subject from the open source CMB-BRCA dataset

```yaml
images:
  CMB-BRCA:                          # XNAT project
    MSB-06586:                       # Subject
      MSB-06586-MR-19591112:         # Session label
        - 201.zip                    # DICOM zip , DNAnexus file IDs are extracted from metadata
        - 301.zip                    # DICOM zip , DNAnexus file IDs are extracted from metadata
        - MSB-06586-EX001-SVS.zip    # Non-DICOM -> WSI XNAT session resource folder , DNAnexus file IDs are extracted from metadata
        - clinical_report.pdf        # Non-DICOM -> PDF XNAT session resource folder , DNAnexus file IDs are extracted from metadata
        - lab_results.csv            # Non-DICOM -> CSV XNAT session resource folder , DNAnexus file IDs are extracted from metadata
        - file-XXXXXXXXXXXXXXXXXXXX  # OR DNAnexus file IDs (used directly). 
```

Filenames are resolved by looking them up in `{src_root_path}/{project}/{subject}/{session}/` on DNAnexus.

### File Type Detection

| Pattern | Type | XNAT Destination |
|---------|------|------------------|
| `*.zip`(explicit checks IF DICOM or not) | DICOM | Prearchive -> auto-archive |
| `*-SVS.zip` or `*.svs` | SVS resource | Session resource folder `WSI/` |
| `*-CSV.zip` or `*.csv` | CSV resource | Session resource folder `CSV/` |
| `*-JSON.zip` or `*.json` | JSON resource | Session resource folder `JSON/` |
| `*-PDF.zip` or `*.pdf` | PDF resource | Session resource folder `PDF/` |

DICOM files must be **zip archives**. 

## Output

A CSV report with columns: `project, subject, session_label, file_name, file_id, sent_to_xnat, xnat_experiment_id, message, xnat_resource, xnat_file_name, md5`

`xnat_file_name` is the name stored on XNAT (differs from `file_name` when a new timestamped version was stored); `md5` is the MD5 of the DNAnexus file.

## Running

```bash
dx run adapt-xnat-sender \
  -imanifest=manifest.yaml \
  -isecrets=secrets.yaml \ # protected, to be set up on DNAnexus platform
  -isrc_root_path="project-XXXX:/test_data/test-xnat-sender"
```

## Building the XNAT Sender Applet and Deploying to DNAnexus

The Docker image can be built as follows. Note : the docker image must be rebuilt whenever `docker/app/main.py` changes. 
Perform these steps when logged into the DNAnexus platform (dx login)

```bash
docker buildx build --platform linux/amd64 -t dnax2xnat:1.1 --load docker/
docker save dnax2xnat:1.1 -o docker-image-asset/resources/opt/dnax2xnat/dnax2xnat-1.1.tar
dx build_asset docker-image-asset --destination <project>:/ 
# Update dxapp.json assetDepends with new asset record ID
dx build . --destination <project>:/
```

**Note:** The `dxapp.json` ships with a placeholder asset ID (`record-Insert-your-custom-build-assetID-here`). After running `dx build_asset`, replace it with your actual asset record ID before building the applet.
