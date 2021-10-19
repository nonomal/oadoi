from recordthresher.record_maker import CrossrefRecordMaker
from recordthresher.util import parseland_authors


class BiorxivRecordMaker(CrossrefRecordMaker):
    @staticmethod
    def _is_specialized_record_maker(pub):
        return pub.doi.startswith('10.1101/') and pub.genre == 'posted-content'

    @classmethod
    def _make_source_specific_record_changes(cls, record, pub):
        if (pl_authors := parseland_authors(cls._parseland_api_url(pub))) is not None:
            record.authors = pl_authors
