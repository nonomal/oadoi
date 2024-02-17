import datetime
import json

import dateutil.parser
from dateutil.parser import ParserError
import shortuuid
from sqlalchemy import orm
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.attributes import flag_modified

from app import db


class Record(db.Model):
    __tablename__ = 'recordthresher_record'
    __table_args__ = {'schema': 'ins'}
    __bind_key__ = 'openalex'

    id = db.Column(db.Text, primary_key=True)
    updated = db.Column(db.DateTime)

    # ids
    record_type = db.Column(db.Text, nullable=False)
    doi = db.Column(db.Text)
    pmid = db.Column(db.Text)
    pmh_id = db.Column(db.Text)
    pmcid = db.Column(db.Text)
    arxiv_id = db.Column(db.Text)

    # metadata
    title = db.Column(db.Text)
    published_date = db.Column(db.DateTime)
    genre = db.Column(db.Text)
    abstract = db.Column(db.Text)
    mesh = db.Column(db.Text)
    publisher = db.Column(db.Text)
    normalized_book_publisher = db.Column(db.Text)
    institution_host = db.Column(db.Text)
    is_retracted = db.Column(db.Boolean)
    volume = db.Column(db.Text)
    issue = db.Column(db.Text)
    first_page = db.Column(db.Text)
    last_page = db.Column(db.Text)

    # related tables
    citations = db.Column(db.Text)
    authors = db.Column(db.Text)

    # source links
    repository_id = db.Column(db.Text)
    journal_issns = db.Column(db.Text)
    journal_issn_l = db.Column(db.Text)
    venue_name = db.Column(db.Text)

    # record data
    record_webpage_url = db.Column(db.Text)
    record_webpage_archive_url = db.Column(db.Text)
    record_structured_url = db.Column(db.Text)
    record_structured_archive_url = db.Column(db.Text)

    # oa and urls
    work_pdf_url = db.Column(db.Text)
    work_pdf_archive_url = db.Column(db.Text)
    is_work_pdf_url_free_to_read = db.Column(db.Boolean)
    is_oa = db.Column(db.Boolean)
    open_license = db.Column(db.Text)
    open_version = db.Column(db.Text)

    normalized_title = db.Column(db.Text)
    funders = db.Column(db.Text)


    def __init__(self, **kwargs):
        self.id = shortuuid.uuid()[0:20]
        self.error = ""
        self.updated = datetime.datetime.utcnow().isoformat()

        self._original_json = {}
        super(Record, self).__init__(**kwargs)

    @orm.reconstructor
    def init_on_load(self):
        self._original_json = {}

    __mapper_args__ = {'polymorphic_on': record_type}

    def __repr__(self):
        return "<Record ( {} ) {}, {}, {}>".format(self.id, self.record_type, self.doi, self.title)

    def set_authors(self, authors):
        self.authors = authors

    def _set_datetime(self, name, value):
        if isinstance(value, str):
            try:
                default_datetime = datetime.datetime(datetime.MAXYEAR, 1, 1)
                parsed_value = dateutil.parser.parse(value, default=default_datetime)
                if parsed_value.year == datetime.MAXYEAR and str(datetime.MAXYEAR) not in value:
                    parsed_value = None
            except ParserError:
                parsed_value = None

            if parsed_value:
                value = parsed_value
            else:
                value = None
        elif isinstance(value, datetime.datetime):
            pass

        setattr(self, name, value)

    def set_published_date(self, published_date):
        self._set_datetime('published_date', published_date)

    @staticmethod
    def remove_json_keys(obj, keys):
        obj_copy = json.loads(json.dumps(obj))

        if isinstance(obj_copy, dict):
            for key in keys:
                try:
                    del obj_copy[key]
                except KeyError:
                    pass

            obj_keys = obj_copy.keys()
            for obj_key in obj_keys:
                if isinstance(obj_copy[obj_key], dict) or isinstance(obj_copy[obj_key], list):
                    obj_copy[obj_key] = Record.remove_json_keys(obj_copy[obj_key], keys)
        elif isinstance(obj_copy, list):
            for i in range(0, len(obj_copy)):
                if isinstance(obj_copy[i], dict) or isinstance(obj_copy[i], list):
                    obj_copy[i] = Record.remove_json_keys(obj_copy[i], keys)

        return obj_copy

    def set_jsonb(self, name, value):
        if name not in self._original_json:
            original_value = getattr(self, name)
            self._original_json[name] = json.dumps(original_value, sort_keys=True, indent=2)

        setattr(self, name, value)

    def flag_modified_jsonb(self, ignore_keys=None):
        ignore_keys = ignore_keys or {}

        for attribute_name in self._original_json:
            original_value = json.loads(self._original_json[attribute_name])
            current_value = getattr(self, attribute_name)

            if attribute_name in ignore_keys:
                original_value = Record.remove_json_keys(original_value, ignore_keys[attribute_name])
                current_value = Record.remove_json_keys(current_value, ignore_keys[attribute_name])

            original_json = json.dumps(original_value, sort_keys=True, indent=2)
            current_json = json.dumps(current_value, sort_keys=True, indent=2)

            if original_json != current_json:
                flag_modified(self, attribute_name)


class RecordFulltext(db.Model):
    __table_args__ = {'schema': 'mid'}
    __tablename__ = "record_fulltext"
    __bind_key__ = 'openalex'

    recordthresher_id = db.Column(db.Text, db.ForeignKey("ins.recordthresher_record.id"), primary_key=True)
    fulltext = db.Column(db.Text)


class RecordthresherParentRecord(db.Model):
    __table_args__ = {'schema': 'ins'}
    __tablename__ = "recordthresher_parent_record"
    __bind_key__ = 'openalex'

    record_id = db.Column(db.Text, primary_key=True)
    parent_record_id = db.Column(db.Text, primary_key=True)


Record.fulltext = db.relationship(RecordFulltext, lazy='selectin', viewonly=True, uselist=False)

