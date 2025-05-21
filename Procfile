# long web timeout value needed to facilitate proxy of s3 changefile content
# setting to 10 hours: 60*60*10=36000
web: gunicorn views:app -w $WEB_WORKERS_PER_DYNO --timeout 36000 --reload
web_dev: gunicorn views:app -w 2 --timeout 36000 --reload
update: bin/start-pgbouncer-stunnel bash run_worker.sh
refresh: bin/start-pgbouncer-stunnel bash run_hybrid_worker.sh
refresh_aux: bin/start-pgbouncer-stunnel bash run_hybrid_worker_aux_0.sh
run_pmh: bash run_pmh.sh
run_repo: bash run_repo.sh
run_pdf_url_check: bin/start-pgbouncer-stunnel bash run_pdf_url_check.sh
green_scrape: bash run_green_scrape_worker.sh
green_scrape_arxiv: bash run_green_scrape_arxiv_worker.sh
publisher_scrape: bash run_publisher_scrape_worker.sh
repo_oa_location_export: python repo_oa_location_export.py
pubmed_record_queue: bash run_pubmed_record_worker.sh
pmh_rt_record_queue: bash run_pmh_rt_record_worker.sh
doi_rt_record_queue: bash run_doi_rt_record_worker.sh
datacite_record_queue: bash run_datacite_doi_rt_record_worker.sh
recordthresher_refresh: bash run_recordthresher_refresh_worker.sh
recordthresher_refresh_enqueue_top_25: bash run_recordthresher_refresh_enqueue_top_25.sh
crossref_snapshot_sync: bash run_crossref_snapshot_sync.sh
parse_pdfs: bash run_pdf_parse_worker.sh
download_pdfs: bash run_pdf_download_worker.sh
passive_enqueue_missing_affs: python3 passive_enqueue_missing_affs.py
