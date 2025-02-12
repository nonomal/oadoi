import os

LANDING_PAGE_ARCHIVE_BUCKET = 'unpaywall-doi-landing-page'
LANDING_PAGE_ARCHIVE_BUCKET_NEW = 'openalex-harvested-html'
PDF_ARCHIVE_BUCKET = 'unpaywall-doi-pdf'
PDF_ARCHIVE_BUCKET_NEW = 'openalex-harvested-pdfs'
GROBID_XML_BUCKET = 'grobid-xml'


ZYTE_API_URL = "https://api.zyte.com/v1/extract"
ZYTE_API_KEY = os.getenv("ZYTE_API_KEY")