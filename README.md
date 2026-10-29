# VISTA processing code

This repository holds the code that produced VISTA, the VIIRS Surface Water
Temporal Atlas. VISTA is a global record of dekadal (ten-day) flood
observations and the historical flood frequencies derived from them. It is built from the NOAA/GMU VIIRS flood product at about
375 m resolution and covers the third dekad of January 2012 to the third dekad
of April 2026. The dataset is described in a Data Descriptor prepared for
*Scientific Data* (citation to follow).

The VISTA website, <https://diem-vista.web.app/>, describes the products,
offers GeoTIFF downloads and hosts the user guide. The released data are
public:

- Cloud Storage: `gs://vista_project`, with a full object manifest at
  <https://storage.googleapis.com/vista_project/vista_publication_inventory.csv>
- Earth Engine: `projects/fao-oer/assets/vista_dekadal_archive`,
  `projects/fao-oer/assets/vista_monthly_archive` and
  `projects/fao-oer/assets/vista_products`

## Pipeline

| Phase | Entry point | What it does | Paper section |
| --- | --- | --- | --- |
| 1 | `jobs.run_phase1` | Downloads NOAA VIIRS 5-day composites, reclassifies source values into VISTA classes and combines the composites of each dekad | Methods 3 to 5 and 7 |
| 1, Pipeline B | `jobs.run_phase1_pipeB` | Recovers dekads the NOAA run logged as missing, from the GMU archive, with the same classifier. For the released record this covered January 2021 to August 2023 | Methods 6 |
| 2 | `jobs.run_phase2` | Uploads dekadal rasters to Cloud Storage and ingests them into Earth Engine | Methods 8 |
| 3 | `jobs.run_phase3` | Computes count, frequency and dominant-class products per calendar dekad in Earth Engine | Methods 9 to 11 |
| 3B | `jobs.run_phase3b` | Mosaics the exported tiles into one Cloud Optimized GeoTIFF per product | Methods 13 |
| 4 | `jobs.run_phase4` | Builds the rolling three-dekad monthly archive | Methods 12 |
| 5 | `jobs.run_phase5` | Computes count, frequency and dominant-class products over the monthly archive | Methods 12 |

`jobs.run_phase1_and_2` and `jobs.run_phase1_pipeB_and_2` run Phase 1 and
Phase 2 dekad by dekad. `core/phase1_local/process_dekad_composites.py` holds
the reclassification table and the within-dekad priority rule. The dekadal
calendar is in `data/calendars/`.

## Requirements

- Conda, to create the environment in `environment.yml`. The environment also
  provides the GDAL command-line tools (`gdalbuildvrt`, `gdal_translate`) used
  by Phase 3B and the `earthengine` command used by Phase 2.
- AWS CLI version 2 on the `PATH`. Phase 1 downloads from the public NOAA
  bucket `s3://noaa-jpss` without credentials. The Phase 1 module looks for
  the `aws` command when it is imported, and the other phases import it, so the
  CLI must be installed even for runs that do not download.
- For Phases 2 to 5, the Google Cloud CLI (`gcloud`) and a Google Cloud project
  with Cloud Storage and Earth Engine enabled, with write access to your own
  bucket and asset folder. Dry runs still read existing objects and assets, so
  they need read access. Phase 1 and Pipeline B run locally without Google Cloud.

## Setup

```bash
conda env create -f environment.yml
conda activate fao-vista
cp config/config.example.yaml config/config.yaml
```

Edit `config/config.yaml`: set `paths.data_root` to a local working folder and
replace `your-gcp-project` and `your-bucket` with your own project and bucket.

Phase 1 has no dry run. It requires `phase1_local.processing.dekad_ids` and
stops if it is empty; once dekads are selected it downloads composites, writes
rasters under `data_root` and deletes intermediates according to the
`housekeeping` settings. Start with one dekad and one tile, for example
`dekad_ids: 469` and `tile_id: 115`. Pipeline B processes only the dekads
listed in the availability log that Phase 1 writes, unless
`phase1_pipeB.force_pipeline_b` is true.

Phases 2, 3, 3B, 4 and 5 have a `dry_run` switch, set to true in the example,
so their first run lists the planned uploads and exports without making them.

Authenticate before running the cloud phases:

```bash
gcloud auth application-default login
earthengine authenticate
```

## Running

Run each phase as a module from the repository root, for example:

```bash
python -m jobs.run_phase1
python -m jobs.run_phase3
```

Select dekads and products in `config/config.yaml`; the comments next to each
setting explain the accepted values.

### Run order

Phases 2 to 5 submit Earth Engine tasks and return before the tasks finish.
Wait until every task of a step has completed (Tasks tab of the Earth Engine
Code Editor, or `earthengine task list`) before starting the next step:

1. Phase 1 and Phase 2 (`jobs.run_phase1_and_2`, or `jobs.run_phase1` then
   `jobs.run_phase2`).
2. Pipeline B with its own ingestion, `jobs.run_phase1_pipeB_and_2`, for the
   dekads Phase 1 logged as missing. Standalone Phase 2 does not upload the
   Pipeline B rasters (their file names end in `_pipeB`); if you ran
   `jobs.run_phase1_pipeB` first, the combined runner reuses its local
   rasters. Wait until both sets of ingestions have finished.
3. Phase 3 with `statistic: count`, then `frequency` and `dominant`, which read
   the count assets.
4. Phase 3B, once every GeoTIFF tile of the selected products has been
   exported.
5. Phase 4, then Phase 5 with `statistic: count`, then `frequency` and
   `dominant`.

Earth Engine does not create parent folders, and even a dry run lists the
folders a phase reads from and writes to. Before the first cloud run, dry or
not, create the asset roots set in your `config/config.yaml`, for example:

```bash
earthengine --project your-gcp-project create folder -p projects/your-gcp-project/assets/vista_dekadal_archive
```

Do the same for the monthly archive and products roots, and for the output
folders that Phases 3 and 5 print at the start of each run ("Statistics asset
root").

Phases 4 and 5 save only Earth Engine assets unless `run_control.export_gcs`
is true; set it to produce the GeoTIFF files. `jobs.run_phase1_and_2` and
`jobs.run_phase1_pipeB_and_2` run the local Phase 1 processing even when
Phase 2 is in dry run.

## Scope and limitations

- Classes 1, 2 and 3 record the observed water-fraction intensity of the
  source product (1-49%, 50-79% and 80-100%). They do not say that the water
  was unusual. Whether a frequency is exceptional depends on a threshold the
  user chooses against the historical frequency products.
- A dekadal value combines the composites of that dekad by a fixed class
  priority, not by the numeric maximum of the class codes:
  3 > 2 > 1 > 6 > 5 > 4 > 10 > 11 > 9 > 0 (`DEKAD_AGGREGATION_PRIORITY` in
  `core/phase1_local/process_dekad_composites.py`). A flood class (1, 2 or 3)
  in any composite therefore wins over every other class, including permanent
  water (11), and a false flood detection in a single composite is kept.
- The dekads from January 2021 to August 2023 come from the GMU archive
  through Pipeline B, because distribution through the NOAA stream was
  interrupted over that period.
- Dekads rejected by quality control are listed, with the reason, in
  `config/quality_control.yaml` and are left out of the statistics. The
  object manifest lists every released file, so dekads missing from the record
  can be identified there.

## Tests

```bash
python -m unittest discover -s tests -t .
```

## Citation

Cite the Data Descriptor and the version of this code you used, through its
Zenodo DOI (to be added on the first release). `CITATION.cff` holds the
citation metadata.

## Licence

- Code: MIT licence (see `LICENSE`).
- VISTA data: CC BY 4.0.
- The NOAA/GMU VIIRS flood product that VISTA is built from is not
  redistributed here and remains under its provider's terms.

Copyright FAO, Viale delle Terme di Caracalla, 00153 Rome, Italy.
Contact for copyright and terms of use: <copyright@fao.org>.

## Acknowledgements

VISTA is built on the VIIRS flood product of NOAA and the JPSS programme,
distributed through the NOAA archive on AWS and by George Mason University.

## Contact

Report bugs and questions about the code as
[GitHub issues](https://github.com/un-fao/fao-vista/issues). For other
questions, contact Andrea Amparore, Food and Agriculture Organization of the
United Nations (andrea.amparore@fao.org). `CONTRIBUTING.md` explains how
changes reach this repository.
