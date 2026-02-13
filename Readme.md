# ADAPT XNAT Sender v1.0

DNAnexus applet that exports DICOM zip archives and non-DICOM, clinical files (SVS, CSV, PDF, JSON) from DNAnexus to XNAT.

## How It Works

1. Resolves manifest file entries (filenames or file IDs) on DNAnexus
2. Splits files into DICOM zips and non-DICOM resources by extension/suffix
3. **DICOM**: Validates metadata, streams to XNAT, confirms files are archived.
4. **Resources**: Finds existing XNAT session and uploads to type-specific resource folders (SVS/, CSV/, JSON/, PDF/). SVS files can create a new Slide Microscopy session if matching session exists.
5. Outputs a timestamped CSV report with detailed logging.

## Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `manifest` | file (.yaml) | Yes | Hierarchical YAML defining project/subject/session/files |
| `secrets` | protected file (.yaml) | Yes | XNAT authentication |
| `src_root_path` | string | Yes | DNAnexus data base path (e.g. `project-XXXX:/test_data/folder`) |
| `xnat_project` | string | No | Override XNAT project from manifest |
| `report_filename` | string | No | Output CSV name (default: `export_report.csv`, always UTC-timestamped) |

## Example of Manifest Format shown using a sample subject from the open source CMB-BRCA dataset

```yaml
images:
  CMB-BRCA:                          # XNAT project
    MSB-06586:                       # Subject
      MSB-06586-MR-19591112:         # Session label
        - 201.zip                    # DICOM zip , DNAnexus file IDs are extracted from metadata
        - 301.zip                    # DICOM zip , DNAnexus file IDs are extracted from metadata
        - MSB-06586-EX001-SVS.zip    # Non-DICOM -> SVS XNAT session resource folder , DNAnexus file IDs are extracted from metadata
        - clinical_report.pdf        # Non-DICOM -> PDF XNAT session resource folder , DNAnexus file IDs are extracted from metadata
        - lab_results.csv            # Non-DICOM -> CSV XNAT session resource folder , DNAnexus file IDs are extracted from metadata
        - file-XXXXXXXXXXXXXXXXXXXX  # OR DNAnexus file IDs (used directly). 
```

Filenames are resolved by looking them up in `{src_root_path}/{project}/{subject}/{session}/` on DNAnexus.

### File Type Detection

| Pattern | Type | XNAT Destination |
|---------|------|------------------|
| `*.zip` (no known suffix) | DICOM | Prearchive -> auto-archive |
| `*-SVS.zip` or `*.svs` | SVS resource | Session resource folder `SVS/` |
| `*-CSV.zip` or `*.csv` | CSV resource | Session resource folder `CSV/` |
| `*-JSON.zip` or `*.json` | JSON resource | Session resource folder `JSON/` |
| `*-PDF.zip` or `*.pdf` | PDF resource | Session resource folder `PDF/` |

DICOM files must be **zip archives**. 

## Output

A CSV report with columns: `project, subject, session_label, file_name, file_id, sent_to_xnat, xnat_experiment_id, message`

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
docker buildx build -t dnax2xnat:1.0 --load docker/
docker save dnax2xnat:1.0 -o docker-image-asset/resources/opt/dnax2xnat/dnax2xnat-1.0.tar
dx build_asset docker-image-asset --destination <project>:/ --brief
# Update dxapp.json assetDepends with new asset record ID
dx build . --destination <project>:/
```

**Note:** The `dxapp.json` ships with a placeholder asset ID (`record-Insert-your-custom-build-assetID-here`). After running `dx build_asset`, replace it with your actual asset record ID before building the applet.
